from __future__ import annotations

import contextlib
import gzip
import logging
import os
import pickle
import shutil
from contextlib import ExitStack
from datetime import UTC
from email.utils import parsedate_to_datetime
from importlib import import_module
from pathlib import Path
from time import time
from typing import IO, TYPE_CHECKING, Any, Concatenate, cast
from urllib.parse import quote
from weakref import WeakKeyDictionary

from w3lib.http import headers_dict_to_raw, headers_raw_to_dict

from scrapy.http import Response
from scrapy.utils.httpobj import urlparse_cached
from scrapy.utils.project import data_path
from scrapy.utils.python import to_unicode
from scrapy.utils.response import response_from_dict

if TYPE_CHECKING:
    import os
    from collections.abc import Callable
    from types import ModuleType

    from scrapy.http.request import Request
    from scrapy.settings import BaseSettings
    from scrapy.spiders import Spider
    from scrapy.utils.request import RequestFingerprinterProtocol


logger = logging.getLogger(__name__)


def _get_scope(settings: BaseSettings, supported: tuple[str, ...]) -> str:
    scope: str = settings["HTTPCACHE_SCOPE"]
    if scope not in supported:
        supported_repr = ", ".join(repr(value) for value in supported)
        raise ValueError(
            f"Unsupported HTTPCACHE_SCOPE value: {scope!r}. The cache storage "
            f"in use supports the following values: {supported_repr}."
        )
    return scope


class DummyPolicy:
    def __init__(self, settings: BaseSettings):
        self.ignore_schemes: list[str] = settings.getlist("HTTPCACHE_IGNORE_SCHEMES")
        self.ignore_http_codes: list[int] = [
            int(x) for x in settings.getlist("HTTPCACHE_IGNORE_HTTP_CODES")
        ]

    def should_cache_request(self, request: Request) -> bool:
        return urlparse_cached(request).scheme not in self.ignore_schemes

    def should_cache_response(self, response: Response, request: Request) -> bool:
        return response.status not in self.ignore_http_codes

    def is_cached_response_fresh(
        self, cachedresponse: Response, request: Request
    ) -> bool:
        return True

    def is_cached_response_valid(
        self, cachedresponse: Response, response: Response, request: Request
    ) -> bool:
        return True


