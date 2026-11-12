"""
Optional resource access policy for converting untrusted SVG documents.

A :class:`ResourcePolicy` restricts which external resources CairoSVG is
allowed to load while converting a document:

- no network access: only local files (and optionally ``data:`` URLs) are
  allowed, HTTP/HTTPS and every other protocol are rejected;
- files can only be read inside a caller-defined root directory, resolved by
  real file location (``..``, percent-encoding and symlinks cannot escape);
- each resource is limited to ``max_resource_bytes`` bytes (counted after
  ``data:`` decoding), and files are never read unboundedly before the size
  check.

Denials raise :class:`ResourceAccessError`; they never rely on underlying
network or I/O failures.

"""

import base64
from pathlib import Path
from urllib.parse import unquote, unquote_to_bytes, urlparse, urlunparse

LOCAL_FILE_HOSTS = ('', 'localhost')


class ResourceAccessError(Exception):
    """Raised when a :class:`ResourcePolicy` denies access to a resource.

    The ``url`` attribute holds the exact resource that was denied and the
    ``reason`` attribute a human-readable explanation. The string
    representation is sanitized: credentials, query strings and ``data:``
    payloads are never included.

    """

    def __init__(self, url, reason):
        self.url = url
        self.reason = reason
        super().__init__(
            f'Resource access denied ({reason}): {sanitize_url(url)}')


def sanitize_url(url, limit=200):
    """Return a representation of ``url`` safe to include in error messages.

    Userinfo (passwords), query strings, fragments and ``data:`` payloads are
    stripped so that no secret embedded in the URL is leaked.

    """
    try:
        parsed = urlparse(str(url))
        if parsed.scheme.lower() == 'data':
            return 'data:<payload redacted>'
        netloc = parsed.netloc
        if '@' in netloc:
            netloc = netloc.rsplit('@', 1)[-1]
        safe = urlunparse(
            (parsed.scheme, netloc, parsed.path, '', '', ''))
    except Exception:
        safe = '<unparseable URL>'
    if len(safe) > limit:
        safe = safe[:limit] + '...'
    return safe


class ResourcePolicy:
    """Policy restricting which external resources may be loaded.

    :param root: directory outside of which no file may be read. The
        boundary is enforced on the real (symlink-resolved) file location.
    :param allow_data: whether ``data:`` URLs are allowed.
    :param max_resource_bytes: maximum size in bytes of any single loaded
        resource, counted after ``data:`` decoding. ``None`` means no limit;
        when set, it must be a positive integer.

    """

    def __init__(self, root, allow_data=True, max_resource_bytes=None):
        if max_resource_bytes is not None and (
                isinstance(max_resource_bytes, bool) or
                not isinstance(max_resource_bytes, int) or
                max_resource_bytes <= 0):
            raise ValueError(
                'max_resource_bytes must be a positive integer or None')
        self.root = Path(root).resolve()
        self.allow_data = bool(allow_data)
        self.max_resource_bytes = max_resource_bytes

    def wrap_fetcher(self, fetcher=None):
        """Return a URL fetcher enforcing this policy.

        ``fetcher`` is an optional user-defined URL fetcher. The returned
        fetcher validates the URL against the policy *before* calling it, so
        a custom fetcher cannot be used to bypass protocol or file boundary
        restrictions, and checks the size of the bytes it returns.

        """
        def policy_fetcher(url, resource_type):
            kind, value = self._validate(url)
            if kind == 'data':
                return value
            if fetcher is not None:
                data = fetcher(url, resource_type)
                self._check_size(url, len(data))
                return data
            return self._read_file(value, url)
        return policy_fetcher

    def _validate(self, url):
        """Check ``url`` against the policy.

        Return ``('data', decoded_bytes)`` for ``data:`` URLs and
        ``('file', resolved_path)`` for local files. Raise
        :class:`ResourceAccessError` for anything the policy forbids.

        """
        text = str(url)
        parsed = urlparse(text)
        scheme = parsed.scheme.lower()

        if scheme == 'data':
            if not self.allow_data:
                raise ResourceAccessError(
                    text, 'data: URLs are not allowed by this policy')
            return 'data', self._decode_data(text)

        if scheme not in ('', 'file'):
            raise ResourceAccessError(
                text, f'protocol "{scheme or text.split(":", 1)[0]}" is not '
                'allowed, only local files are')

        if scheme == 'file':
            # Strip userinfo and port before comparing the host.
            host = parsed.netloc.rsplit('@', 1)[-1].split(':', 1)[0].lower()
            if host not in LOCAL_FILE_HOSTS:
                raise ResourceAccessError(
                    text, f'file: URL host "{host}" is not local')

        # Fragments identify a part of a document, not another resource.
        path = unquote(parsed.path)
        if not path:
            raise ResourceAccessError(
                text, 'URL does not designate a loadable resource')

        # resolve() collapses "..", follows symlinks and makes the path
        # absolute, so the boundary check applies to the real file location.
        resolved = Path(path).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise ResourceAccessError(
                text, f'path is outside of allowed root "{self.root}"')
        return 'file', resolved

    def _decode_data(self, url):
        """Decode a ``data:`` URL and enforce the size limit on the result."""
        header, _, payload = url.partition(',')
        if ';base64' in header.lower():
            data = base64.b64decode(payload)
        else:
            data = unquote_to_bytes(payload)
        self._check_size(url, len(data))
        return data

    def _read_file(self, path, url):
        """Read a local file, never reading more than the size limit + 1."""
        limit = self.max_resource_bytes
        with open(path, 'rb') as stream:
            if limit is None:
                return stream.read()
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ResourceAccessError(
                url, f'resource is larger than {limit} bytes')
        return data

    def _check_size(self, url, size):
        limit = self.max_resource_bytes
        if limit is not None and size > limit:
            raise ResourceAccessError(
                url, f'resource is larger than {limit} bytes')
