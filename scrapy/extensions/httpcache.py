from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import pickle
import shutil
import sys
from datetime import UTC
from email.utils import parsedate_to_datetime
from importlib import import_module
from pathlib import Path
from time import time
from typing import IO, TYPE_CHECKING, Any, Concatenate, cast
from urllib.parse import quote
from weakref import WeakKeyDictionary

from w3lib.http import headers_dict_to_raw, headers_raw_to_dict

from scrapy.http import Headers, Response
from scrapy.utils.httpobj import urlparse_cached
from scrapy.utils.project import data_path
from scrapy.utils.python import to_unicode
from scrapy.utils.response import response_from_dict

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    from scrapy.http.request import Request
    from scrapy.settings import BaseSettings
    from scrapy.spiders import Spider
    from scrapy.utils.request import RequestFingerprinterProtocol


logger = logging.getLogger(__name__)

# Files whose checksum is recorded in a generation manifest.
_GENERATION_FILES: frozenset[str] = frozenset(
    {
        "meta",
        "pickled_meta",
        "request_body",
        "request_headers",
        "response_body",
        "response_data",
        "response_headers",
    }
)
# Request headers carrying credentials. They are redacted before a cache entry
# is written in generation mode so that no plaintext secret reaches the disk.
_GENERATION_REDACTED_HEADERS: tuple[bytes, ...] = (
    b"Authorization",
    b"Proxy-Authorization",
)
_GENERATION_MANIFEST_VERSION = 1
_GENERATION_POINTER_VERSION = 1


class GenerationCacheError(Exception):
    """Base class for errors of the generation-publishing mode of
    :class:`FilesystemCacheStorage`."""


class GenerationPublishError(GenerationCacheError):
    """Raised when a cache generation could not be built or published.

    The previously published generation, if any, is left readable and the
    temporary files of the failed attempt are removed."""


class GenerationUncertainError(GenerationCacheError):
    """Raised when the pointer to the current generation was switched but the
    cache directory could not be synced afterwards.

    The new generation may or may not be visible to a reader after a crash, so
    it is no longer safe to claim that the previous generation is unchanged;
    the caller must report this state rather than retry blindly."""


