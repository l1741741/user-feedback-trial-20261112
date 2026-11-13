"""Source of information about a wheel file."""

import base64
import binascii
import csv
import hashlib
import io
import posixpath
import re
import stat
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cached_property
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    BinaryIO,
    ClassVar,
    cast,
)

from installer.exceptions import InstallerError
from installer.records import Hash, parse_record_file
from installer.utils import canonicalize_name, parse_wheel_filename

if TYPE_CHECKING:
    import os

WheelContentElement = tuple[tuple[str, str, str], BinaryIO, bool]


__all__ = ["WheelFile", "WheelSource"]

#: Maximum number of bytes read from a wheel member in one ``read()`` call.
_VALIDATION_CHUNK_SIZE = 64 * 1024

#: Hash algorithms accepted in a wheel's ``RECORD`` (PEP 427 / installers'
#: current practice). The generic ``Hash`` API intentionally accepts more.
_ACCEPTED_RECORD_HASHES: frozenset[str] = frozenset({"sha256", "sha384", "sha512"})

#: Canonical, unpadded URL-safe Base64 (RFC 4648 section 5).
_UNPADDED_URLSAFE_B64_RE = re.compile(r"^[A-Za-z0-9_-]+$")

#: ASCII decimal non-negative integer, no sign, no whitespace, no Unicode digits.
_ASCII_DECIMAL_RE = re.compile(r"^[0-9]+$")

#: Names of the optional digital signature files (PEP 427).
_SIGNATURE_FILE_NAMES = frozenset({"RECORD.jws", "RECORD.p7s"})


class WheelSource:
    """Represents an installable wheel.

    This is an abstract class, whose methods have to be implemented by subclasses.
    """

    validation_error: ClassVar[type[Exception]] = ValueError  #: :meta hide-value:
    """
    .. versionadded:: 0.7.0

    Exception to be raised by :py:meth:`validate_record` when validation fails.
    This is expected to be a subclass of :py:class:`ValueError`.
    """

    def __init__(self, distribution: str, version: str) -> None:
        """Initialize a WheelSource object.

        :param distribution: distribution name (like ``urllib3``)
        :param version: version associated with the wheel
        """
        super().__init__()
        self.distribution = distribution
        self.version = version

    @property
    def dist_info_dir(self) -> str:
        """Name of the dist-info directory."""
        return f"{self.distribution}-{self.version}.dist-info"

    @property
    def data_dir(self) -> str:
        """Name of the data directory."""
        return f"{self.distribution}-{self.version}.data"

    @property
    def dist_info_filenames(self) -> list[str]:
        """Get names of all files in the dist-info directory.

        Sample usage/behaviour::

            >>> wheel_source.dist_info_filenames
            ['METADATA', 'WHEEL']
        """
        raise NotImplementedError

    def read_dist_info(self, filename: str) -> str:
        """Get contents, from ``filename`` in the dist-info directory.

        Sample usage/behaviour::

            >>> wheel_source.read_dist_info("METADATA")
            ...

        :param filename: name of the file
        """
        raise NotImplementedError

    def validate_record(self) -> None:
        """Validate ``RECORD`` of the wheel.

        .. versionadded:: 0.7.0

        This method should be called before :py:func:`install <installer.install>`
        if validation is required.
        """
        raise NotImplementedError

    def get_contents(self) -> Iterator[WheelContentElement]:
        """Sequential access to all contents of the wheel (including dist-info files).

        This method should return an iterable. Each value from the iterable must be a
        tuple containing 3 elements:

        - record: 3-value tuple, to pass to
          :py:meth:`RecordEntry.from_elements <installer.records.RecordEntry.from_elements>`.
        - stream: An :py:class:`io.BufferedReader` object, providing the contents of the
          file at the location provided by the first element (path).
        - is_executable: A boolean, representing whether the item has an executable bit.

        All paths must be relative to the root of the wheel.

        Sample usage/behaviour::

            >>> iterable = wheel_source.get_contents()
            >>> next(iterable)
            (('pkg/__init__.py', '', '0'), <...>, False)

        This method may be called multiple times. Each iterable returned must
        provide the same content upon reading from a specific file's stream.
        """
        raise NotImplementedError


class _WheelFileValidationError(ValueError, InstallerError):
    """Raised when a wheel file fails validation."""

    def __init__(self, issues: list[str]) -> None:
        super().__init__(repr(issues))
        self.issues = issues

    def __repr__(self) -> str:
        return f"WheelFileValidationError(issues={self.issues!r})"


