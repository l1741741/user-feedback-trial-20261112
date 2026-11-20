"""
Explicit error redaction for task failures.

Tasks may handle signed file URLs and authentication fields. When a task
fails, the exception text, traceback and the error metadata stored in the
result backend are persisted by several systems, so this module provides a
configurable boundary that removes caller-declared secrets from everything
Huey surfaces publicly (logs, the error result, the ``SIGNAL_ERROR`` signal
and ``on_error`` handler arguments) while preserving the information needed
to debug a failure: the task id, the exception *type* and the code locations
in the traceback.

The original exception instance is never mutated or replaced while a task is
running: retry bookkeeping and the post-execute hooks keep operating on the
raw exception. A sanitized copy is produced at each output boundary. If the
redaction machinery itself fails, a minimal safe fallback is used instead and
the original task finalization continues uninterrupted.
"""
import re
from urllib.parse import urlsplit
from urllib.parse import urlunsplit


# Placeholder substituted for every redacted value.
REDACTION_PLACEHOLDER = '***REDACTED***'

# Marker used when even the safe fallback cannot render an exception.
REDACTION_FAILED = 'redaction failed'


class RedactedError(Exception):
    """
    Public, picklable representation of a failed task's exception.

    The original exception type name is preserved (including the module) so
    that error metadata remains actionable, while ``args`` only contains
    sanitized strings. Instances are real exceptions, so code that receives
    them (e.g. ``on_error`` handlers, signal receivers) can use normal
    exception handling.
    """
    def __init__(self, original_type, message, details=None):
        self.original_type = original_type
        self.details = details or {}
        super(RedactedError, self).__init__(message)

    def __repr__(self):
        # Mirror the standard "TypeName(sanitized message)" form while
        # preserving the original exception's qualified type name.
        return '%s(%s)' % (self.original_type, self.args[0])

    def __reduce__(self):
        # Explicit reduce so instances survive every serializer/start-method.
        return (self.__class__, (self.original_type, self.args[0],
                                 self.details))


class RedactionFailedError(RedactedError):
    """
    Raised (as a sanitized stand-in) when redaction itself could not produce
    a safe representation of the original exception.
    """
    def __init__(self, original_type, reason=None):
        details = {'redaction_failed': True}
        if reason:
            details['reason'] = reason
        super(RedactionFailedError, self).__init__(
            original_type, REDACTION_FAILED, details)

    def __reduce__(self):
        return (self.__class__, (self.original_type,
                                 self.details.get('reason')))


# Matches "key=value" / "key: value" pairs where key names a sensitive field.
# Handles both bare and quoted keys (dict reprs). The key name is retained
# (case-insensitive); only the value is replaced.
_SENSITIVE_KEY_RE_TMPL = (
    r'(?i)(?P<key>[\'"]?\b(?:%s)\b[\'"]?\s*[:=]\s*)'
    r'(?:"[^"]*"|\'[^\']*\'|[^\s,;&\'"]+)')

# Query-string parameter names that are treated as sensitive even when they
# were not explicitly configured (authentication material in URLs).
_DEFAULT_SENSITIVE_QUERY_PARAMS = (
    'password', 'passwd', 'pwd', 'secret', 'token', 'access_token',
    'refresh_token', 'api_key', 'apikey', 'auth', 'authorization',
    'signature', 'sig', 'sign', 'signed', 'credential', 'credentials',
    'code', 'key', 'session', 'session_key')


def _build_key_re(names):
    escaped = '|'.join(re.escape(n) for n in names)
    return re.compile(_SENSITIVE_KEY_RE_TMPL % escaped)


def _build_query_param_set(names):
    params = set(n.strip().lower() for n in names if n)
    params.update(_DEFAULT_SENSITIVE_QUERY_PARAMS)
    return params