class GenerationCorruptError(GenerationCacheError):
    """Raised when the published generation is missing files or fails its
    recorded size or digest checks, so that the entry cannot be trusted."""


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
    def __init__(self, settings: BaseSettings):
        self.cachedir: str = data_path(settings["HTTPCACHE_DIR"])
        self.expiration_secs: int = settings.getint("HTTPCACHE_EXPIRATION_SECS")
        self.use_gzip: bool = settings.getbool("HTTPCACHE_GZIP")
        self.generation_mode: bool = settings.getbool("HTTPCACHE_GENERATION")
        self.read_legacy: bool = settings.getbool("HTTPCACHE_GENERATION_READ_LEGACY")
        self.use_fsync: bool = settings.getbool("HTTPCACHE_FSYNC")
        self._scope = _get_scope(settings, ("spider", "none", "domain"))
        # https://github.com/python/mypy/issues/10740
        self._open: Callable[
            Concatenate[str | os.PathLike[str], str, ...], IO[bytes]
        ] = (
            gzip.open if self.use_gzip else open  # type: ignore[assignment]
        )
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
        """Return response if present in cache, or None otherwise."""
        if self.generation_mode:
            return self._retrieve_generation(spider, request)
        return self._retrieve_legacy(spider, request)

    def store_response(
        self, spider: Spider, request: Request, response: Response
    ) -> None:
        """Store the given response in the cache."""
        if self.generation_mode:
            self._store_generation(spider, request, response)
            return
        self._store_legacy(spider, request, response)

    def _get_request_path(self, spider: Spider, request: Request) -> str:
        key = self._fingerprinter.fingerprint(request).hex()
        scope_path = self._get_scope_path(spider, request)
        return str(Path(self.cachedir, scope_path, key[0:2], key))

    def _get_scope_path(self, spider: Spider, request: Request) -> str:
        if self._scope == "spider":
            return spider.name
        if self._scope == "domain":
            # quote() covers hostnames that are not valid path components,
            # such as IPv6 addresses.
            return quote(urlparse_cached(request).hostname or "_", safe="")
        return ""

    # -- Legacy layout -------------------------------------------------------

    def _retrieve_legacy(self, spider: Spider, request: Request) -> Response | None:
        metadata = self._read_meta_legacy(spider, request)
        if metadata is None:
            return None  # not cached
        rpath = Path(self._get_request_path(spider, request))
        with self._open(rpath / "response_body", "rb") as f:
            body = f.read()
        with self._open(rpath / "response_headers", "rb") as f:
            rawheaders = f.read()
        data = {
            "url": metadata["response_url"],
            "status": metadata["status"],
            "headers": headers_raw_to_dict(rawheaders),
            "body": body,
        }
        datapath = rpath / "response_data"
        if datapath.exists():
            with self._open(datapath, "rb") as f:
                data.update(pickle.load(f))  # noqa: S301
        request.meta["cache_timestamp"] = metadata["timestamp"]
        return response_from_dict(data)

    def _store_legacy(
        self, spider: Spider, request: Request, response: Response
    ) -> None:
        rpath = Path(self._get_request_path(spider, request))
        if not rpath.exists():
            rpath.mkdir(parents=True)
        metadata = {
            "url": request.url,
            "method": request.method,
            "status": response.status,
            "response_url": response.url,
            "timestamp": time(),
        }
        with self._open(rpath / "meta", "wb") as f:
            f.write(repr(metadata).encode())
        with self._open(rpath / "pickled_meta", "wb") as f:
            pickle.dump(metadata, f, protocol=4)
        with self._open(rpath / "response_headers", "wb") as f:
            f.write(headers_dict_to_raw(response.headers))
        with self._open(rpath / "response_body", "wb") as f:
            f.write(response.body)
        data = {
            key: value
            for key, value in response.to_dict().items()
            if key not in {"url", "status", "headers", "body"}
        }
        with self._open(rpath / "response_data", "wb") as f:
            pickle.dump(data, f, protocol=4)
        with self._open(rpath / "request_headers", "wb") as f:
            f.write(headers_dict_to_raw(request.headers))
        with self._open(rpath / "request_body", "wb") as f:
            f.write(request.body)

    def _read_meta_legacy(
        self, spider: Spider, request: Request
    ) -> dict[str, Any] | None:
        rpath = Path(self._get_request_path(spider, request))
        metapath = rpath / "pickled_meta"
        if not metapath.exists():
            return None  # not found
        mtime = metapath.stat().st_mtime
        if 0 < self.expiration_secs < time() - mtime:
            return None  # expired
        with self._open(metapath, "rb") as f:
            return cast("dict[str, Any]", pickle.load(f))  # noqa: S301

    # -- Generation-publishing layout ---------------------------------------

    def _entry_dir(self, spider: Spider, request: Request) -> Path:
        return Path(self._get_request_path(spider, request))

    @staticmethod
    def _generation_dir(entry_dir: Path, generation: int) -> Path:
        return entry_dir / f"gen-{generation}"

    @staticmethod
    def _staging_dir(entry_dir: Path, generation: int, counter: int) -> Path:
        return entry_dir / f".staging-{generation}-{counter}-{os.getpid()}"

    @staticmethod
    def _pointer_path(entry_dir: Path) -> Path:
        return entry_dir / "current.json"

    def _read_pointer(self, entry_dir: Path) -> dict[str, Any] | None:
        pointer_path = self._pointer_path(entry_dir)
        if not pointer_path.exists():
            return None  # no published generation
        try:
            pointer = json.loads(pointer_path.read_bytes().decode("utf-8"))
            if int(pointer["version"]) != _GENERATION_POINTER_VERSION:
                raise ValueError("unknown pointer version")
            generation = int(pointer["generation"])
            if generation < 0:
                raise ValueError("negative generation")
            manifest_digest = str(pointer["manifest_digest"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise GenerationCorruptError(
                f"Unreadable generation pointer at {pointer_path}"
            ) from exc
        # Generation ids are non-negative integers, so the generation directory
        # can never contain a path separator or escape the cache entry.
        generation_path = self._generation_dir(entry_dir, generation)
        self._ensure_within(entry_dir.resolve(), generation_path.resolve())
        return {"generation": generation, "manifest_digest": manifest_digest}

    def _next_generation(self, entry_dir: Path) -> int:
        # Consider both the pointer and generation directories left behind by a
        # crashed run, so a recovered entry never reuses an existing id.
        highest = -1
        pointer = self._read_pointer(entry_dir)
        if pointer is not None:
            highest = int(pointer["generation"])
        if entry_dir.is_dir():
            for child in entry_dir.iterdir():
                if not child.name.startswith("gen-"):
                    continue
                try:
                    highest = max(highest, int(child.name[len("gen-") :]))
                except ValueError:
                    continue
        return highest + 1

    def _cleanup_staging(self, entry_dir: Path) -> None:
        if not entry_dir.is_dir():
            return
        for child in entry_dir.iterdir():
            if child.name.startswith(".staging-") and child.is_dir():
                self._remove_tree(child)

    def _store_generation(
        self, spider: Spider, request: Request, response: Response
    ) -> None:
        entry_dir = self._entry_dir(spider, request)
        self._cleanup_staging(entry_dir)
        old_pointer = self._read_pointer(entry_dir)
        generation = self._next_generation(entry_dir)
        self._generation_counter += 1
        staging = self._staging_dir(entry_dir, generation, self._generation_counter)
        old_generation_dir = (
            self._generation_dir(entry_dir, int(old_pointer["generation"]))
            if old_pointer is not None
            else None
        )
        generation_path = self._generation_dir(entry_dir, generation)
        try:
            staging.mkdir(parents=True, exist_ok=False)
            manifest = self._build_generation_manifest(
                staging, generation, request, response
            )
            manifest_bytes = self._dump_manifest(manifest)
            self._write_manifest(staging / "manifest.json", manifest_bytes)
            self._verify_generation(staging, manifest, manifest_bytes)
            self._fsync_dir(staging)
            self._ensure_within(entry_dir.resolve(), generation_path.resolve())
            try:
                staging.replace(generation_path)
            except OSError as exc:
                raise GenerationPublishError(
                    f"Could not publish generation {generation} in {entry_dir}"
                ) from exc
            # The new generation now exists under its final name but the
            # pointer still names the previous one. A failure before the
            # pointer switch leaves the new directory unreferenced, so it is
            # removed below; once the switch succeeds it must stay, even if
            # the directory sync then reports an uncertain state.
            self._publish_pointer(
                entry_dir, generation, hashlib.sha256(manifest_bytes).hexdigest()
            )
        except GenerationUncertainError:
            self._remove_tree(staging)
            raise
        except GenerationCacheError:
            self._remove_tree(staging)
            self._remove_tree(generation_path)
            raise
        except OSError as exc:
            self._remove_tree(staging)
            self._remove_tree(generation_path)
            raise GenerationPublishError(
                f"Could not write generation {generation} in {entry_dir}"
            ) from exc
        if old_generation_dir is not None:
            try:
                self._remove_tree(old_generation_dir)
            except OSError:
                logger.warning(
                    f"Could not remove the previous cache generation in "
                    f"{old_generation_dir}; it is no longer referenced and can "
                    f"be deleted manually.",
                    extra={"spider": spider},
                )

    def _build_generation_manifest(
        self,
        staging: Path,
        generation: int,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        metadata = {
            "url": request.url,
            "method": request.method,
            "status": response.status,
            "response_url": response.url,
            "timestamp": time(),
        }
        redacted_headers = Headers(request.headers)
        for header in _GENERATION_REDACTED_HEADERS:
            redacted_headers.pop(header, None)
        data = {
            key: value
            for key, value in response.to_dict().items()
            if key not in {"url", "status", "headers", "body"}
        }
        payloads = {
            "meta": repr(metadata).encode(),
            "pickled_meta": pickle.dumps(metadata, protocol=4),
            "request_body": request.body,
            "request_headers": headers_dict_to_raw(redacted_headers),
            "response_body": response.body,
            "response_data": pickle.dumps(data, protocol=4),
            "response_headers": headers_dict_to_raw(response.headers),
        }
        files: dict[str, dict[str, int | str]] = {}
        for name in _GENERATION_FILES:
            content = payloads[name]
            path = staging / name
            self._write_generation_file(path, content)
            files[name] = {
                # Size of the file on disk, which is the gzip stream length
                # when HTTPCACHE_GZIP is enabled; the digest always covers the
                # uncompressed payload so that it can be checked after reading.
                "size": path.stat().st_size,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        return {
            "version": _GENERATION_MANIFEST_VERSION,
            "generation": generation,
            "gzip": self.use_gzip,
            "metadata": metadata,
            "files": files,
        }

    @staticmethod
    def _dump_manifest(manifest: dict[str, Any]) -> bytes:
        return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )

    def _write_generation_file(self, path: Path, content: bytes) -> None:
        with self._open(path, "wb") as f:
            f.write(content)
            if self.use_fsync:
                f.flush()
                os.fsync(f.fileno())

    def _write_manifest(self, path: Path, content: bytes) -> None:
        # The manifest itself is always plain JSON, even when the payload files
        # are gzip-compressed, so that readers do not need a working gzip
        # stream to find out what the generation is supposed to contain.
        with path.open("wb") as f:
            f.write(content)
            if self.use_fsync:
                f.flush()
                os.fsync(f.fileno())

    def _publish_pointer(
        self, entry_dir: Path, generation: int, manifest_digest: str
    ) -> None:
        pointer_bytes = self._dump_manifest(
            {
                "version": _GENERATION_POINTER_VERSION,
                "generation": generation,
                "manifest_digest": manifest_digest,
            }
        )
        pointer_path = self._pointer_path(entry_dir)
        temp_path = entry_dir / f".current-{generation}-{os.getpid()}.tmp"
        try:
            with temp_path.open("wb") as f:
                f.write(pointer_bytes)
                if self.use_fsync:
                    f.flush()
                    os.fsync(f.fileno())
            try:
                temp_path.replace(pointer_path)
            except OSError as exc:
                raise GenerationPublishError(
                    f"Could not switch the generation pointer in {entry_dir}"
                ) from exc
            try:
                self._fsync_dir(entry_dir)
            except OSError as exc:
                raise GenerationUncertainError(
                    f"The generation pointer in {entry_dir} was switched to "
                    f"generation {generation}, but the directory could not be "
                    f"synced; which generation a reader sees after a crash is "
                    f"unknown."
                ) from exc
        finally:
            self._remove_tree(temp_path)

    def _verify_generation(
        self,
        generation_path: Path,
        manifest: dict[str, Any],
        manifest_bytes: bytes,
    ) -> None:
        try:
            files = manifest["files"]
            if not isinstance(files, dict) or set(files) != _GENERATION_FILES:
                raise ValueError("the manifest does not list exactly the payload files")
            for name in sorted(_GENERATION_FILES):
                record = files[name]
                if not isinstance(record, dict):
                    raise ValueError(f"invalid manifest record for {name}")
                path = self._manifest_path(generation_path, name)
                if path.stat().st_size != int(record["size"]):
                    raise ValueError(f"size mismatch for {name}")
                with self._open(path, "rb") as f:
                    content = f.read()
                if hashlib.sha256(content).hexdigest() != str(record["sha256"]):
                    raise ValueError(f"digest mismatch for {name}")
            if (generation_path / "manifest.json").read_bytes() != manifest_bytes:
                raise ValueError(
                    "the written manifest does not match the in-memory manifest"
                )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise GenerationPublishError(
                f"Verification of the cache generation in {generation_path} failed"
            ) from exc

    def _manifest_path(self, generation_path: Path, name: str) -> Path:
        if name not in _GENERATION_FILES:
            raise GenerationCorruptError(
                f"The manifest names a file outside the cache entry: {name!r}"
            )
        path = (generation_path / name).resolve()
        self._ensure_within(generation_path.resolve(), path)
        return path

    @staticmethod
    def _is_within(directory: Path, path: Path) -> bool:
        try:
            path.relative_to(directory)
        except ValueError:
            return False
        return True

    def _ensure_within(self, directory: Path, path: Path) -> None:
        if not self._is_within(directory, path):
            raise GenerationCorruptError(
                f"The cache entry references {path}, which is outside {directory}"
            )

    def _retrieve_generation(self, spider: Spider, request: Request) -> Response | None:
        entry_dir = self._entry_dir(spider, request)
        pointer = self._read_pointer(entry_dir)
        if pointer is None:
            if self.read_legacy:
                return self._retrieve_legacy(spider, request)
            return None  # no published generation
        generation_path = self._generation_dir(entry_dir, int(pointer["generation"]))
        self._ensure_within(entry_dir.resolve(), generation_path.resolve())
        if not generation_path.is_dir():
            raise GenerationCorruptError(
                f"The pointer in {entry_dir} references missing generation "
                f"{pointer['generation']}"
            )
        manifest = self._load_manifest(generation_path, str(pointer["manifest_digest"]))
        metadata = self._read_meta_generation(generation_path, manifest)
        if metadata is None:
            return None  # expired
        open_payload = self._payload_opener(bool(manifest["gzip"]))
        body = self._read_payload(
            generation_path, manifest, open_payload, "response_body"
        )
        rawheaders = self._read_payload(
            generation_path, manifest, open_payload, "response_headers"
        )
        data = {
            "url": metadata["response_url"],
            "status": metadata["status"],
            "headers": headers_raw_to_dict(rawheaders),
            "body": body,
        }
        raw_data = self._read_payload(
            generation_path, manifest, open_payload, "response_data"
        )
        data.update(pickle.loads(raw_data))  # noqa: S301
        request.meta["cache_timestamp"] = metadata["timestamp"]
        return response_from_dict(data)

    @staticmethod
    def _payload_opener(
        use_gzip: bool,
    ) -> Callable[Concatenate[str | os.PathLike[str], str, ...], IO[bytes]]:
        return gzip.open if use_gzip else open  # type: ignore[return-value]

    def _load_manifest(
        self, generation_path: Path, expected_digest: str
    ) -> dict[str, Any]:
        manifest_path = generation_path / "manifest.json"
        self._ensure_within(generation_path.resolve(), manifest_path.resolve())
        try:
            manifest_bytes = manifest_path.read_bytes()
        except OSError as exc:
            raise GenerationCorruptError(
                f"Missing manifest in cache generation {generation_path}"
            ) from exc
        if hashlib.sha256(manifest_bytes).hexdigest() != expected_digest:
            raise GenerationCorruptError(
                f"The manifest of cache generation {generation_path} does not "
                f"match the generation pointer"
            )
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
            if int(manifest["version"]) != _GENERATION_MANIFEST_VERSION:
                raise ValueError("unknown manifest version")
            if not isinstance(manifest["files"], dict):
                raise ValueError("invalid file list")
            if not isinstance(manifest["metadata"], dict):
                raise ValueError("invalid metadata")
            if not isinstance(manifest["gzip"], bool):
                raise ValueError("invalid gzip flag")
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise GenerationCorruptError(
                f"Unreadable manifest in cache generation {generation_path}"
            ) from exc
        try:
            for name, record in manifest["files"].items():
                path = self._manifest_path(generation_path, str(name))
                if not isinstance(record, dict) or not path.is_file():
                    raise ValueError(f"invalid or missing payload {name}")
                if path.stat().st_size != int(record["size"]):
                    raise ValueError(f"size mismatch for {name}")
                if not isinstance(record["sha256"], str):
                    raise ValueError(f"invalid digest for {name}")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise GenerationCorruptError(
                f"Invalid manifest payload list in cache generation {generation_path}"
            ) from exc
        return cast("dict[str, Any]", manifest)

    def _read_payload(
        self,
        generation_path: Path,
        manifest: dict[str, Any],
        open_payload: Callable[
            Concatenate[str | os.PathLike[str], str, ...], IO[bytes]
        ],
        name: str,
    ) -> bytes:
        path = self._manifest_path(generation_path, name)
        with open_payload(path, "rb") as f:
            content = f.read()
        record = manifest["files"][name]
        if hashlib.sha256(content).hexdigest() != str(record["sha256"]):
            raise GenerationCorruptError(
                f"Digest mismatch for {name} in cache generation {generation_path}"
            )
        return content

    def _read_meta_generation(
        self, generation_path: Path, manifest: dict[str, Any]
    ) -> dict[str, Any] | None:
        if 0 < self.expiration_secs:
            mtime = (generation_path / "manifest.json").stat().st_mtime
            if self.expiration_secs < time() - mtime:
                return None  # expired
        try:
            metadata = manifest["metadata"]
            return {
                "url": str(metadata["url"]),
                "method": str(metadata["method"]),
                "status": int(metadata["status"]),
                "response_url": str(metadata["response_url"]),
                "timestamp": float(metadata["timestamp"]),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise GenerationCorruptError(
                f"Invalid metadata in cache generation {generation_path}"
            ) from exc

    def _fsync_dir(self, path: Path) -> None:
        if not self.use_fsync or sys.platform == "win32":
            # Windows cannot open directories for fsync; its file system
            # journal already makes renames atomic and durable.
            return
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _remove_tree(path: Path) -> None:
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


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
