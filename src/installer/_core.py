"""Core wheel installation logic."""

import posixpath
import warnings
from io import BytesIO
from typing import cast

from installer.destinations import WheelDestination
from installer.exceptions import InvalidWheelSource, UnsafePathError
from installer.records import InvalidRecordEntry, RecordEntry, parse_record_file
from installer.sources import WheelSource
from installer.utils import (
    SCHEME_NAMES,
    Scheme,
    parse_entrypoints,
    parse_metadata_file,
    validate_installation_path,
)

__all__ = ["install"]


def _process_WHEEL_file(source: WheelSource) -> Scheme:  # noqa: N802
    """Process the WHEEL file, from ``source``.

    Returns the scheme that the archive root should go in.
    """
    stream = source.read_dist_info("WHEEL")
    metadata = parse_metadata_file(stream)

    # Ensure compatibility with this wheel version.
    if not (metadata["Wheel-Version"] and metadata["Wheel-Version"].startswith("1.")):
        message = "Incompatible Wheel-Version {}, only support version 1.x wheels."
        raise InvalidWheelSource(source, message.format(metadata["Wheel-Version"]))

    # Determine where archive root should go.
    if metadata["Root-Is-Purelib"] == "true":
        return cast("Scheme", "purelib")
    else:
        return cast("Scheme", "platlib")


def _determine_scheme(
    path: str, source: WheelSource, root_scheme: Scheme
) -> tuple[Scheme, str]:
    """Determine which scheme to place given path in, from source."""
    data_dir = source.data_dir

    # If it's in not `{distribution}-{version}.data`, then it's in root_scheme.
    if posixpath.commonpath([data_dir, path]) != data_dir:
        return root_scheme, path

    # Figure out which scheme this goes to.
    parts = []
    scheme_name = None
    left = path
    while True:
        left, right = posixpath.split(left)
        parts.append(right)
        if left == source.data_dir:
            scheme_name = right
            break

    if scheme_name not in SCHEME_NAMES:
        msg_fmt = "{path} is not contained in a valid .data subdirectory."
        raise InvalidWheelSource(source, msg_fmt.format(path=path))

    relative_path = (
        posixpath.join(*reversed(parts[:-1])) if parts[:-1] else ""
    )
    return cast("Scheme", scheme_name), relative_path


def _is_signature_path(path: str, dist_info_dir: str) -> bool:
    """Whether ``path`` is a RECORD signature file within the dist-info dir."""
    return (
        path.rsplit("/", 1)[-1] in ("RECORD.p7s", "RECORD.jws")
        and posixpath.commonpath([dist_info_dir, path]) == dist_info_dir
    )


def _is_pycache_path(path: str) -> bool:
    return "__pycache__" in path.split("/")[:-1]


def _strict_validate_record(source: WheelSource) -> dict[str, RecordEntry]:
    """Validate the entire RECORD and return its entries keyed by normalised path."""
    try:
        record_text = source.read_dist_info("RECORD")
    except Exception as exc:
        raise InvalidWheelSource(
            source, f"Unable to retrieve `RECORD` from {source!r}: {exc!r}"
        ) from exc

    try:
        rows = list(parse_record_file(record_text.splitlines()))
    except InvalidRecordEntry as exc:
        raise InvalidWheelSource(
            source, f"Unable to parse `RECORD` from {source!r}: {exc}"
        ) from exc

    entries: dict[str, RecordEntry] = {}
    issues: list[str] = []

    for row in rows:
        # Validate the path first: unsafe paths raise UnsafePathError, which is
        # also a ValueError for callers that rely on that.
        normalized = validate_installation_path(row[0])
        try:
            entry = RecordEntry.from_elements(*row)
        except InvalidRecordEntry as exc:
            issues.extend(exc.issues)
            continue

        if normalized in entries:
            issues.append(
                f"`RECORD` contains duplicate entries for path "
                f"{normalized!r}: {entries[normalized].path!r} and {entry.path!r}"
            )
            continue
        entries[normalized] = entry

    if issues:
        raise InvalidWheelSource(source, "\n".join(issues))

    return entries