class RFC2616Policy:
    MAXAGE = 3600 * 24 * 365  # one year

    def __init__(self, settings: BaseSettings):
        self.always_store: bool = settings.getbool("HTTPCACHE_ALWAYS_STORE")
        self.ignore_schemes: list[str] = settings.getlist("HTTPCACHE_IGNORE_SCHEMES")
        self._cc_parsed: WeakKeyDictionary[
            Request | Response, dict[bytes, bytes | None]
        ] = WeakKeyDictionary()
        self.ignore_response_cache_controls: list[bytes] = [
            cc.encode()
            for cc in settings.getlist("HTTPCACHE_IGNORE_RESPONSE_CACHE_CONTROLS")
        ]

    def _parse_cachecontrol(self, r: Request | Response) -> dict[bytes, bytes | None]:
        if r not in self._cc_parsed:
            cch = r.headers.get(b"Cache-Control", b"")
            assert cch is not None
            parsed = parse_cachecontrol(cch)
            if isinstance(r, Response):
                for key in self.ignore_response_cache_controls:
                    parsed.pop(key, None)
            self._cc_parsed[r] = parsed
        return self._cc_parsed[r]

    def should_cache_request(self, request: Request) -> bool:
        if urlparse_cached(request).scheme in self.ignore_schemes:
            return False
        cc = self._parse_cachecontrol(request)
        # obey user-agent directive "Cache-Control: no-store"
        return b"no-store" not in cc

    def should_cache_response(self, response: Response, request: Request) -> bool:
        # What is cacheable - https://www.w3.org/Protocols/rfc2616/rfc2616-sec14.html#sec14.9.1
        # Response cacheability - https://www.w3.org/Protocols/rfc2616/rfc2616-sec13.html#sec13.4
        # Status code 206 is not included because cache can not deal with partial contents
        cc = self._parse_cachecontrol(response)
        # obey directive "Cache-Control: no-store"
        if b"no-store" in cc:
            return False
        # Never cache 304 (Not Modified) responses
        if response.status == 304:
            return False
        # Cache unconditionally if configured to do so
        if self.always_store:
            return True
        # Any hint on response expiration is good
        if b"max-age" in cc or b"Expires" in response.headers:
            return True
        # Firefox fallbacks this statuses to one year expiration if none is set
        if response.status in {300, 301, 308}:
            return True
        # Other statuses without expiration requires at least one validator
        if response.status in {200, 203, 401}:
            return b"Last-Modified" in response.headers or b"ETag" in response.headers
        # Any other is probably not eligible for caching
        # Makes no sense to cache responses that does not contain expiration
        # info and can not be revalidated
        return False

    def is_cached_response_fresh(
        self, cachedresponse: Response, request: Request
    ) -> bool:
        cc = self._parse_cachecontrol(cachedresponse)
        ccreq = self._parse_cachecontrol(request)
        if b"no-cache" in cc or b"no-cache" in ccreq:
            return False

        now = time()
        freshnesslifetime = self._compute_freshness_lifetime(
            cachedresponse, request, now
        )
        currentage = self._compute_current_age(cachedresponse, request, now)

        reqmaxage = self._get_max_age(ccreq)
        if reqmaxage is not None:
            freshnesslifetime = min(freshnesslifetime, reqmaxage)

        if currentage < freshnesslifetime:
            return True

        if b"max-stale" in ccreq and b"must-revalidate" not in cc:
            # From RFC2616: "Indicates that the client is willing to
            # accept a response that has exceeded its expiration time.
            # If max-stale is assigned a value, then the client is
            # willing to accept a response that has exceeded its
            # expiration time by no more than the specified number of
            # seconds. If no value is assigned to max-stale, then the
            # client is willing to accept a stale response of any age."
            staleage = ccreq[b"max-stale"]
            if staleage is None:
                return True

            try:
                if currentage < freshnesslifetime + max(0, int(staleage)):
                    return True
            except ValueError:
                pass

        # Cached response is stale, try to set validators if any
        self._set_conditional_validators(request, cachedresponse)
        return False

    def is_cached_response_valid(
        self, cachedresponse: Response, response: Response, request: Request
    ) -> bool:
        # Use the cached response if the new response is a server error,
        # as long as the old response didn't specify must-revalidate.
        if response.status >= 500:
            cc = self._parse_cachecontrol(cachedresponse)
            if b"must-revalidate" not in cc:
                return True

        # Use the cached response if the server says it hasn't changed.
        return response.status == 304

    def _set_conditional_validators(
        self, request: Request, cachedresponse: Response
    ) -> None:
        if b"Last-Modified" in cachedresponse.headers:
            request.headers[b"If-Modified-Since"] = cachedresponse.headers[
                b"Last-Modified"
            ]

        if b"ETag" in cachedresponse.headers:
            request.headers[b"If-None-Match"] = cachedresponse.headers[b"ETag"]

    def _get_max_age(self, cc: dict[bytes, bytes | None]) -> int | None:
        try:
            return max(0, int(cc[b"max-age"]))  # type: ignore[arg-type]
        except (KeyError, ValueError):
            return None

    def _compute_freshness_lifetime(
        self, response: Response, request: Request, now: float
    ) -> float:
        # Reference nsHttpResponseHead::ComputeFreshnessLifetime
        # https://dxr.mozilla.org/mozilla-central/source/netwerk/protocol/http/nsHttpResponseHead.cpp#706
        cc = self._parse_cachecontrol(response)
        maxage = self._get_max_age(cc)
        if maxage is not None:
            return maxage

        # Parse date header or synthesize it if none exists
        date = rfc1123_to_epoch(response.headers.get(b"Date")) or now

        # Try HTTP/1.0 Expires header
        if b"Expires" in response.headers:
            expires = rfc1123_to_epoch(response.headers[b"Expires"])
            # When parsing Expires header fails RFC 2616 section 14.21 says we
            # should treat this as an expiration time in the past.
            return max(0, expires - date) if expires else 0

        # Fallback to heuristic using last-modified header
        # This is not in RFC but on Firefox caching implementation
        lastmodified = rfc1123_to_epoch(response.headers.get(b"Last-Modified"))
        if lastmodified and lastmodified <= date:
            return (date - lastmodified) / 10

        # This request can be cached indefinitely
        if response.status in {300, 301, 308}:
            return self.MAXAGE

        # Insufficient information to compute freshness lifetime
        return 0

    def _compute_current_age(
        self, response: Response, request: Request, now: float
    ) -> float:
        # Reference nsHttpResponseHead::ComputeCurrentAge
        # https://dxr.mozilla.org/mozilla-central/source/netwerk/protocol/http/nsHttpResponseHead.cpp#658
        currentage: float = 0
        # If Date header is not set we assume it is a fast connection, and
        # clock is in sync with the server
        date = rfc1123_to_epoch(response.headers.get(b"Date")) or now
        if now > date:
            currentage = now - date

        if b"Age" in response.headers:
            try:
                age = int(response.headers[b"Age"])  # type: ignore[arg-type]
                currentage = max(currentage, age)
            except ValueError:
                pass

        return currentage


