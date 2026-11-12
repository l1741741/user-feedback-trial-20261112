"""Utilities related to handling / interacting with wheel files."""

import base64
import configparser
import contextlib
import csv
import hashlib
import io
import keyword
import os
import re
import sys
from collections import namedtuple
from collections.abc import Callable, Iterable, Iterator
from configparser import ConfigParser
from email.message import Message
from email.parser import FeedParser
from email.policy import compat32
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    BinaryIO,
    NewType,
    cast,
)

if TYPE_CHECKING:
    from installer.records import RecordEntry
    from installer.scripts import LauncherKind, ScriptSection

Scheme = NewType("Scheme", str)
AllSchemes = tuple[Scheme, ...]

__all__ = [
    "SCHEME_NAMES",
    "InvalidEntryPoint",
    "WheelFilename",
    "construct_record_file",
    "copyfileobj_with_hashing",
    "fix_shebang",
    "get_launcher_kind",
    "make_file_executable",
    "parse_entrypoints",
    "parse_metadata_file",
    "parse_wheel_filename",
]

# Borrowed from https://github.com/python/cpython/blob/v3.9.1/Lib/shutil.py#L52
_WINDOWS = os.name == "nt"
_COPY_BUFSIZE = 1024 * 1024 if _WINDOWS else 64 * 1024

# According to https://www.python.org/dev/peps/pep-0427/#file-name-convention
_WHEEL_FILENAME_REGEX = re.compile(
    r"""
    ^
    (?P<distribution>.+?)
    -(?P<version>.*?)
    (?:-(?P<build_tag>\d[^-]*?))?
    -(?P<tag>.+?-.+?-.+?)
    \.whl
    $
    """,
    re.VERBOSE | re.UNICODE,
)
WheelFilename = namedtuple(
    "WheelFilename", ["distribution", "version", "build_tag", "tag"]
)

# Adapted from https://github.com/python/importlib_metadata/blob/v3.4.0/importlib_metadata/__init__.py#L90
_ENTRYPOINT_REGEX = re.compile(
    r"""
    (?P<module>[\w.]+)\s*
    (:\s*(?P<attrs>[\w.]+))\s*
    (?P<extras>\[.*\])?\s*$
    """,
    re.VERBOSE | re.UNICODE,
)

# According to https://www.python.org/dev/peps/pep-0427/#id7
SCHEME_NAMES = cast("AllSchemes", ("purelib", "platlib", "headers", "scripts", "data"))


def parse_metadata_file(contents: str) -> Message:
    """Parse :pep:`376` ``PKG-INFO``-style metadata files.

    ``METADATA`` and ``WHEEL`` files (as per :pep:`427`) use the same syntax
    and can also be parsed using this function.

    :param contents: The entire contents of the file
    """
    feed_parser = FeedParser(policy=compat32)
    feed_parser.feed(contents)
    return feed_parser.close()


