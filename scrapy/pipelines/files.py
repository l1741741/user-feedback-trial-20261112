"""
Files Pipeline

See documentation in topics/media-pipeline.rst
"""

from __future__ import annotations

import asyncio
import base64
import functools
import glob
import hashlib
import logging
import mimetypes
import os
import threading
import time
import warnings
from collections import defaultdict
from contextlib import suppress
from datetime import UTC, datetime
from ftplib import FTP
from io import BytesIO
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    NoReturn,
    Protocol,
    Self,
    TypedDict,
    cast,
)
from urllib.parse import urlparse

from itemadapter import ItemAdapter

from scrapy.exceptions import IgnoreRequest, NotConfigured, ScrapyDeprecationWarning
from scrapy.http import Request, Response
from scrapy.http.request import NO_CALLBACK
from scrapy.pipelines.media import FileException as _FileException
from scrapy.pipelines.media import (
    FileInfo,
    FileInfoOrError,
    MediaPipeline,
    _MediaRequestFiltered,
)
from scrapy.utils._datatypes import CaseInsensitiveDict
from scrapy.utils._ftp import ftp_store_file
from scrapy.utils.asyncio import is_asyncio_available, run_in_thread
from scrapy.utils.boto import (
    _get_max_pool_connections,
    is_aiobotocore_available,
    is_botocore_available,
)
from scrapy.utils.decorators import _warn_spider_arg
from scrapy.utils.defer import _maybeDeferred_coro, deferred_from_coro, ensure_awaitable
from scrapy.utils.httpobj import urlparse_cached
from scrapy.utils.log import failure_to_exc_info
from scrapy.utils.misc import load_object
from scrapy.utils.python import without_none_values
from scrapy.utils.request import referer_str

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from os import PathLike

    from twisted.internet.defer import Deferred
    from twisted.python.failure import Failure

    from scrapy import Spider
    from scrapy.crawler import Crawler
    from scrapy.settings import BaseSettings


logger = logging.getLogger(__name__)


def _to_string(path: str | PathLike[str]) -> str:
    return str(path)  # convert a Path object to string


def _checksum_algorithm(settings: BaseSettings, key: str) -> str:
    algorithm: str = settings.get(key, "md5")
    hashlib.new(algorithm)  # fail early on an unsupported algorithm
    return algorithm


class StatInfo(TypedDict, total=False):
    checksum: str
    last_modified: float