class _WheelFileBadDistInfo(ValueError, InstallerError):
    """Raised when a wheel file has issues around `.dist-info`."""

    def __init__(self, *, reason: str, filename: str | None, dist_info: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.filename = filename
        self.dist_info = dist_info

    def __str__(self) -> str:
        return (
            f"{self.reason} (filename={self.filename!r}, dist_info={self.dist_info!r})"
        )


def _parse_wheel_record(
    record_text: str,
) -> tuple[dict[str, tuple[str, str, str]], list[str]]:
    """Parse a wheel's ``RECORD`` text.

    Unlike :func:`installer.records.parse_record_file`, this keeps paths
    exactly as written in the CSV (no backslash rewriting or other
    normalization), so they can be matched precisely against the raw member
    names stored in the ZIP. Malformed rows and duplicate paths are collected
    as issues instead of aborting the parse or silently overwriting values.

    :param record_text: full decoded contents of ``RECORD``.
    :return: mapping of path to the first ``(path, hash, size)`` row seen, and
        the structural issues found while parsing.
    """
    issues: list[str] = []
    entries: dict[str, tuple[str, str, str]] = {}

    reader = csv.reader(
        io.StringIO(record_text, newline=""),
        delimiter=",",
        quotechar='"',
        lineterminator="\n",
    )
    for row_index, elements in enumerate(reader):
        if len(elements) != 3:
            issues.append(
                f"Row Index {row_index}: expected 3 elements, got {len(elements)}"
            )
            continue

        row = (elements[0], elements[1], elements[2])
        path = row[0]
        if path in entries:
            issues.append(f"Duplicate path {path!r} in RECORD")
            continue
        entries[path] = row

    return entries, issues


def _parse_record_hash(hash_text: str) -> tuple[Hash | None, list[str]]:
    """Validate a ``RECORD`` hash column under the wheel-specific rules.

    Only :data:`_ACCEPTED_RECORD_HASHES` are accepted and the digest must be
    canonical, unpadded URL-safe Base64 whose decoded length matches the
    algorithm's digest size.

    :param hash_text: raw second column of a ``RECORD`` row.
    :return: the parsed :class:`Hash` (or ``None`` when invalid) and the list
        of issues found.
    """
    issues: list[str] = []

    algorithm, separator, digest_text = hash_text.partition("=")
    if not separator or not algorithm or not digest_text:
        return None, ["`hash` does not follow the required format"]

    if algorithm not in _ACCEPTED_RECORD_HASHES:
        issues.append(f"invalid hash algorithm {algorithm!r}")

    if not _UNPADDED_URLSAFE_B64_RE.fullmatch(digest_text):
        issues.append("hash digest is not unpadded URL-safe Base64")
    else:
        try:
            # Re-add the padding that canonical RECORD digests strip.
            padding = "=" * (-len(digest_text) % 4)
            digest = base64.b64decode(
                digest_text + padding, altchars=b"-_", validate=True
            )
        except (binascii.Error, ValueError):
            issues.append("hash digest is not valid URL-safe Base64")
        else:
            if algorithm in _ACCEPTED_RECORD_HASHES:
                expected_digest_size = hashlib.new(algorithm).digest_size
                if len(digest) != expected_digest_size:
                    issues.append(
                        f"hash digest decodes to {len(digest)} bytes, expected "
                        f"{expected_digest_size} bytes for {algorithm!r}"
                    )

    if issues:
        return None, issues
    return Hash(algorithm, digest_text), []


def _parse_record_size(size_text: str) -> tuple[int | None, str | None]:
    """Validate a ``RECORD`` size column.

    Only ASCII decimal non-negative integer strings are accepted (leading
    zeros are fine; signs, whitespace and Unicode digits are not).

    :param size_text: raw third column of a ``RECORD`` row.
    :return: the parsed size (or ``None`` when invalid) and the issue found,
        if any.
    """
    if not _ASCII_DECIMAL_RE.fullmatch(size_text):
        return None, "`size` is not an ASCII decimal non-negative integer"
    return int(size_text), None


def _hash_and_size_stream(
    stream: BinaryIO, algorithm: str
) -> tuple[str, int]:
    """Hash and measure a wheel member while streaming it in 64 KiB chunks.

    The member is never fully buffered in memory and nothing is extracted to
    the filesystem.

    :param stream: open member stream from :meth:`zipfile.ZipFile.open`.
    :param algorithm: name of the hash algorithm to use.
    :return: the unpadded URL-safe Base64 digest and the number of bytes read.
    """
    hasher = hashlib.new(algorithm)
    size = 0
    while True:
        chunk = stream.read(_VALIDATION_CHUNK_SIZE)
        if not chunk:
            break
        hasher.update(chunk)
        size += len(chunk)

    digest = base64.urlsafe_b64encode(hasher.digest()).decode("ascii").rstrip("=")
    return digest, size


class WheelFile(WheelSource):
    """Implements `WheelSource`, for an existing file from the filesystem.

    Example usage::

        >>> with WheelFile.open("sampleproject-2.0.0-py3-none-any.whl") as source:
        ...     installer.install(source, destination)
    """

    validation_error = _WheelFileValidationError

    def __init__(self, f: zipfile.ZipFile) -> None:
        """Initialize a WheelFile object.

        :param f: An open zipfile, which will stay open as long as this object is used.
        """
        self._zipfile = f
        assert f.filename

        basename = Path(f.filename).name
        parsed_name = parse_wheel_filename(basename)
        super().__init__(
            version=parsed_name.version,
            distribution=parsed_name.distribution,
        )

    @classmethod
    @contextmanager
    def open(cls, path: "os.PathLike[str]") -> Iterator["WheelFile"]:
        """Create a wheelfile from a given path."""
        with zipfile.ZipFile(path) as f:
            yield cls(f)

    @cached_property
    def dist_info_dir(self) -> str:
        """Name of the dist-info directory."""
        top_level_directories = {
            path.split("/", 1)[0] for path in self._zipfile.namelist()
        }
        dist_infos = [
            name for name in top_level_directories if name.endswith(".dist-info")
        ]

        try:
            (dist_info_dir,) = dist_infos
        except ValueError:
            raise _WheelFileBadDistInfo(
                reason="Wheel doesn't contain exactly one .dist-info directory",
                filename=self._zipfile.filename,
                dist_info=str(sorted(dist_infos)),
            ) from None

        # NAME-VER.dist-info
        di_dname = dist_info_dir.rsplit("-", 2)[0]
        norm_di_dname = canonicalize_name(di_dname)
        norm_file_dname = canonicalize_name(self.distribution)

        if norm_di_dname != norm_file_dname:
            raise _WheelFileBadDistInfo(
                reason="Wheel .dist-info directory doesn't match wheel filename",
                filename=self._zipfile.filename,
                dist_info=dist_info_dir,
            )

        return dist_info_dir

    @property
    def dist_info_filenames(self) -> list[str]:
        """Get names of all files in the dist-info directory."""
        base = self.dist_info_dir
        return [
            name[len(base) + 1 :]
            for name in self._zipfile.namelist()
            if name[-1:] != "/"
            if base == posixpath.commonpath([name, base])
        ]

    def read_dist_info(self, filename: str) -> str:
        """Get contents, from ``filename`` in the dist-info directory."""
        path = posixpath.join(self.dist_info_dir, filename)
        return self._zipfile.read(path).decode("utf-8")

    def validate_record(self, *, validate_contents: bool = True) -> None:
        """Validate ``RECORD`` of the wheel.

        This method should be called before :py:func:`install <installer.install>`
        if validation is required.

        Non-directory members of the ZIP and paths listed in ``RECORD`` are
        checked bidirectionally: duplicate ZIP member names, duplicate
        ``RECORD`` paths, rows that do not have exactly three columns, files
        listed in ``RECORD`` but absent from the ZIP, and ZIP members missing
        from ``RECORD`` are all reported. The optional ``RECORD.jws`` and
        ``RECORD.p7s`` files directly inside this wheel's ``.dist-info``
        directory are the only members allowed to be absent from ``RECORD``;
        listing either one there is still an error, and same-named files
        elsewhere in the wheel are not exempt.

        Every regular file must declare both a hash (``sha256``, ``sha384``
        or ``sha512`` with a canonical, unpadded URL-safe Base64 digest of
        the algorithm's digest length) and an ASCII decimal non-negative
        size. The ``RECORD`` file itself must be present and leave both
        columns empty.

        :param validate_contents: Whether to stream each regular member and
            verify its actual size and hash against ``RECORD``. When false,
            only the structural and value-format checks above are performed.
        """
        wheel_filename = self._zipfile.filename
        dist_info_dir = self.dist_info_dir
        record_path = f"{dist_info_dir}/RECORD"

        try:
            record_text = self.read_dist_info("RECORD")
        except Exception as exc:
            raise _WheelFileValidationError(
                [f"Unable to retrieve `RECORD` from {wheel_filename}: {exc!r}"]
            ) from exc

        record_entries, issues = _parse_wheel_record(record_text)

        # Record every non-directory ZIP member, detecting duplicates without
        # letting a later entry shadow an earlier one.
        seen_members: set[str] = set()
        members: list[zipfile.ZipInfo] = []
        for item in self._zipfile.infolist():
            if item.filename[-1:] == "/":  # looks like a directory
                continue
            if item.filename in seen_members:
                issues.append(
                    f"In {wheel_filename}, {item.filename} is duplicated in the ZIP"
                )
                continue
            seen_members.add(item.filename)
            members.append(item)

        for item in members:
            # Exact match against the raw name stored in the ZIP.
            record_args = record_entries.pop(item.filename, None)

            is_dist_info_signature = (
                posixpath.dirname(item.filename) == dist_info_dir
                and posixpath.basename(item.filename) in _SIGNATURE_FILE_NAMES
            )
            if is_dist_info_signature:
                # Both files are for digital signatures, and not mentioned in
                # RECORD; same-named files outside this directory are not exempt.
                if record_args is not None:
                    issues.append(
                        f"In {wheel_filename}, digital signature file "
                        f"{item.filename} is incorrectly contained in RECORD."
                    )
                continue

            if record_args is None:
                issues.append(
                    f"In {wheel_filename}, {item.filename} is not mentioned in RECORD"
                )
                continue

            if item.filename == record_path:
                # RECORD itself must exist and declare neither hash nor size.
                if record_args[1] or record_args[2]:
                    issues.append(
                        f"In {wheel_filename}, RECORD file incorrectly contains "
                        "hash / size."
                    )
                continue

            hash_text, size_text = record_args[1], record_args[2]
            if not hash_text or not size_text:
                issues.append(
                    f"In {wheel_filename}, hash / size of {item.filename} is not "
                    "included in RECORD"
                )
                continue

            record_hash, hash_issues = _parse_record_hash(hash_text)
            for issue in hash_issues:
                issues.append(
                    f"In {wheel_filename}, entry in RECORD file for "
                    f"{item.filename} is invalid: {issue}"
                )

            record_size, size_issue = _parse_record_size(size_text)
            if size_issue is not None:
                issues.append(
                    f"In {wheel_filename}, entry in RECORD file for "
                    f"{item.filename} is invalid: {size_issue}"
                )

            if record_hash is None or record_size is None:
                # Cannot meaningfully check bytes against malformed metadata.
                continue

            if validate_contents:
                try:
                    with self._zipfile.open(item, "r") as stream:
                        actual_digest, actual_size = _hash_and_size_stream(
                            cast("BinaryIO", stream), record_hash.name
                        )
                except Exception as exc:  # noqa: BLE001 - keep checking other members
                    issues.append(
                        f"In {wheel_filename}, unable to read {item.filename} for "
                        f"validation: {exc!r}"
                    )
                    continue

                if actual_size != record_size or actual_digest != record_hash.value:
                    issues.append(
                        f"In {wheel_filename}, hash / size of {item.filename} "
                        "didn't match RECORD"
                    )

        # Anything still listed in RECORD has no corresponding non-directory
        # ZIP member (signature files were popped above if present).
        for leftover_path in record_entries:
            issues.append(
                f"In {wheel_filename}, {leftover_path} is mentioned in RECORD but "
                "not present in the ZIP"
            )

        if issues:
            raise _WheelFileValidationError(issues)

    def get_contents(self) -> Iterator[WheelContentElement]:
        """Sequential access to all contents of the wheel (including dist-info files).

        This implementation requires that every file that is a part of the wheel
        archive has a corresponding entry in RECORD. If they are not, an
        :any:`AssertionError` will be raised.
        """
        # Convert the record file into a useful mapping
        record_lines = self.read_dist_info("RECORD").splitlines()
        records = parse_record_file(record_lines)
        record_mapping = {record[0]: record for record in records}

        for item in self._zipfile.infolist():
            if item.filename[-1:] == "/":  # looks like a directory
                continue

            # Pop record with empty default, because validation is handled by `validate_record`
            record = record_mapping.pop(item.filename, (item.filename, "", ""))

            # Borrowed from:
            # https://github.com/pypa/pip/blob/0f21fb92/src/pip/_internal/utils/unpacking.py#L96-L100
            mode = item.external_attr >> 16
            is_executable = bool(mode and stat.S_ISREG(mode) and mode & 0o111)

            with self._zipfile.open(item) as stream:
                stream_casted = cast("BinaryIO", stream)
                yield record, stream_casted, is_executable