def canonicalize_name(name: str) -> str:
    """Canonicalize a project name according to PEP-503.

    :param name: The project name to canonicalize
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_wheel_filename(filename: str) -> WheelFilename:
    """Parse a wheel filename, into it's various components.

    :param filename: The filename to parse
    """
    wheel_info = _WHEEL_FILENAME_REGEX.match(filename)
    if not wheel_info:
        raise ValueError(f"Not a valid wheel filename: {filename}")
    return WheelFilename(*wheel_info.groups())


def copyfileobj_with_hashing(
    source: BinaryIO,
    dest: BinaryIO,
    hash_algorithm: str,
) -> tuple[str, int]:
    """Copy a buffer while computing the content's hash and size.

    Copies the source buffer into the destination buffer while computing the
    hash of the contents. Adapted from :any:`shutil.copyfileobj`.

    :param source: buffer holding the source data
    :param dest: destination buffer
    :param hash_algorithm: hashing algorithm

    :return: hash digest of the contents, size of the contents
    """
    hasher = hashlib.new(hash_algorithm)
    size = 0
    while True:
        buf = source.read(_COPY_BUFSIZE)
        if not buf:
            break
        hasher.update(buf)
        dest.write(buf)
        size += len(buf)

    return base64.urlsafe_b64encode(hasher.digest()).decode("ascii").rstrip("="), size


def get_stream_length(source: BinaryIO) -> int:
    """Read a buffer while computing the content's size.

    :param source: buffer holding the source data
    :return: size of the contents
    """
    size = 0
    while True:
        buf = source.read(_COPY_BUFSIZE)
        if not buf:
            break
        size += len(buf)

    return size


def get_launcher_kind() -> "LauncherKind":  # pragma: no cover
    """Get the launcher kind for the current machine."""
    if os.name != "nt":
        return "posix"

    if "amd64" in sys.version.lower():
        return "win-amd64"
    if "(arm64)" in sys.version.lower():
        return "win-arm64"
    if "(arm)" in sys.version.lower():
        return "win-arm"
    if sys.platform == "win32":
        return "win-ia32"

    raise NotImplementedError("Unknown launcher kind for this machine")


@contextlib.contextmanager
def fix_shebang(stream: BinaryIO, interpreter: str) -> Iterator[BinaryIO]:
    """Replace ``#!python`` shebang in a stream with the correct interpreter.

    :param stream: stream to modify
    :param interpreter: "correct interpreter" to substitute the shebang with

    :returns: A context manager, that provides an appropriately modified stream.
    """
    stream.seek(0)
    if stream.read(8) == b"#!python":
        new_stream = io.BytesIO()
        # write our new shebang
        new_stream.write(f"#!{interpreter}\n".encode())
        # copy the rest of the stream
        stream.seek(0)
        stream.readline()  # skip first line
        while True:
            buf = stream.read(_COPY_BUFSIZE)
            if not buf:
                break
            new_stream.write(buf)
        new_stream.seek(0)
        yield new_stream
        new_stream.close()
    else:
        stream.seek(0)
        yield stream


def construct_record_file(
    records: Iterable[tuple[Scheme, "RecordEntry"]],
    prefix_for_scheme: Callable[[Scheme], str | None] = lambda _: None,
) -> BinaryIO:
    """Construct a RECORD file.

    :param records:
        ``records`` as passed into :any:`WheelDestination.finalize_installation`
    :param prefix_for_scheme:
        function to get a prefix to add for RECORD entries, within a scheme

    :return: A stream that can be written to file. Must be closed by the caller.
    """
    stream = io.TextIOWrapper(
        io.BytesIO(), encoding="utf-8", write_through=True, newline=""
    )
    writer = csv.writer(stream, delimiter=",", quotechar='"', lineterminator="\n")
    for scheme, record in sorted(records, key=lambda x: x[1].path):
        writer.writerow(record.to_row(prefix_for_scheme(scheme)))
    stream.seek(0)
    return stream.detach()


class InvalidEntryPoint(ValueError):
    """Raised when an entry point declaration is invalid.

    Carries the entry point ``group`` (e.g. ``console_scripts``) and the
    ``name`` of the offending declaration, when they could be determined.
    """

    def __init__(
        self, reason: str, *, group: str | None = None, name: str | None = None
    ) -> None:
        """Initialize an InvalidEntryPoint error.

        :param reason: why the declaration is invalid
        :param group: entry point group (section) of the declaration
        :param name: name of the offending declaration
        """
        self.reason = reason
        self.group = group
        self.name = name
        location = ""
        if group is not None:
            location = f" in group {group!r}"
            if name is not None:
                location = f" for {name!r} in group {group!r}"
        super().__init__(f"Invalid entry point{location}: {reason}")


# A "drive prefix" like ``C:`` at the start of a would-be file name.
_DRIVE_PREFIX_REGEX = re.compile(r"^[A-Za-z]:")

# A valid "extra" name, per PEP 508.
_EXTRA_NAME_REGEX = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")

# Overall shape of a strict entry point value: ``module:attr[.sub] [extras]``.
# Components are validated individually after this match.
_ENTRYPOINT_VALUE_REGEX = re.compile(
    r"""
    (?P<module>[^\s:\[\]]+?)
    \s*:\s*
    (?P<attrs>[^\s:\[\]]+?)
    \s*
    (?:\[\s*(?P<extras>[^\[\]]*?)\s*\])?
    \s*
    """,
    re.VERBOSE | re.UNICODE,
)


def _dotted_identifier_error(value: str, *, kind: str) -> str | None:
    """Return a reason why ``value`` is not valid dotted Python identifiers."""
    for segment in value.split("."):
        if not segment.isidentifier():
            return f"{kind} segment {segment!r} is not a valid Python identifier"
        if keyword.iskeyword(segment):
            return f"{kind} segment {segment!r} is a Python keyword"
    return None


def _script_name_error(name: str) -> str | None:
    """Return a reason why ``name`` is not a safe script (file) name."""
    if not name:
        return "script name must not be empty"
    if "\x00" in name:
        return "script name must not contain NUL bytes"
    if "\n" in name or "\r" in name:
        return "script name must not contain newlines"
    if "/" in name or "\\" in name:
        return "script name must not contain directory separators"
    if name in (".", ".."):
        return "script name must not be a dot directory"
    if _DRIVE_PREFIX_REGEX.match(name):
        return "script name must not start with a drive prefix"
    return None


def _validate_entrypoint_value(
    value: str, *, group: str, name: str
) -> tuple[str, str]:
    """Validate a strict entry point value, returning (module, attrs)."""
    if "%" in value:
        raise InvalidEntryPoint(
            "configuration interpolation syntax is not allowed",
            group=group,
            name=name,
        )
    match = _ENTRYPOINT_VALUE_REGEX.fullmatch(value)
    if match is None:
        raise InvalidEntryPoint(
            f"malformed declaration {value!r}, expected 'module:attr [extras]'",
            group=group,
            name=name,
        )

    module = match.group("module")
    error = _dotted_identifier_error(module, kind="module")
    if error is not None:
        raise InvalidEntryPoint(error, group=group, name=name)

    attrs = match.group("attrs")
    error = _dotted_identifier_error(attrs, kind="attribute")
    if error is not None:
        raise InvalidEntryPoint(error, group=group, name=name)

    extras = match.group("extras")
    if extras is not None:
        tokens = [token.strip() for token in extras.split(",")]
        if not all(
            token and _EXTRA_NAME_REGEX.fullmatch(token) for token in tokens
        ):
            raise InvalidEntryPoint(
                f"invalid extras declaration [{extras}]",
                group=group,
                name=name,
            )

    return module, attrs


def _parse_entrypoints_strict(
    text: str,
) -> list[tuple[str, str, str, "ScriptSection"]]:
    """Fully validate and parse ``entry_points.txt``-style contents.

    The entire file is validated before any entry is produced: either the
    returned list describes every declaration in the ``console_scripts`` and
    ``gui_scripts`` groups, or an :any:`InvalidEntryPoint` is raised and no
    entries are produced at all.
    """
    config = ConfigParser(delimiters="=", interpolation=None, strict=True)
    config.optionxform = str  # type: ignore[assignment, method-assign]
    try:
        config.read_string(text)
    except configparser.DuplicateSectionError as exc:
        raise InvalidEntryPoint(
            "duplicate section declaration", group=exc.section
        ) from exc
    except configparser.DuplicateOptionError as exc:
        raise InvalidEntryPoint(
            "duplicate entry point declaration", group=exc.section, name=exc.option
        ) from exc
    except configparser.Error as exc:
        raise InvalidEntryPoint(
            f"cannot parse file: {exc.message or exc!r}"
        ) from exc

    if config.defaults():
        name = next(iter(config.defaults()))
        raise InvalidEntryPoint(
            "a DEFAULT section is not allowed",
            group=config.default_section,
            name=name,
        )

    entries: list[tuple[str, str, str, "ScriptSection"]] = []
    seen: dict[str, str] = {}  # script name -> group that declared it
    for section in config.sections():
        if section not in ("console_scripts", "gui_scripts"):
            continue

        for name, value in config.items(section):
            error = _script_name_error(name)
            if error is not None:
                raise InvalidEntryPoint(error, group=section, name=name)

            module, attrs = _validate_entrypoint_value(value, group=section, name=name)

            if name in seen:
                raise InvalidEntryPoint(
                    f"already declared in group {seen[name]!r},"
                    " script names must be unique across groups",
                    group=section,
                    name=name,
                )
            seen[name] = section

            script_section = cast("ScriptSection", section[: -len("_scripts")])
            entries.append((name, module, attrs, script_section))

    return entries


def _parse_entrypoints_lenient(
    text: str,
) -> Iterable[tuple[str, str, str, "ScriptSection"]]:
    """Parse ``entry_points.txt``-style files, with the legacy lenient rules."""
    # Borrowed from https://github.com/python/importlib_metadata/blob/v3.4.0/importlib_metadata/__init__.py#L115
    config = ConfigParser(delimiters="=")
    config.optionxform = str  # type: ignore[assignment, method-assign]
    config.read_string(text)

    for section in config.sections():
        if section not in ["console_scripts", "gui_scripts"]:
            continue

        for name, value in config.items(section):
            assert isinstance(name, str)
            match = _ENTRYPOINT_REGEX.match(value)
            assert match

            module = match.group("module")
            assert isinstance(module, str)

            attrs = match.group("attrs")
            # TODO: make this a proper error, which can be caught.
            assert attrs is not None
            assert isinstance(attrs, str)

            script_section = cast("ScriptSection", section[: -len("_scripts")])

            yield name, module, attrs, script_section


def parse_entrypoints(
    text: str, *, strict: bool = True
) -> Iterable[tuple[str, str, str, "ScriptSection"]]:
    """Parse ``entry_points.txt``-style files.

    :param text: entire contents of the file
    :param strict:
        When true (the default), fully validate the ``console_scripts`` and
        ``gui_scripts`` groups before producing any entries, raising
        :any:`InvalidEntryPoint` on the first invalid declaration. Unrecognized
        groups are still ignored. When false, use the legacy lenient parsing
        behaviour, for compatibility with older callers.
    :return:
        name of the script, module to use, attribute to call, kind of script (cli / gui)
    """
    if strict:
        return _parse_entrypoints_strict(text)
    return _parse_entrypoints_lenient(text)


def _current_umask() -> int:
    """Get the current umask which involves having to set it temporarily."""
    mask = os.umask(0)
    os.umask(mask)
    return mask


# Borrowed from:
# https://github.com/pypa/pip/blob/0f21fb92/src/pip/_internal/utils/unpacking.py#L93
def make_file_executable(path: Path) -> None:
    """Make the file at the provided path executable."""
    path.chmod(0o777 & ~_current_umask() | 0o111)