class FilesStoreProtocol(Protocol):
    checksum_algorithm: str
    """Name of the :mod:`hashlib` algorithm that :meth:`stat_file` must use for
    the checksums that it calculates itself.

    It is assigned by the pipeline.
    """

    def __init__(self, uri: str, /): ...

    def persist_file(
        self,
        path: str,
        buf: BytesIO,
        info: MediaPipeline.SpiderInfo,
        meta: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Deferred[Any] | None: ...

    def stat_file(
        self, path: str, info: MediaPipeline.SpiderInfo
    ) -> StatInfo | Deferred[StatInfo]: ...


class FSFilesStore:
    checksum_algorithm: str = "md5"

    def __init__(
        self,
        basedir: str | PathLike[str],
        *,
        safe_write: bool = False,
    ):
        basedir = _to_string(basedir)
        if "://" in basedir:
            basedir = basedir.split("://", 1)[1]
        self.basedir: str = basedir
        self.safe_write: bool = safe_write
        self.created_directories: defaultdict[MediaPipeline.SpiderInfo, set[str]] = (
            defaultdict(set)
        )
        self._mkdir(Path(self.basedir))
        # One lock per target path serializes concurrent safe writes to the
        # same file, so the committed file always contains one whole write.
        self._write_locks_guard = threading.Lock()
        self._write_locks: dict[str, threading.Lock] = {}
        self._tmp_counter = 0

    @classmethod
    def from_crawler(
        cls, crawler: Crawler, uri: str, *, get_setting_name: Callable[[str], str]
    ) -> Self:
        return cls(
            uri,
            safe_write=crawler.settings.getbool(get_setting_name("SAFE_WRITE")),
        )

    def persist_file(
        self,
        path: str | PathLike[str],
        buf: BytesIO,
        info: MediaPipeline.SpiderInfo,
        meta: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        absolute_path = self._get_filesystem_path(path)
        if self.safe_write:
            self._safe_persist_file(path, absolute_path, buf)
            return
        self._mkdir(absolute_path.parent, info)
        absolute_path.write_bytes(buf.getvalue())

    def _safe_persist_file(
        self, path: str | PathLike[str], absolute_path: Path, buf: BytesIO
    ) -> None:
        target = self._safe_path(path, absolute_path)
        self._ensure_directory(target.parent)
        data = buf.getvalue()
        with self._lock_for(str(target)):
            self._discard_stale_temps(target)
            self._tmp_counter += 1
            tmp_path = target.with_name(
                f".{target.name}.{os.getpid()}.{self._tmp_counter}.tmp"
            )
            self._atomic_write(target, data, tmp_path)

    @staticmethod
    def _ensure_directory(dirname: Path) -> None:
        # exist_ok=True also covers the case where another process creates
        # the directory before this call runs.
        dirname.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _discard_stale_temps(target: Path) -> None:
        """Remove temporary files a crashed process left for *target*.

        Such files never replaced the target, so they hold no committed data.
        A temporary file name embeds the PID of its writer, and a PID belongs
        to at most one live process, so only files written by the current
        process or by a process that no longer exists are removed. Files of
        other live processes are left untouched even though the per-target
        lock does not span processes. The glob pattern is escaped so
        metacharacters in a custom file name are matched literally.
        """
        literal_prefix = f".{target.name}."
        pattern = f"{glob.escape(literal_prefix)}*.tmp"
        current_pid = os.getpid()
        for candidate in target.parent.glob(pattern):
            if not FSFilesStore._temp_pid_is_reclaimable(
                candidate.name, literal_prefix, current_pid
            ):
                continue
            with suppress(FileNotFoundError, OSError):
                candidate.unlink()

    @staticmethod
    def _temp_pid_is_reclaimable(
        name: str, literal_prefix: str, current_pid: int
    ) -> bool:
        if not name.startswith(literal_prefix):
            return False
        pid_text = name[len(literal_prefix) :].split(".", 1)[0]
        if not pid_text.isdigit():
            return False
        pid = int(pid_text)
        if pid == current_pid:
            # Same-process writes to this target are serialized by the lock,
            # so a matching file here cannot be a live temporary file.
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except (PermissionError, OSError):
            # The process still exists, or the platform has no signal-0
            # liveness check; leave the file for its owner or a later run.
            return False
        return False

    def _open_temp(self, tmp_path: Path) -> tuple[int, Path]:
        """Open an exclusive new temporary file for *tmp_path*.

        Returns the file descriptor and the path it was opened at. O_EXCL
        guarantees no other live writer created the same name. A name a
        crashed process left behind is unlinked first; if another process
        raced ahead and recreated it, a uniquely named sibling is used
        instead of touching that writer's file. Either way the returned path
        ends in the temporary-file suffix, so it is reclaimed by the stale
        sweep after a crash.
        """
        try:
            fd = os.open(
                _to_string(tmp_path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o644,
            )
        except FileExistsError:
            with suppress(FileNotFoundError):
                tmp_path.unlink()
            try:
                fd = os.open(
                    _to_string(tmp_path),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o644,
                )
            except FileExistsError:
                tmp_path = tmp_path.with_name(
                    f"{tmp_path.name}.{os.getpid()}."
                    f"{threading.get_ident()}.{self._tmp_counter}.tmp"
                )
                fd = os.open(
                    _to_string(tmp_path),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o644,
                )
        return fd, tmp_path

    def _atomic_write(self, target: Path, data: bytes, tmp_path: Path) -> None:
        """Write *data* to a temporary file and atomically replace *target*.

        The destination file is created from the complete buffer in a
        uniquely named temporary file, flushed and fsynced, and only renamed
        onto *target* once its on-disk length matches the buffer. A failure
        before the rename leaves any previously committed file untouched and
        removes the temporary file.
        """
        fd, actual_tmp = self._open_temp(tmp_path)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written == 0:
                    raise OSError("os.write() wrote 0 bytes")
                view = view[written:]
            os.fsync(fd)
            if os.fstat(fd).st_size != len(data):
                raise OSError("temporary file length does not match the buffer")
            os.close(fd)
        except BaseException:
            with suppress(OSError):
                os.close(fd)
            with suppress(FileNotFoundError):
                actual_tmp.unlink()
            raise
        try:
            actual_tmp.replace(target)
            self._fsync_directory(target.parent)
        except BaseException:
            with suppress(FileNotFoundError):
                actual_tmp.unlink()
            raise

    @staticmethod
    def _fsync_directory(dirname: Path) -> None:
        # Persisting the rename requires fsyncing the directory, which has no
        # file descriptor on Windows; there an fsync of the file itself is the
        # best durability guarantee available.
        try:
            dir_fd = os.open(_to_string(dirname), os.O_RDONLY | os.O_DIRECTORY)
        except (AttributeError, OSError):  # pragma: no cover - non-Unix
            return
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _lock_for(self, key: str) -> threading.Lock:
        with self._write_locks_guard:
            lock = self._write_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._write_locks[key] = lock
            return lock

    def stat_file(
        self, path: str | PathLike[str], info: MediaPipeline.SpiderInfo
    ) -> StatInfo:
        absolute_path = self._get_filesystem_path(path)
        if self.safe_write:
            absolute_path = self._safe_path(path, absolute_path)
        try:
            last_modified = absolute_path.stat().st_mtime
        except OSError:
            return {}

        with absolute_path.open("rb") as f:
            checksum = hashlib.file_digest(f, self.checksum_algorithm).hexdigest()

        return {"last_modified": last_modified, "checksum": checksum}

    def _safe_path(self, path: str | PathLike[str], absolute_path: Path) -> Path:
        """Resolve *absolute_path* and reject locations outside the store root.

        Absolute components, parent-directory traversal and symbolic links that
        point outside of the store root would otherwise let a storage path read
        or replace files that do not belong to the store.
        """
        path_text = _to_string(path)
        root = Path(self.basedir).resolve(strict=True)
        if Path(path_text).is_absolute() or ".." in path_text.split("/"):
            raise ValueError(
                f"Storage path {path_text!s} resolves outside of the storage "
                f"root {root!s}"
            )
        target = absolute_path.resolve()
        if target != root and root not in target.parents:
            raise ValueError(
                f"Storage path {path_text!s} resolves outside of the storage "
                f"root {root!s}"
            )
        return target

    def _get_filesystem_path(self, path: str | PathLike[str]) -> Path:
        path_comps = _to_string(path).split("/")
        return Path(self.basedir, *path_comps)

    def _mkdir(
        self, dirname: Path, domain: MediaPipeline.SpiderInfo | None = None
    ) -> None:
        seen: set[str] = self.created_directories[domain] if domain else set()
        if str(dirname) not in seen:
            if not dirname.exists():
                dirname.mkdir(parents=True)
            seen.add(str(dirname))


class S3FilesStore:
    # Amazon S3 cannot hash a stored object on demand. It reports the ETag,
    # which is an MD5 hash for the objects that persist_file() uploads, and the
    # checksums that were requested when the object was uploaded.
    checksum_algorithm: str = "md5"

    AWS_ACCESS_KEY_ID = None
    AWS_SECRET_ACCESS_KEY = None
    AWS_SESSION_TOKEN = None
    AWS_ENDPOINT_URL = None
    AWS_REGION_NAME = None
    AWS_USE_SSL = None
    AWS_VERIFY = None
    # None means the botocore default
    AWS_MAX_POOL_CONNECTIONS: int | None = None
    UPLOAD_TIMEOUT: float | None = None

    POLICY = "private"
    HEADERS: ClassVar[dict[str, str]] = {
        "Cache-Control": "max-age=172800",
    }

    @classmethod
    def from_crawler(
        cls, crawler: Crawler, uri: str, *, get_setting_name: Callable[[str], str]
    ) -> Self:
        return cls(uri, **cls._get_config(crawler, get_setting_name))

    @staticmethod
    def _get_config(
        crawler: Crawler, get_setting_name: Callable[[str], str]
    ) -> dict[str, Any]:
        settings = crawler.settings
        return {
            "AWS_ACCESS_KEY_ID": settings["AWS_ACCESS_KEY_ID"],
            "AWS_SECRET_ACCESS_KEY": settings["AWS_SECRET_ACCESS_KEY"],
            "AWS_SESSION_TOKEN": settings["AWS_SESSION_TOKEN"],
            "AWS_ENDPOINT_URL": settings["AWS_ENDPOINT_URL"],
            "AWS_REGION_NAME": settings["AWS_REGION_NAME"],
            "AWS_USE_SSL": settings["AWS_USE_SSL"],
            "AWS_VERIFY": settings["AWS_VERIFY"],
            "AWS_MAX_POOL_CONNECTIONS": _get_max_pool_connections(settings),
            "POLICY": settings[get_setting_name("S3_ACL")],
            "UPLOAD_TIMEOUT": settings.getfloat("UPLOAD_TIMEOUT") or None,
        }

    def __init__(self, uri: str, **config: Any):
        self.__dict__.update(config)
        if not is_botocore_available():
            raise NotConfigured("missing botocore library")
        import botocore.session  # noqa: PLC0415
        from botocore.config import Config  # noqa: PLC0415

        config_kwargs: dict[str, Any] = {}
        if self.AWS_MAX_POOL_CONNECTIONS is not None:
            config_kwargs["max_pool_connections"] = self.AWS_MAX_POOL_CONNECTIONS
        if self.UPLOAD_TIMEOUT is not None:
            config_kwargs["read_timeout"] = self.UPLOAD_TIMEOUT
        self._client_kwargs: dict[str, Any] = {
            "aws_access_key_id": self.AWS_ACCESS_KEY_ID,
            "aws_secret_access_key": self.AWS_SECRET_ACCESS_KEY,
            "aws_session_token": self.AWS_SESSION_TOKEN,
            "endpoint_url": self.AWS_ENDPOINT_URL,
            "region_name": self.AWS_REGION_NAME,
            "use_ssl": True if self.AWS_USE_SSL is None else self.AWS_USE_SSL,
            "verify": self.AWS_VERIFY,
            "config": Config(**config_kwargs),
        }
        # Used when asyncio or aiobotocore is not available, in a thread.
        session = botocore.session.get_session()
        self.s3_client = session.create_client("s3", **self._client_kwargs)
        if not uri.startswith("s3://"):
            raise ValueError(f"Incorrect URI scheme in {uri}, expected 's3'")
        self.bucket, self.prefix = uri[5:].split("/", 1)

        # Created lazily, from within the running event loop, and closed by
        # close().
        self._aio_client: Any = None
        self._aio_client_cm: Any = None
        self._aio_client_lock = asyncio.Lock()

    def _use_async(self) -> bool:
        return is_asyncio_available() and is_aiobotocore_available()

    async def _get_aio_client(self) -> Any:
        async with self._aio_client_lock:
            if self._aio_client is None:
                from aiobotocore.session import get_session  # noqa: PLC0415

                self._aio_client_cm = get_session().create_client(
                    "s3", **self._client_kwargs
                )
                # pylint: disable-next=unnecessary-dunder-call
                self._aio_client = await self._aio_client_cm.__aenter__()
        return self._aio_client

    async def close(self) -> None:
        """Close the underlying aiobotocore client, if one was created."""
        if self._aio_client is not None:
            await self._aio_client_cm.__aexit__(None, None, None)
            self._aio_client = self._aio_client_cm = None

    def _onsuccess(self, boto_key: dict[str, Any]) -> StatInfo:
        last_modified = boto_key["LastModified"]
        modified_stamp = last_modified.timestamp()
        if self.checksum_algorithm != "md5":
            return {"last_modified": modified_stamp}
        checksum = boto_key["ETag"].strip('"')
        return {"checksum": checksum, "last_modified": modified_stamp}

    def stat_file(
        self, path: str, info: MediaPipeline.SpiderInfo
    ) -> Deferred[StatInfo]:
        return deferred_from_coro(self._stat_file(path))

    async def _stat_file(self, path: str) -> StatInfo:
        key_name = f"{self.prefix}{path}"
        if self._use_async():
            client = await self._get_aio_client()
            boto_key = await client.head_object(Bucket=self.bucket, Key=key_name)
        else:
            boto_key = await run_in_thread(
                self.s3_client.head_object,  # type: ignore[attr-defined]
                Bucket=self.bucket,
                Key=key_name,
            )
        return self._onsuccess(boto_key)

    def persist_file(
        self,
        path: str,
        buf: BytesIO,
        info: MediaPipeline.SpiderInfo,
        meta: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Deferred[Any]:
        """Upload file to S3 storage"""
        return deferred_from_coro(self._persist_file(path, buf, meta, headers))

    async def _persist_file(
        self,
        path: str,
        buf: BytesIO,
        meta: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> Any:
        key_name = f"{self.prefix}{path}"
        buf.seek(0)
        extra = self._headers_to_botocore_kwargs(self.HEADERS)
        if headers:
            extra.update(self._headers_to_botocore_kwargs(headers))
        kwargs: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key_name,
            "Body": buf,
            "Metadata": {k: str(v) for k, v in meta.items()} if meta else {},
            "ACL": self.POLICY,
            **extra,
        }
        if self._use_async():
            client = await self._get_aio_client()
            return await client.put_object(**kwargs)
        return await run_in_thread(
            self.s3_client.put_object,  # type: ignore[attr-defined]
            **kwargs,
        )

    @functools.cached_property
    def _botocore_header_kwargs(self) -> CaseInsensitiveDict:
        input_shape = self.s3_client.meta.service_model.operation_model(
            "PutObject"
        ).input_shape
        assert input_shape is not None
        return CaseInsensitiveDict(
            {
                shape.serialization["name"]: name
                for name, shape in input_shape.members.items()
                if shape.serialization.get("location") == "header"
            }
        )

    def _headers_to_botocore_kwargs(self, headers: dict[str, str]) -> dict[str, str]:
        """Convert headers to botocore keyword arguments."""
        mapping = self._botocore_header_kwargs
        extra: dict[str, Any] = {}
        for key, value in headers.items():
            try:
                kwarg = mapping[key]
            except KeyError:
                raise TypeError(
                    f'Header "{key}" is not supported by botocore'
                ) from None
            extra[kwarg] = value
        return extra


class GCSFilesStore:
    # Google Cloud Storage cannot hash a stored object on demand. It reports an
    # MD5 hash and a CRC32C checksum.
    checksum_algorithm: str = "md5"

    GCS_PROJECT_ID = None
    UPLOAD_TIMEOUT: float | None = None

    CACHE_CONTROL = "max-age=172800"

    # The bucket's default object ACL will be applied to the object.
    POLICY = None

    @classmethod
    def from_crawler(
        cls, crawler: Crawler, uri: str, *, get_setting_name: Callable[[str], str]
    ) -> Self:
        return cls(uri, **cls._get_config(crawler, get_setting_name))

    @staticmethod
    def _get_config(
        crawler: Crawler, get_setting_name: Callable[[str], str]
    ) -> dict[str, Any]:
        settings = crawler.settings
        return {
            "GCS_PROJECT_ID": settings["GCS_PROJECT_ID"],
            "POLICY": settings[get_setting_name("GCS_ACL")] or None,
            "UPLOAD_TIMEOUT": settings.getfloat("UPLOAD_TIMEOUT") or None,
        }

    def __init__(self, uri: str, **config: Any):
        self.__dict__.update(config)
        from google.cloud import storage  # noqa: PLC0415

        client = storage.Client(project=self.GCS_PROJECT_ID)
        bucket, prefix = uri[5:].split("/", 1)
        self.bucket = client.bucket(bucket)
        self.prefix: str = prefix
        permissions = self.bucket.test_iam_permissions(
            ["storage.objects.get", "storage.objects.create"]
        )
        if "storage.objects.get" not in permissions:
            logger.warning(
                "No 'storage.objects.get' permission for GCS bucket %(bucket)s. "
                "Checking if files are up to date will be impossible. Files will be downloaded every time.",
                {"bucket": bucket},
            )
        if "storage.objects.create" not in permissions:
            logger.error(
                "No 'storage.objects.create' permission for GCS bucket %(bucket)s. Saving files will be impossible!",
                {"bucket": bucket},
            )

    def _onsuccess(self, blob: Any) -> StatInfo:
        if not blob:
            return {}
        last_modified = blob.updated.timestamp()
        if self.checksum_algorithm != "md5":
            return {"last_modified": last_modified}
        checksum = base64.b64decode(blob.md5_hash).hex()
        return {"checksum": checksum, "last_modified": last_modified}

    def stat_file(
        self, path: str, info: MediaPipeline.SpiderInfo
    ) -> Deferred[StatInfo]:

        blob_path = self._get_blob_path(path)
        d: Deferred[Any] = deferred_from_coro(
            run_in_thread(self.bucket.get_blob, blob_path)
        )
        return d.addCallback(self._onsuccess)

    def _get_content_type(self, headers: dict[str, str] | None) -> str:
        if headers and "Content-Type" in headers:
            return headers["Content-Type"]
        return "application/octet-stream"

    def _get_blob_path(self, path: str) -> str:
        return self.prefix + path

    def persist_file(
        self,
        path: str,
        buf: BytesIO,
        info: MediaPipeline.SpiderInfo,
        meta: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Deferred[Any]:
        blob_path = self._get_blob_path(path)
        blob = self.bucket.blob(blob_path)
        blob.cache_control = self.CACHE_CONTROL
        blob.metadata = {k: str(v) for k, v in meta.items()} if meta else {}
        timeout = self.UPLOAD_TIMEOUT
        kwargs = {} if timeout is None else {"timeout": timeout}
        return deferred_from_coro(
            run_in_thread(
                blob.upload_from_string,
                data=buf.getvalue(),
                content_type=self._get_content_type(headers),
                predefined_acl=self.POLICY,
                **kwargs,
            )
        )


class FTPFilesStore:
    checksum_algorithm: str = "md5"

    FTP_USERNAME: str | None = None
    FTP_PASSWORD: str | None = None
    USE_ACTIVE_MODE: bool | None = None
    UPLOAD_TIMEOUT: float | None = None

    @classmethod
    def from_crawler(
        cls, crawler: Crawler, uri: str, *, get_setting_name: Callable[[str], str]
    ) -> Self:
        return cls(uri, **cls._get_config(crawler, get_setting_name))

    @staticmethod
    def _get_config(
        crawler: Crawler, get_setting_name: Callable[[str], str]
    ) -> dict[str, Any]:
        settings = crawler.settings
        return {
            "FTP_USERNAME": settings["FTP_USER"],
            "FTP_PASSWORD": settings["FTP_PASSWORD"],
            "USE_ACTIVE_MODE": settings.getbool("FEED_STORAGE_FTP_ACTIVE"),
            "UPLOAD_TIMEOUT": settings.getfloat("UPLOAD_TIMEOUT") or None,
        }

    def __init__(self, uri: str, **config: Any):
        self.__dict__.update(config)
        if not uri.startswith("ftp://"):
            raise ValueError(f"Incorrect URI scheme in {uri}, expected 'ftp'")
        u = urlparse(uri)
        assert u.port
        assert u.hostname
        self.port: int = u.port
        self.host: str = u.hostname
        self.port = int(u.port or 21)
        assert self.FTP_USERNAME
        assert self.FTP_PASSWORD
        self.username: str = u.username or self.FTP_USERNAME
        self.password: str = u.password or self.FTP_PASSWORD
        self.basedir: str = u.path.rstrip("/")

    def persist_file(
        self,
        path: str,
        buf: BytesIO,
        info: MediaPipeline.SpiderInfo,
        meta: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Deferred[Any]:
        path = f"{self.basedir}/{path}"
        return deferred_from_coro(
            run_in_thread(
                ftp_store_file,
                path=path,
                file=buf,
                host=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                use_active_mode=bool(self.USE_ACTIVE_MODE),
                timeout=self.UPLOAD_TIMEOUT,
            )
        )

    def _stat_file(self, path: str) -> StatInfo:
        try:
            with FTP() as ftp:
                ftp.connect(self.host, self.port)
                ftp.login(self.username, self.password)
                if self.USE_ACTIVE_MODE:
                    ftp.set_pasv(False)
                file_path = f"{self.basedir}/{path}"
                modified = ftp.voidcmd(f"MDTM {file_path}")[4:].strip()
                time_format = "%Y%m%d%H%M%S.%f" if "." in modified else "%Y%m%d%H%M%S"
                last_modified = (
                    datetime.strptime(modified, time_format)
                    .replace(tzinfo=UTC)
                    .timestamp()
                )
                m = hashlib.new(self.checksum_algorithm)
                ftp.retrbinary(f"RETR {file_path}", m.update)
            return {"last_modified": last_modified, "checksum": m.hexdigest()}
        # The file doesn't exist
        except Exception:
            return {}

    def stat_file(
        self, path: str, info: MediaPipeline.SpiderInfo
    ) -> Deferred[StatInfo]:
        return deferred_from_coro(run_in_thread(self._stat_file, path))


_STORE_SCHEMES_DEPRECATION = (
    "FilesPipeline.STORE_SCHEMES is deprecated, use the MEDIA_STORAGES setting instead."
)
_BUILTIN_STORE_SCHEMES: dict[str, type[FilesStoreProtocol]] = {
    "": FSFilesStore,
    "file": FSFilesStore,
    "s3": S3FilesStore,
    "gs": GCSFilesStore,
    "ftp": FTPFilesStore,
}


class _DeprecatedStoreSchemes:
    def __init__(self) -> None:
        self.value: dict[str, type[FilesStoreProtocol]] = dict(_BUILTIN_STORE_SCHEMES)

    def __get__(
        self, instance: Any, owner: type
    ) -> dict[str, type[FilesStoreProtocol]]:
        warnings.warn(
            _STORE_SCHEMES_DEPRECATION,
            category=ScrapyDeprecationWarning,
            stacklevel=2,
        )
        return self.value


def _store_scheme_overrides(
    pipeline: FilesPipeline,
) -> dict[str, type[FilesStoreProtocol] | None]:
    """Return the schemes that the ``STORE_SCHEMES`` attribute of *pipeline*
    adds or replaces, and the built-in schemes that it removes, mapped to
    ``None``.
    """
    owner = next(
        obj
        for obj in (pipeline, *type(pipeline).__mro__)
        if "STORE_SCHEMES" in vars(obj)
    )
    schemes = vars(owner)["STORE_SCHEMES"]
    if isinstance(schemes, _DeprecatedStoreSchemes):
        schemes = schemes.value
    overrides: dict[str, type[FilesStoreProtocol] | None] = {
        scheme: store
        for scheme, store in schemes.items()
        if _BUILTIN_STORE_SCHEMES.get(scheme) is not store
    }
    for scheme in _BUILTIN_STORE_SCHEMES.keys() - schemes.keys():
        overrides[scheme] = None
    return overrides


class FilesPipeline(MediaPipeline):
    """Pipeline that implements file downloading.

    This pipeline tries to minimize network transfers and file processing,
    doing stat of the files and determining if file is new, up-to-date or
    expired.

    ``new`` files are those that pipeline never processed and needs to be
        downloaded from supplier site the first time.

    ``uptodate`` files are the ones that the pipeline processed and are still
        valid files.

    ``expired`` files are those that pipeline already processed but the last
        modification was made long time ago, so a reprocessing is recommended to
        refresh it in case of change.

    """

    MEDIA_NAME: str = "file"
    EXPIRES: int = 90
    if TYPE_CHECKING:
        STORE_SCHEMES: ClassVar[dict[str, type[FilesStoreProtocol]]]
    else:
        STORE_SCHEMES = _DeprecatedStoreSchemes()
    DEFAULT_FILES_URLS_FIELD: str = "file_urls"
    DEFAULT_FILES_RESULT_FIELD: str = "files"

    _checksum_warned: bool = False

    def __init__(
        self,
        store_uri: str | PathLike[str],
        download_func: None = None,
        *,
        crawler: Crawler,
    ):
        if download_func is not None:  # pragma: no cover
            warnings.warn(
                "The download_func argument of FilesPipeline.__init__() is ignored"
                " and will be removed in a future Scrapy version.",
                category=ScrapyDeprecationWarning,
                stacklevel=2,
            )

        if not (store_uri and (store_uri := _to_string(store_uri))):
            from scrapy.pipelines.images import ImagesPipeline  # noqa: PLC0415

            setting_name = (
                "IMAGES_STORE" if isinstance(self, ImagesPipeline) else "FILES_STORE"
            )
            raise NotConfigured(
                f"{setting_name} setting must be set to a valid path (not empty) "
                f"to enable {self.__class__.__name__}."
            )

        super().__init__(crawler=crawler)

        settings = crawler.settings
        cls_name = "FilesPipeline"
        storages = dict(settings.getwithbase("MEDIA_STORAGES"))
        overrides = _store_scheme_overrides(self)
        if overrides:
            warnings.warn(
                _STORE_SCHEMES_DEPRECATION,
                category=ScrapyDeprecationWarning,
                stacklevel=2,
            )
            storages.update(overrides)
        self._storages: dict[str, Any] = without_none_values(storages)
        self._legacy_schemes: set[str] = set(overrides)
        self.store: FilesStoreProtocol = self._get_store(store_uri)
        resolve = functools.partial(
            self._key_for_pipe, base_class_name=cls_name, settings=settings
        )
        self.store.checksum_algorithm = _checksum_algorithm(
            settings, resolve("FILES_CHECKSUM_ALGORITHM")
        )
        self.expires: float = settings.getfloat(resolve("FILES_EXPIRES"), self.EXPIRES)
        if not hasattr(self, "FILES_URLS_FIELD"):
            self.FILES_URLS_FIELD = self.DEFAULT_FILES_URLS_FIELD
        if not hasattr(self, "FILES_RESULT_FIELD"):
            self.FILES_RESULT_FIELD = self.DEFAULT_FILES_RESULT_FIELD
        self.files_urls_field: str = settings.get(
            resolve("FILES_URLS_FIELD"), self.FILES_URLS_FIELD
        )
        self.files_result_field: str = settings.get(
            resolve("FILES_RESULT_FIELD"), self.FILES_RESULT_FIELD
        )

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        return cls(crawler.settings["FILES_STORE"], crawler=crawler)

    @_warn_spider_arg
    async def close_spider(self, spider: Spider | None = None) -> None:
        close = getattr(self.store, "close", None)
        if close is not None:
            await ensure_awaitable(close())

    def _get_store_setting_name(self, name: str) -> str:
        return self._key_for_pipe(
            f"FILES_STORE_{name}", "FilesPipeline", self.crawler.settings
        )

    def _get_store(self, uri: str) -> FilesStoreProtocol:
        # to support win32 paths like: C:\\some\dir
        scheme = "file" if Path(uri).is_absolute() else urlparse(uri).scheme
        store_cls = load_object(self._storages[scheme])
        if scheme in self._legacy_schemes:
            # Storages from STORE_SCHEMES get the configuration of the
            # built-in storage of their scheme as class attributes, and are
            # built with the URI alone.
            builtin = _BUILTIN_STORE_SCHEMES.get(scheme)
            if get_config := getattr(builtin, "_get_config", None):
                config = get_config(self.crawler, self._get_store_setting_name)
                for name, value in config.items():
                    setattr(store_cls, name, value)
            return cast("FilesStoreProtocol", store_cls(uri))
        if hasattr(store_cls, "from_crawler"):
            return cast(
                "FilesStoreProtocol",
                store_cls.from_crawler(
                    self.crawler, uri, get_setting_name=self._get_store_setting_name
                ),
            )
        return cast("FilesStoreProtocol", store_cls(uri))

    def _onsuccess(
        self,
        result: StatInfo,
        request: Request,
        info: MediaPipeline.SpiderInfo,
        path: str,
    ) -> FileInfo | None:
        if not result:
            return None  # returning None force download

        last_modified = result.get("last_modified", None)
        if not last_modified:
            return None  # returning None force download

        age_seconds = time.time() - last_modified
        age_days = age_seconds / 60 / 60 / 24
        if 0 <= self.expires < age_days:
            return None  # returning None force download

        referer = referer_str(request)
        logger.debug(
            "File (uptodate): Downloaded %(medianame)s from %(request)s "
            "referred in <%(referer)s>",
            {"medianame": self.MEDIA_NAME, "request": request, "referer": referer},
            extra={"spider": info.spider},
        )
        self.inc_stats("uptodate")

        checksum = result.get("checksum", None)
        if checksum is None and not self._checksum_warned:
            self._checksum_warned = True
            logger.warning(
                f"{self.store.__class__.__name__} does not report "
                f"{self.store.checksum_algorithm} checksums of stored files, so "
                f"files that are not downloaded again, because they have not "
                f"expired yet, get no checksum.",
                extra={"spider": info.spider},
            )
        return {
            "url": request.url,
            "path": path,
            "checksum": checksum,
            "status": "uptodate",
        }

    def media_to_download(
        self, request: Request, info: MediaPipeline.SpiderInfo, *, item: Any = None
    ) -> Deferred[FileInfo | None] | None:
        path = self.file_path(request, info=info, item=item)
        dfd: Deferred[StatInfo] = _maybeDeferred_coro(
            self.store.stat_file, False, path, info
        )
        dfd2: Deferred[FileInfo | None] = dfd.addCallback(
            functools.partial(self._onsuccess, request=request, info=info, path=path)
        )
        dfd2.addErrback(lambda _: None)
        dfd2.addErrback(
            lambda f: logger.error(
                self.__class__.__name__ + ".store.stat_file",
                exc_info=failure_to_exc_info(f),
                extra={"spider": info.spider},
            )
        )
        return dfd2

    def media_failed(
        self, failure: Failure, request: Request, info: MediaPipeline.SpiderInfo
    ) -> NoReturn:
        referer = referer_str(request)
        if isinstance(failure.value, IgnoreRequest):
            logger.debug(
                f"File (filtered): Not downloading {self.MEDIA_NAME} from "
                f"{request} referred in <{referer}>: {failure.value}",
                extra={"spider": info.spider},
            )
            raise _MediaRequestFiltered(str(failure.value)) from failure.value

        logger.warning(
            f"File (unknown-error): Error downloading {self.MEDIA_NAME} from "
            f"{request} referred in <{referer}>: {failure.value}",
            extra={"spider": info.spider},
        )
        raise _FileException

    async def media_downloaded(
        self,
        response: Response,
        request: Request,
        info: MediaPipeline.SpiderInfo,
        *,
        item: Any = None,
    ) -> FileInfo:
        referer = referer_str(request)

        if response.status not in {200, 201}:
            logger.warning(
                "File (code: %(status)s): Error downloading file from "
                "%(request)s referred in <%(referer)s>",
                {"status": response.status, "request": request, "referer": referer},
                extra={"spider": info.spider},
            )
            raise _FileException("download-error")

        if not response.body:
            logger.warning(
                "File (empty-content): Empty file from %(request)s referred "
                "in <%(referer)s>: no-content",
                {"request": request, "referer": referer},
                extra={"spider": info.spider},
            )
            raise _FileException("empty-content")

        status = "cached" if "cached" in response.flags else "downloaded"
        logger.debug(
            "File (%(status)s): Downloaded file from %(request)s referred in "
            "<%(referer)s>",
            {"status": status, "request": request, "referer": referer},
            extra={"spider": info.spider},
        )
        self.inc_stats(status)

        try:
            path = self.file_path(request, response=response, info=info, item=item)
            checksum: str = await ensure_awaitable(
                self.file_downloaded(response, request, info, item=item)
            )
        except _FileException as exc:
            logger.warning(
                "File (error): Error processing file from %(request)s "
                "referred in <%(referer)s>: %(errormsg)s",
                {"request": request, "referer": referer, "errormsg": str(exc)},
                extra={"spider": info.spider},
                exc_info=True,
            )
            raise
        except Exception as exc:
            logger.exception(
                "File (unknown-error): Error processing file from %(request)s "
                "referred in <%(referer)s>",
                {"request": request, "referer": referer},
                extra={"spider": info.spider},
            )
            raise _FileException(str(exc)) from exc

        return {
            "url": request.url,
            "path": path,
            "checksum": checksum,
            "status": status,
        }

    def inc_stats(self, status: str) -> None:
        stats = self.crawler.stats
        stats.inc_value("file_count")
        stats.inc_value(f"file_status_count/{status}")

    async def _file_downloaded(
        self,
        response: Response,
        request: Request,
        info: MediaPipeline.SpiderInfo,
        *,
        item: Any = None,
    ) -> str:
        path = self.file_path(request, response=response, info=info, item=item)
        buf = BytesIO(response.body)
        checksum = hashlib.file_digest(buf, self.store.checksum_algorithm).hexdigest()
        buf.seek(0)
        await ensure_awaitable(self.store.persist_file(path, buf, info))
        return checksum

    # Overridable Interface
    def get_media_requests(
        self, item: Any, info: MediaPipeline.SpiderInfo
    ) -> list[Request]:
        urls = ItemAdapter(item).get(self.files_urls_field, [])
        if not isinstance(urls, list):
            raise TypeError(
                f"{self.files_urls_field} must be a list of URLs, got {type(urls).__name__}. "
            )
        return [Request(u, callback=NO_CALLBACK) for u in urls]

    def file_downloaded(
        self,
        response: Response,
        request: Request,
        info: MediaPipeline.SpiderInfo,
        *,
        item: Any = None,
    ) -> str | Awaitable[str]:
        return self._file_downloaded(response, request, info, item=item)

    def item_completed(
        self, results: list[FileInfoOrError], item: Any, info: MediaPipeline.SpiderInfo
    ) -> Any:
        with suppress(KeyError):
            ItemAdapter(item)[self.files_result_field] = [x for ok, x in results if ok]
        return item

    def file_path(
        self,
        request: Request,
        response: Response | None = None,
        info: MediaPipeline.SpiderInfo | None = None,
        *,
        item: Any = None,
    ) -> str:
        media_guid = hashlib.sha1(request.url.encode()).hexdigest()  # noqa: S324

        # clean it up and look at the path first
        parsed_url = urlparse_cached(request)
        media_ext = Path(parsed_url.path).suffix

        # if path has no extension look at the raw  URL
        if media_ext not in mimetypes.types_map:
            media_ext = Path(request.url).suffix

        # Handles empty and wild extensions by trying to guess the
        # mime type then extension or default to empty string otherwise
        if media_ext not in mimetypes.types_map:
            media_ext = ""
            media_type = mimetypes.guess_type(request.url)[0]
            if media_type:
                media_ext = cast("str", mimetypes.guess_extension(media_type))
        return f"full/{media_guid}{media_ext}"


def __getattr__(name: str) -> Any:
    if name == "FileException":
        warnings.warn(
            "scrapy.pipelines.files.FileException is deprecated, use "
            "scrapy.pipelines.media.FileException instead.",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )
        return _FileException
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