def _strict_preflight(
    source: WheelSource,
    destination: WheelDestination,
    root_scheme: Scheme,
    record_file_path: str,
    additional_metadata: dict[str, bytes],
) -> None:
    """Validate everything that can be known before any file is written.

    This checks ``RECORD`` integrity, every member path, scheme-mapped target
    conflicts (including entry point scripts and the final ``RECORD``), and the
    state of existing targets.
    """
    record_entries = _strict_validate_record(source)
    record_target = validate_installation_path(record_file_path)

    planned: list[tuple[Scheme, str, str]] = []
    issues: list[str] = []
    matched: set[str] = set()
    seen_members: dict[str, str] = {}

    for record_elements, stream, _is_executable in source.get_contents():
        member_path = record_elements[0]
        normalized = validate_installation_path(member_path)

        if normalized in seen_members:
            issues.append(
                f"Wheel contains duplicate member paths that normalise to "
                f"{normalized!r}: {seen_members[normalized]!r} and "
                f"{member_path!r}"
            )
            continue
        seen_members[normalized] = member_path

        entry = record_entries.get(normalized)
        is_signature = _is_signature_path(normalized, source.dist_info_dir)
        if entry is None:
            if not is_signature:
                issues.append(f"{member_path} is not mentioned in RECORD")
                continue
            # Signature files are allowed to be absent from RECORD. They are
            # still written, so they remain part of the plan below.
        else:
            matched.add(normalized)
            if is_signature:
                issues.append(
                    f"Digital signature file {member_path} is incorrectly "
                    f"contained in RECORD."
                )
                continue

            if normalized == record_target:
                if entry.hash_ is not None or entry.size is not None:
                    issues.append(
                        "RECORD file incorrectly contains hash / size."
                    )
                # The RECORD itself is regenerated at the end; it is only part
                # of the write plan through the explicit entry added below.
                continue

            if entry.hash_ is None or entry.size is None:
                issues.append(
                    f"hash / size of {member_path} is not included in RECORD"
                )
            elif not entry.validate_stream(stream):
                issues.append(
                    f"hash / size of {member_path} didn't match RECORD"
                )

        if normalized == record_target or _is_pycache_path(normalized):
            continue

        scheme, destination_path = _determine_scheme(
            path=normalized, source=source, root_scheme=root_scheme
        )
        destination_path = validate_installation_path(destination_path)
        planned.append(
            (scheme, destination_path, f"Wheel member {member_path!r}")
        )

    if record_target not in record_entries:
        issues.append("RECORD is not mentioned in RECORD")
    for normalized in set(record_entries) - matched:
        issues.append(
            f"{record_entries[normalized].path} is listed in RECORD but is "
            f"not present in the wheel"
        )

    if issues:
        raise InvalidWheelSource(source, "\n".join(issues))

    # Entry point scripts participate in target conflict detection.
    if "entry_points.txt" in source.dist_info_filenames:
        entrypoints_text = source.read_dist_info("entry_points.txt")
        for name, _module, _attr, _section in parse_entrypoints(entrypoints_text):
            normalized_name = validate_installation_path(name)
            script_name = destination._script_file_name(  # type: ignore[attr-defined]
                normalized_name
            )
            script_name = validate_installation_path(script_name)
            planned.append(
                (Scheme("scripts"), script_name, f"Entry point {name!r}")
            )

    # Additional generated metadata is part of the plan too.
    for filename in additional_metadata:
        path = posixpath.join(source.dist_info_dir, filename)
        normalized_path = validate_installation_path(path)
        planned.append(
            (
                root_scheme,
                normalized_path,
                f"Additional metadata {filename!r}",
            )
        )

    # The regenerated RECORD participates in target conflict detection.
    planned.append((root_scheme, record_target, "RECORD file"))

    destination._preflight_writes(planned)  # type: ignore[attr-defined]


def install(
    source: WheelSource,
    destination: WheelDestination,
    additional_metadata: dict[str, bytes],
) -> None:
    """Install wheel described by ``source`` into ``destination``.

    :param source: wheel to install.
    :param destination: where to write the wheel.
    :param additional_metadata: additional metadata files to generate, usually
                                generated by the caller.

    """
    root_scheme = _process_WHEEL_file(source)

    # RECORD handling
    record_file_path = posixpath.join(source.dist_info_dir, "RECORD")
    strict = getattr(destination, "strict_paths", False) is True
    record_target = (
        validate_installation_path(record_file_path)
        if strict
        else record_file_path
    )

    if strict:
        # Run the entire preflight before generating any entry point scripts or
        # writing any files. Nothing is written if this raises.
        _strict_preflight(
            source=source,
            destination=destination,
            root_scheme=root_scheme,
            record_file_path=record_file_path,
            additional_metadata=additional_metadata,
        )

    written_records = []

    # Write the entry_points based scripts.
    if "entry_points.txt" in source.dist_info_filenames:
        entrypoints_text = source.read_dist_info("entry_points.txt")
        for name, module, attr, section in parse_entrypoints(entrypoints_text):
            record = destination.write_script(
                name=name,
                module=module,
                attr=attr,
                section=section,
            )
            written_records.append((Scheme("scripts"), record))

    # Write all the files from the wheel.
    for record_elements, stream, is_executable in source.get_contents():
        source_record = RecordEntry.from_elements(*record_elements)
        path = source_record.path
        if strict:
            path = validate_installation_path(path)

        # Skip the RECORD, which is written at the end, based on this info.
        if path == record_target:
            continue

        if "__pycache__" in path.split("/")[:-1]:
            warnings.warn(
                (
                    f"Skip installing {path} from {source.distribution}."
                    " Installing files in a __pycache__ directory poses a security risk."
                    " __pycache__ directories should not be included in wheels."
                    f" This is probably an issue in the build process of '{source.distribution}'."
                ),
                RuntimeWarning,
                stacklevel=2,
            )
            continue

        # Figure out where to write this file.
        scheme, destination_path = _determine_scheme(
            path=path,
            source=source,
            root_scheme=root_scheme,
        )
        if strict:
            destination_path = validate_installation_path(destination_path)
        record = destination.write_file(
            scheme=scheme,
            path=destination_path,
            stream=stream,
            is_executable=is_executable,
        )
        written_records.append((scheme, record))

    # Write all the installation-specific metadata
    for filename, contents in additional_metadata.items():
        path = posixpath.join(source.dist_info_dir, filename)
        if strict:
            path = validate_installation_path(path)

        with BytesIO(contents) as other_stream:
            record = destination.write_file(
                scheme=root_scheme,
                path=path,
                stream=other_stream,
                is_executable=False,
            )
        written_records.append((root_scheme, record))

    written_records.append((root_scheme, RecordEntry(record_target, None, None)))
    destination.finalize_installation(
        scheme=root_scheme,
        record_file_path=record_target,
        records=written_records,
    )