def _safe_text(value):
    """
    Convert an arbitrary exception argument into printable text without
    raising. Binary data is rendered as a length marker instead of being
    decoded, so a secret embedded in bytes is never turned into text.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return '<bytes len=%d>' % len(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    try:
        text = str(value)
    except Exception:
        try:
            text = repr(value)
        except Exception:
            return '<unprintable %s object>' % (
                getattr(type(value), '__name__', 'unknown'))
    if not isinstance(text, str):
        return '<unprintable object>'
    return text


def _redact_query(query, sensitive_params):
    pieces = []
    for piece in query.split('&'):
        if not piece:
            pieces.append(piece)
            continue
        name, sep, _value = piece.partition('=')
        if sep and name.lower() in sensitive_params:
            pieces.append(name + '=' + REDACTION_PLACEHOLDER)
        else:
            pieces.append(piece)
    return '&'.join(pieces)


def _redact_one_url(match, sensitive_params):
    raw = match.group(0)
    try:
        parts = urlsplit(raw)
    except Exception:
        return raw
    if not parts.scheme or not parts.netloc:
        return raw
    netloc = parts.netloc
    if '@' in netloc:
        # Strip the userinfo component entirely, keep host and port.
        netloc = REDACTION_PLACEHOLDER + '@' + netloc.rsplit('@', 1)[1]
    query = _redact_query(parts.query, sensitive_params)
    try:
        return urlunsplit((parts.scheme, netloc, parts.path, query,
                           parts.fragment))
    except Exception:
        return raw


# Any http(s)/ftp-like URL, including those embedded inside exception text.
_URL_RE = re.compile(r'[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s\'"\)\]\}>]+')


def _sanitize_url(text, sensitive_params):
    """
    Redact the userinfo component and sensitive query parameters of any URL
    embedded in ``text``. The URL's host, path and non-sensitive parameters
    are preserved so failures remain debuggable.
    """
    try:
        return _URL_RE.sub(
            lambda m: _redact_one_url(m, sensitive_params), text)
    except Exception:
        # URL parsing must never break redaction of the surrounding text.
        return text


class ErrorRedactor(object):
    """
    Configurable redaction policy for the public representation of task
    failures.

    :param secrets: literal secret values (strings or bytes) that must be
        replaced wherever they occur, e.g. API keys and passwords.
    :param sensitive_fields: field/parameter names whose *values* must be
        replaced in ``key=value`` style content (exception messages, nested
        string arguments).
    :param sensitive_url_params: extra URL query-parameter names whose values
        are redacted. A built-in set of authentication-related names is
        always included.
    :param max_depth: maximum depth followed across exception
        ``__cause__`` / ``__context__`` chains and nested arguments.
    :param max_width: maximum number of arguments rendered per exception.
    """
    def __init__(self, secrets=None, sensitive_fields=None,
                 sensitive_url_params=None, max_depth=10, max_width=50):
        self.secrets = []
        for secret in (secrets or ()):
            if secret:
                self.secrets.append(secret)
        # Pre-compute string forms of byte secrets for text replacement.
        self._secret_terms = []
        for secret in self.secrets:
            if isinstance(secret, bytes):
                self._secret_terms.append(secret)
                try:
                    self._secret_terms.append(secret.decode('utf-8'))
                except UnicodeDecodeError:
                    pass
            else:
                self._secret_terms.append(str(secret))
        self._secret_terms = [s for s in self._secret_terms if s]
        # Longer secrets first so overlapping values are matched wholesale.
        self._secret_terms.sort(key=len, reverse=True)

        self.sensitive_fields = tuple(sensitive_fields or ())
        self._sensitive_fields_lower = frozenset(
            f.lower() for f in self.sensitive_fields)
        self._key_re = (_build_key_re(self.sensitive_fields)
                        if self.sensitive_fields else None)
        self.sensitive_url_params = _build_query_param_set(
            tuple(sensitive_url_params or ()))
        self.max_depth = max_depth
        self.max_width = max_width

    # -- text boundary -----------------------------------------------------

    def sanitize_text(self, text):
        """Redact a single string. Never raises."""
        if text is None:
            return text
        try:
            if not isinstance(text, str):
                text = _safe_text(text)
            for term in self._secret_terms:
                if isinstance(term, bytes):
                    # Redact byte literals rendered into text, e.g.
                    # "b'prefix secret suffix'".
                    try:
                        literal = term.decode('utf-8')
                    except UnicodeDecodeError:
                        continue
                else:
                    literal = term
                if literal and literal in text:
                    text = text.replace(literal, REDACTION_PLACEHOLDER)
            if self._key_re is not None:
                text = self._key_re.sub(
                    lambda m: m.group('key') + REDACTION_PLACEHOLDER, text)
            text = _sanitize_url(text, self.sensitive_url_params)
            return text
        except Exception:
            # Last-resort: retain nothing of the original content.
            return REDACTION_PLACEHOLDER

    # -- exception boundary ------------------------------------------------

    def redact_exception(self, exc):
        """
        Return a sanitized, picklable exception representing ``exc``.

        The original exception (and its cause/context chain) is traversed but
        never modified. A reference to the original is retained on the
        returned object so callers can still render frame locations, but it is
        not part of the pickled/public state. On any internal failure a
        :class:`RedactionFailedError` carrying only the original exception type
        name is returned.
        """
        try:
            original_type = self._type_name(exc)
            message = self._render_exception(exc)
            details = self._collect_details(exc)
            redacted = RedactedError(original_type, message, details)
        except Exception as exc2:
            try:
                name = self._type_name(exc)
            except Exception:
                name = 'unknown'
            try:
                reason = self._safe_reason(exc2)
            except Exception:
                reason = None
            redacted = RedactionFailedError(name, reason)
        # Private, non-pickled link to the live failure for traceback
        # rendering within the same process. Never serialized to storage.
        redacted.__original_exception__ = exc
        return redacted

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _type_name(exc):
        exc_type = type(exc)
        module = getattr(exc_type, '__module__', None)
        name = getattr(exc_type, '__name__', 'unknown')
        return '%s.%s' % (module, name) if module else name

    @staticmethod
    def _safe_reason(exc):
        try:
            return type(exc).__name__
        except Exception:
            return None

    def _render_exception(self, exc, seen=None, depth=0):
        if seen is None:
            seen = set()
        # Recursive / self-referential exception chains terminate here.
        if id(exc) in seen or depth > self.max_depth:
            return '<recursive exception>'
        seen.add(id(exc))

        try:
            args = self._sanitize_args(exc, seen, depth)
        except Exception:
            args = (REDACTION_PLACEHOLDER,)
        try:
            args = tuple(args)[:self.max_width]
        except Exception:
            args = (REDACTION_PLACEHOLDER,)

        # Prefer a *custom* string representation (overridden __str__ or
        # __repr__), but sanitize its output rather than trusting it. The
        # base Exception implementations only reformat args, which were already
        # sanitized above -- using them would bypass binary-arg handling.
        custom = None
        if self._has_custom_render(exc):
            for method_name in ('__str__', '__repr__'):
                try:
                    method = getattr(exc, method_name)
                    rendered = method()
                    if rendered is not None:
                        custom = self.sanitize_text(_safe_text(rendered))
                        break
                except Exception:
                    continue

        if custom is not None:
            message = custom
        elif args:
            # Structured args (dicts/lists) were sanitized element-wise; run
            # their rendered form through the text policy as well so quoted
            # "key": value pairs in repr output are covered consistently.
            message = self.sanitize_text(', '.join(_safe_text(arg)
                                                    for arg in args))
        else:
            message = ''

        # Surface the sanitized cause/context chain consistently.
        chain = self._render_chain(exc, seen, depth)
        if chain:
            message = '%s | %s' % (message, chain) if message else chain
        return message

    @staticmethod
    def _has_custom_render(exc):
        for method_name in ('__str__', '__repr__'):
            method = getattr(type(exc), method_name, None)
            owner = getattr(method, '__qualname__', '')
            # Base implementations live on BaseException/object/Exception.
            if method is not None and not owner.startswith((
                    'BaseException.', 'Exception.', 'object.')):
                return True
        return False

    def _render_chain(self, exc, seen, depth):
        links = []
        for attr, label in (('__cause__', 'caused by'),
                            ('__context__', 'during handling of')):
            nested = getattr(exc, attr, None)
            if nested is None or id(nested) in seen:
                continue
            nested_text = self._render_exception(nested, seen, depth + 1)
            links.append('%s %s: %s' % (
                label, self._type_name(nested), nested_text))
        return '; '.join(links)

    def _sanitize_args(self, exc, seen, depth):
        result = []
        args = getattr(exc, 'args', ()) or ()
        try:
            iterator = iter(args)
        except TypeError:
            iterator = iter((args,))
        for arg in iterator:
            try:
                result.append(self._sanitize_value(arg, seen, depth))
            except RecursionError:
                raise
            except Exception:
                # An unrenderable argument must not take down redaction of
                # the whole exception, nor leak the raw argument.
                result.append(REDACTION_PLACEHOLDER)
        return result

    def _sanitize_value(self, value, seen, depth):
        if depth > self.max_depth:
            return REDACTION_PLACEHOLDER
        if isinstance(value, BaseException):
            if id(value) in seen:
                return '<recursive exception>'
            return self._render_exception(value, seen, depth + 1)
        if isinstance(value, bytes):
            # Never decode binary arguments: redact byte secrets in place and
            # otherwise keep only a length marker so the raw bytes (which may
            # contain credentials) are never rendered as text.
            if self._bytes_contain_secret(value):
                return REDACTION_PLACEHOLDER
            return '<bytes len=%d>' % len(value)
        if isinstance(value, str):
            return self.sanitize_text(value)
        if isinstance(value, dict):
            return self._sanitize_mapping(value, seen, depth)
        if isinstance(value, (list, tuple, set, frozenset)):
            return self._sanitize_sequence(value, seen, depth)
        # Other objects: render through their printable form and redact that.
        text = _safe_text(value)
        return self.sanitize_text(text)

    def _sanitize_mapping(self, value, seen, depth):
        sanitized = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= self.max_width:
                sanitized['...'] = '...'
                break
            key_text = self.sanitize_text(_safe_text(key))
            try:
                key_lower = key_text.lower()
            except Exception:
                key_lower = key_text
            if key_lower in self._sensitive_fields_lower:
                sanitized[key_text] = REDACTION_PLACEHOLDER
            else:
                sanitized[key_text] = self._sanitize_value(
                    item, seen, depth + 1)
        return sanitized

    def _sanitize_sequence(self, value, seen, depth):
        items = []
        for index, item in enumerate(value):
            if index >= self.max_width:
                items.append('...')
                break
            items.append(self._sanitize_value(item, seen, depth + 1))
        return items

    def _bytes_contain_secret(self, value):
        for term in self._secret_terms:
            if isinstance(term, bytes) and term and term in value:
                return True
        return False

    def _collect_details(self, exc):
        details = {}
        chain = []
        seen = set()
        current = exc
        depth = 0
        while current is not None and depth <= self.max_depth:
            if id(current) in seen:
                chain.append('<recursive exception>')
                break
            seen.add(id(current))
            chain.append(self._type_name(current))
            current = getattr(current, '__cause__', None) or \
                getattr(current, '__context__', None)
            depth += 1
        if chain:
            details['exception_chain'] = chain
        return details


def redact_traceback_text(redactor, text):
    """
    Apply the text-level policy to a formatted traceback string.

    Code locations and exception types are untouched; only secret literals,
    sensitive ``key=value`` content and URL credentials/query values are
    rewritten. Falls back to a generic marker if redaction fails, so an
    unredacted traceback is never persisted.
    """
    try:
        return redactor.sanitize_text(text)
    except Exception:
        return 'Traceback redacted (%s).' % REDACTION_FAILED