class DbmCacheStorage:
    def __init__(self, settings: BaseSettings):
        self.cachedir: str = data_path(settings["HTTPCACHE_DIR"], createdir=True)
        self.expiration_secs: int = settings.getint("HTTPCACHE_EXPIRATION_SECS")
        self.dbmodule: ModuleType = import_module(settings["HTTPCACHE_DBM_MODULE"])
        self.db: Any = None  # the real type is private
        self._scope = _get_scope(settings, ("spider", "none"))

    def open_spider(self, spider: Spider) -> None:
        name = spider.name if self._scope == "spider" else "httpcache"
        dbpath = Path(self.cachedir, f"{name}.db")
        self.db = self.dbmodule.open(str(dbpath), "c")

        logger.debug(
            "Using DBM cache storage in %(cachepath)s",
            {"cachepath": dbpath},
            extra={"spider": spider},
        )

        self._fingerprinter: RequestFingerprinterProtocol = (
            spider.crawler.request_fingerprinter
        )

    def close_spider(self, spider: Spider) -> None:
        self.db.close()

    def retrieve_response(self, spider: Spider, request: Request) -> Response | None:
        result = self._read_data(spider, request)
        if result is None:
            return None  # not cached
        data, ts = result
        request.meta["cache_timestamp"] = ts
        return response_from_dict(data)

    def store_response(
        self, spider: Spider, request: Request, response: Response
    ) -> None:
        key = self._fingerprinter.fingerprint(request).hex()
        self.db[f"{key}_data"] = pickle.dumps(response.to_dict(), protocol=4)
        self.db[f"{key}_time"] = str(time())

    def _read_data(
        self, spider: Spider, request: Request
    ) -> tuple[dict[str, Any], float] | None:
        key = self._fingerprinter.fingerprint(request).hex()
        db = self.db
        tkey = f"{key}_time"
        if tkey not in db:
            return None  # not found

        ts = float(db[tkey])
        if 0 < self.expiration_secs < time() - ts:
            return None  # expired

        data = cast("dict[str, Any]", pickle.loads(db[f"{key}_data"]))  # noqa: S301
        return data, ts


class FilesystemCacheStorage:
    # A cache entry is a directory named after the request fingerprint. Each
    # fully written response lives in its own generation directory inside it,
    # and the "current" pointer file names the generation that reads must use.
    # Entries written before generations existed keep their files directly in
    # the entry directory (the legacy layout) and are still read as-is.
    _POINTER_FILENAME = "current"
    _GENERATION_PREFIX = "gen-"
    _TMP_SUFFIX = ".tmp"

    def __init__(self, settings: BaseSettings):
        self.cachedir: str = data_path(settings["HTTPCACHE_DIR"])
        self.expiration_secs: int = settings.getint("HTTPCACHE_EXPIRATION_SECS")
        self.use_gzip: bool = settings.getbool("HTTPCACHE_GZIP")
        self._scope = _get_scope(settings, ("spider", "none", "domain"))
        # https://github.com/python/mypy/issues/10740
        self._open: Callable[
            Concatenate[str | os.PathLike[str], str, ...], IO[bytes]
        ] = (
            gzip.open if self.use_gzip else open  # type: ignore[assignment]
        )
        # Disambiguates generation directories created within the same second.
        self._generation_counter = 0

    def open_spider(self, spider: Spider) -> None:
        logger.debug(
            "Using filesystem cache storage in %(cachedir)s",
            {"cachedir": self.cachedir},
            extra={"spider": spider},
        )

        self._fingerprinter = spider.crawler.request_fingerprinter

    def close_spider(self, spider: Spider) -> None:
        pass

    def retrieve_response(self, spider: Spider, request: Request) -> Response | None:
        """Return response if present in cache, or None otherwise.

        Unreadable or truncated cache content is treated as a cache miss
        instead of being raised, so that one damaged entry cannot break a
        crawl. Only the damaged generation of this request is removed; other
        requests and the previous, still readable generation are untouched.
        """
        entry_dir, key = self._get_request_dir_and_key(spider, request)
        try:
            generation = self._current_generation(entry_dir)
            if generation is None:
                # No pointer: the entry is either absent or written in the
                # legacy layout, with files directly in the entry directory.
                if not (entry_dir / "pickled_meta").exists():
                    return None  # not cached
                source_dir = entry_dir
            else:
                source_dir = entry_dir / generation
                if not source_dir.is_dir():
                    raise CacheUnreadableError(
                        f"pointer names missing generation {generation!r}"
                    )
            metadata = self._load_metadata(entry_dir, key, source_dir)
            if metadata is None:
                return None  # expired
            data = self._load_response(source_dir, metadata)
        except CacheUnreadableError as exc:
            # Structural problems detected by the storage itself. An unsafe
            # pointer cannot be mapped to a generation, so nothing is removed.
            self._handle_unreadable(
                entry_dir,
                key,
                clean_up=not exc.unsafe_pointer,
            )
            return None
        except (OSError, EOFError, pickle.PickleError, ValueError, KeyError):
            self._handle_unreadable(entry_dir, key)
            return None
        request.meta["cache_timestamp"] = metadata["timestamp"]
        return response_from_dict(data)

    def store_response(
        self, spider: Spider, request: Request, response: Response
    ) -> None:
        """Store the given response in the cache.

        All files are first written into a fresh generation directory. The
        generation is published by atomically replacing the pointer file, so
        readers only ever see complete generations and never a mix of the old
        and the new metadata, headers and body. A failure while writing leaves
        the previous generation and the pointer to it in place.
        """
        entry_dir, _key = self._get_request_dir_and_key(spider, request)
        entry_dir.mkdir(parents=True, exist_ok=True)
        self._prune_tmp_generations(entry_dir)
        # The directory keeps the tmp suffix until every file is written, so a
        # reader following the pointer can never open it mid-write.
        generation_name = self._new_generation_name()
        tmp_dir = entry_dir / f"{generation_name}{self._TMP_SUFFIX}"
        generation_dir = entry_dir / generation_name
        pointer_tmp = entry_dir / f"{self._POINTER_FILENAME}{self._TMP_SUFFIX}"
        try:
            self._write_generation(tmp_dir, request, response)
            os.replace(tmp_dir, generation_dir)
            pointer_tmp.write_text(generation_name, encoding="utf-8")
            os.replace(pointer_tmp, entry_dir / self._POINTER_FILENAME)
        except BaseException:
            # Remove the unpublished generation and pointer temp file. The
            # published generation and its pointer, if any, stay readable.
            shutil.rmtree(tmp_dir, ignore_errors=True)
            shutil.rmtree(generation_dir, ignore_errors=True)
            with contextlib.suppress(OSError):
                pointer_tmp.unlink()
            raise

    def _get_request_path(self, spider: Spider, request: Request) -> str:
        path, _key = self._get_request_dir_and_key(spider, request)
        return str(path)

    def _get_request_dir_and_key(
        self, spider: Spider, request: Request
    ) -> tuple[Path, str]:
        key = self._fingerprinter.fingerprint(request).hex()
        scope_path = self._get_scope_path(spider, request)
        return Path(self.cachedir, scope_path, key[0:2], key), key

    def _get_scope_path(self, spider: Spider, request: Request) -> str:
        if self._scope == "spider":
            return spider.name
        if self._scope == "domain":
            # quote() covers hostnames that are not valid path components,
            # such as IPv6 addresses.
            return quote(urlparse_cached(request).hostname or "_", safe="")
        return ""

    def _new_generation_name(self) -> str:
        self._generation_counter += 1
        return (
            f"{self._GENERATION_PREFIX}{int(time())}-"
            f"{os.getpid()}-{self._generation_counter}"
        )

    def _write_generation(
        self, generation_dir: Path, request: Request, response: Response
    ) -> None:
        generation_dir.mkdir()
        metadata = {
            "url": request.url,
            "method": request.method,
            "status": response.status,
            "response_url": response.url,
            "timestamp": time(),
        }
        data = {
            key: value
            for key, value in response.to_dict().items()
            if key not in {"url", "status", "headers", "body"}
        }
        files = (
            ("meta", repr(metadata).encode()),
            ("pickled_meta", pickle.dumps(metadata, protocol=4)),
            ("response_headers", headers_dict_to_raw(response.headers)),
            ("response_body", response.body),
            ("response_data", pickle.dumps(data, protocol=4)),
            ("request_headers", headers_dict_to_raw(request.headers)),
            ("request_body", request.body),
        )
        with ExitStack() as stack:
            for name, content in files:
                handle = stack.enter_context(
                    self._open(generation_dir / name, "wb")
                )
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())

    def _current_generation(self, entry_dir: Path) -> str | None:
        pointer = entry_dir / self._POINTER_FILENAME
        if not pointer.exists():
            return None
        generation = pointer.read_text(encoding="utf-8").strip()
        if (
            not generation
            or generation in {os.curdir, os.pardir}
            or os.sep in generation
            or (os.altsep is not None and os.altsep in generation)
        ):
            raise CacheUnreadableError(
                f"pointer names invalid entry {generation!r}", unsafe_pointer=True
            )
        return generation

    def _load_metadata(
        self, entry_dir: Path, key: str, source_dir: Path
    ) -> dict[str, Any] | None:
        metapath = source_dir / "pickled_meta"
        if not metapath.exists():
            raise CacheUnreadableError("pickled_meta is missing")
        mtime = metapath.stat().st_mtime
        if 0 < self.expiration_secs < time() - mtime:
            logger.debug(
                f"Cache entry for request key {key} in {entry_dir} has "
                "expired, treating it as a cache miss."
            )
            return None  # expired
        with self._open(metapath, "rb") as f:
            metadata = cast("dict[str, Any]", pickle.load(f))  # noqa: S301
        if not isinstance(metadata, dict):
            raise CacheUnreadableError("pickled_meta does not contain a dict")
        return metadata

    def _load_response(
        self, source_dir: Path, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        with self._open(source_dir / "response_body", "rb") as f:
            body = f.read()
        with self._open(source_dir / "response_headers", "rb") as f:
            rawheaders = f.read()
        data = {
            "url": metadata["response_url"],
            "status": metadata["status"],
            "headers": headers_raw_to_dict(rawheaders),
            "body": body,
        }
        datapath = source_dir / "response_data"
        if datapath.exists():
            with self._open(datapath, "rb") as f:
                extra = pickle.load(f)  # noqa: S301
            if not isinstance(extra, dict):
                raise CacheUnreadableError("response_data does not contain a dict")
            data.update(extra)
        return data

    def _handle_unreadable(
        self, entry_dir: Path, key: str, *, clean_up: bool = True
    ) -> None:
        logger.warning(
            f"Could not read the cache entry for request key {key} in "
            f"{entry_dir}, treating it as a cache miss.",
            exc_info=True,
        )
        if not clean_up:
            return
        # Only the generation this entry's pointer names is damaged. Legacy
        # entries and the directories of other requests are never removed.
        pointer = entry_dir / self._POINTER_FILENAME
        if not pointer.exists():
            return
        try:
            generation = pointer.read_text(encoding="utf-8").strip()
            if generation and os.sep not in generation:
                shutil.rmtree(entry_dir / generation, ignore_errors=True)
            pointer.unlink()
        except OSError:
            logger.warning(
                f"Could not remove the damaged cache generation for request "
                f"key {key}.",
                exc_info=True,
            )

    def _prune_tmp_generations(self, entry_dir: Path) -> None:
        """Remove generation directories left by interrupted writes."""
        try:
            children = list(entry_dir.iterdir())
        except OSError:
            return
        for child in children:
            if child.name.startswith(self._GENERATION_PREFIX) and child.name.endswith(
                self._TMP_SUFFIX
            ):
                shutil.rmtree(child, ignore_errors=True)


class CacheUnreadableError(Exception):
    """A cache generation on disk is missing or damaged.

    *unsafe_pointer* marks a pointer whose content cannot be mapped to a
    generation directory safely. The pointer and every generation are then
    left on disk instead of removing anything.
    """

    def __init__(self, message: str, *, unsafe_pointer: bool = False) -> None:
        super().__init__(message)
        self.unsafe_pointer = unsafe_pointer


def parse_cachecontrol(header: bytes) -> dict[bytes, bytes | None]:
    """Parse Cache-Control header

    https://www.w3.org/Protocols/rfc2616/rfc2616-sec14.html#sec14.9

    >>> parse_cachecontrol(b'public, max-age=3600') == {b'public': None,
    ...                                                 b'max-age': b'3600'}
    True
    >>> parse_cachecontrol(b'') == {}
    True

    """
    directives = {}
    for directive in header.split(b","):
        key, sep, val = directive.strip().partition(b"=")
        if key:
            directives[key.lower()] = val if sep else None
    return directives


def rfc1123_to_epoch(date_str: str | bytes | None) -> int | None:
    # The HTTP time format (which is how we use this function) is actually in
    # RFC 7231 §7.1.1.1, it refers to RFC 5322 and also requires support for
    # two obsolete formats (RFC 850 and asctime()). email.utils evidently
    # supports all three.
    if date_str is None:
        return None
    try:
        date_str = to_unicode(date_str, encoding="ascii")
        date = parsedate_to_datetime(date_str)
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        return int(date.timestamp())
    except Exception:
        return None
