"""Tests for ``SchemeDictionaryDestination(strict_paths=True)``.

Every sample wheel used here is generated locally inside ``tmp_path``; no test
touches an existing project or a real system directory.
"""

import base64
import hashlib
import io
import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest

from installer import install
from installer.destinations import SchemeDictionaryDestination
from installer.exceptions import InvalidWheelSource, UnsafePathError
from installer.sources import WheelFile
from installer.utils import SCHEME_NAMES

DIST_INFO = "fancy-1.0.0.dist-info"


# ---------------------------------------------------------------------------
# Local wheel / source helpers
# ---------------------------------------------------------------------------
def _b64_hash(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def record_row(name: str, data: bytes) -> str:
    return f"{name},sha256={_b64_hash(data)},{len(data)}\n"


def build_wheel(
    tmp_path: Path,
    members: list[tuple[str, bytes]] | None = None,
    *,
    distribution: str = "fancy",
    version: str = "1.0.0",
    root_is_purelib: bool = True,
    entry_points: str | None = None,
    record_text: str | None = None,
) -> Path:
    """Create a local wheel whose RECORD is generated from the given members."""
    dist_info = f"{distribution}-{version}.dist-info"
    core: list[tuple[str, bytes]] = [
        (
            f"{dist_info}/WHEEL",
            (
                "Wheel-Version: 1.0\n"
                "Generator: test (1.0.0)\n"
                f"Root-Is-Purelib: {str(root_is_purelib).lower()}\n"
                "Tag: py3-none-any\n"
            ).encode(),
        ),
        (
            f"{dist_info}/METADATA",
            (
                "Metadata-Version: 2.1\n"
                f"Name: {distribution}\n"
                f"Version: {version}\n"
            ).encode(),
        ),
    ]
    if entry_points is not None:
        core.append((f"{dist_info}/entry_points.txt", entry_points.encode()))

    all_members = core + list(members or [])
    if record_text is None:
        record_text = "".join(
            record_row(name, data)
            for name, data in all_members
            if Path(name).name not in ("RECORD.jws", "RECORD.p7s")
        ) + f"{dist_info}/RECORD,,\n"

    wheel_path = tmp_path / f"{distribution}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w") as archive:
        for name, data in all_members:
            archive.writestr(name, data)
        archive.writestr(f"{dist_info}/RECORD", record_text.encode())
    return wheel_path


def scheme_paths(tmp_path: Path) -> dict[str, str]:
    paths = {}
    for scheme in SCHEME_NAMES:
        path = tmp_path / scheme
        path.mkdir()
        paths[scheme] = str(path)
    return paths


def make_destination(paths: dict[str, str], **kwargs) -> SchemeDictionaryDestination:
    kwargs.setdefault("interpreter", "/my/python")
    kwargs.setdefault("script_kind", "posix")
    return SchemeDictionaryDestination(paths, strict_paths=True, **kwargs)


def install_wheel(
    wheel: Path,
    paths: dict[str, str],
    *,
    additional_metadata: dict[str, bytes] | None = None,
    **destination_kwargs,
) -> SchemeDictionaryDestination:
    destination = make_destination(paths, **destination_kwargs)
    with WheelFile.open(wheel) as source:
        install(source, destination, additional_metadata or {})
    return destination


def assert_scheme_roots_empty(paths: dict[str, str]) -> None:
    for scheme, root in paths.items():
        assert list(Path(root).iterdir()) == [], f"{scheme} contains files"


class StaticSource:
    """Minimal WheelSource implementation for path spellings zipfile rejects."""

    validation_error = ValueError

    def __init__(self, distribution: str, version: str, files: list[tuple[str, bytes]]):
        from installer.sources import WheelSource

        self._source = WheelSource(distribution, version)
        self.distribution = distribution
        self.version = version
        self.files = files

    @property
    def dist_info_dir(self) -> str:
        return f"{self.distribution}-{self.version}.dist-info"

    @property
    def data_dir(self) -> str:
        return f"{self.distribution}-{self.version}.data"

    @property
    def dist_info_filenames(self) -> list[str]:
        return ["RECORD", "WHEEL"]

    def read_dist_info(self, filename: str) -> str:
        if filename == "WHEEL":
            return (
                "Wheel-Version: 1.0\n"
                "Root-Is-Purelib: true\n"
                "Tag: py3-none-any\n"
            )
        if filename == "RECORD":
            dist_info = self.dist_info_dir
            rows = [record_row(name, data) for name, data in self.files]
            rows.append(f"{dist_info}/RECORD,,\n")
            return "".join(rows)
        raise KeyError(filename)

    def validate_record(self) -> None:
        return None

    def get_contents(self):
        for name, data in self.files:
            record = (name, f"sha256={_b64_hash(data)}", str(len(data)))
            yield record, io.BytesIO(data), False

        record = (f"{self.dist_info_dir}/RECORD", "", "")
        yield record, io.BytesIO(b""), False


# ---------------------------------------------------------------------------
# Default behaviour and successful strict installs
# ---------------------------------------------------------------------------
def test_strict_paths_defaults_to_false(tmp_path):
    paths = scheme_paths(tmp_path)
    destination = SchemeDictionaryDestination(paths, "/my/python", "posix")
    assert destination.strict_paths is False


def test_strict_installs_normal_wheel(tmp_path):
    paths = scheme_paths(tmp_path)
    wheel = build_wheel(
        tmp_path,
        [
            ("fancy/__init__.py", b"# pure python\n"),
            (
                "fancy-1.0.0.data/data/fancy/data.py",
                b"# data scheme\n",
            ),
            (
                "fancy-1.0.0.data/scripts/fancy-script",
                b"#!python\nprint('script')\n",
            ),
        ],
        entry_points="[console_scripts]\nfancy = fancy:main\n",
        root_is_purelib=True,
    )

    install_wheel(
        wheel,
        paths,
        additional_metadata={"generated.txt": b"generated metadata"},
    )

    purelib = Path(paths["purelib"])
    scripts = Path(paths["scripts"])
    data = Path(paths["data"])

    assert (purelib / "fancy" / "__init__.py").read_bytes() == b"# pure python\n"
    assert (data / "fancy" / "data.py").read_bytes() == b"# data scheme\n"
    script = scripts / "fancy-script"
    assert script.read_bytes().startswith(b"#!/my/python\n")
    entry_point_script = scripts / "fancy"
    assert entry_point_script.is_file()
    assert entry_point_script.stat().st_mode & stat.S_IXUSR
    assert (purelib / DIST_INFO / "generated.txt").read_bytes() == b"generated metadata"
    assert (purelib / DIST_INFO / "RECORD").is_file()

    record = (purelib / DIST_INFO / "RECORD").read_text()
    assert f"{DIST_INFO}/RECORD,,\n" in record
    assert "fancy/__init__.py," in record


def test_strict_installs_with_destdir(tmp_path):
    paths = {scheme: f"/fake/prefix/{scheme}" for scheme in SCHEME_NAMES}
    destdir = tmp_path / "dest"
    wheel = build_wheel(tmp_path, [("fancy/__init__.py", b"x = 1\n")])

    install_wheel(wheel, paths, destdir=str(destdir))

    installed = destdir / "fake" / "prefix" / "purelib" / "fancy" / "__init__.py"
    assert installed.read_bytes() == b"x = 1\n"
    assert not Path("/fake/prefix/purelib/fancy/__init__.py").exists()


# ---------------------------------------------------------------------------
# Path spelling rejection
# ---------------------------------------------------------------------------
def test_strict_allows_trusted_scheme_root_symlink(tmp_path):
    """The scheme root itself is a caller-provided trusted starting point."""
    paths = scheme_paths(tmp_path)
    real_root = tmp_path / "real-purelib"
    real_root.mkdir()
    # Replace the purelib root with a symlink to another directory.
    Path(paths["purelib"]).rmdir()
    Path(paths["purelib"]).symlink_to(real_root)
    wheel = build_wheel(tmp_path, [("fancy/__init__.py", b"x = 1\n")])

    install_wheel(wheel, paths)

    assert (real_root / "fancy" / "__init__.py").read_bytes() == b"x = 1\n"


def test_strict_rejects_symlink_component_under_destdir(tmp_path):
    paths = {scheme: f"/fake/{scheme}" for scheme in SCHEME_NAMES}
    destdir = tmp_path / "dest"
    outside = tmp_path / "outside"
    outside.mkdir()
    (destdir / "fake").mkdir(parents=True)
    (destdir / "fake" / "purelib").symlink_to(outside)
    wheel = build_wheel(tmp_path, [("fancy/__init__.py", b"evil\n")])

    with pytest.raises(UnsafePathError, match="symbolic link"):
        install_wheel(wheel, paths, destdir=str(destdir))

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "bad_path",
    [
        pytest.param("/absolute/evil.py", id="absolute"),
        pytest.param("../escape.py", id="parent-escape"),
        pytest.param("pkg/../../escape.py", id="nested-parent-escape"),
        pytest.param("pkg/../other.py", id="any-parent-component"),
        pytest.param("bad\x00name.py", id="nul-byte"),
        pytest.param("..\\..\\escape.py", id="backslash"),
    ],
)
def test_strict_rejects_unsafe_paths_before_writing(tmp_path, bad_path):
    paths = scheme_paths(tmp_path)
    source = StaticSource(
        "fancy",
        "1.0.0",
        [("fancy/__init__.py", b"x = 1\n"), (bad_path, b"evil")],
    )
    destination = make_destination(paths)

    with pytest.raises(UnsafePathError):
        install(source, destination, {})

    assert_scheme_roots_empty(paths)


def test_strict_rejects_empty_path_before_writing(tmp_path):
    paths = scheme_paths(tmp_path)
    source = StaticSource("fancy", "1.0.0", [])
    # Replace the RECORD with a row containing an empty path.
    original_read = source.read_dist_info

    def read_dist_info(filename):
        if filename == "RECORD":
            return ",,\n"
        return original_read(filename)

    source.read_dist_info = read_dist_info
    destination = make_destination(paths)

    with pytest.raises(UnsafePathError):
        install(source, destination, {})

    assert_scheme_roots_empty(paths)


def test_strict_rejects_unsafe_entry_point_name(tmp_path):
    paths = scheme_paths(tmp_path)
    wheel = build_wheel(
        tmp_path,
        [("fancy/__init__.py", b"x = 1\n")],
        entry_points="[console_scripts]\n../evil = fancy:main\n",
    )

    with pytest.raises(UnsafePathError):
        install_wheel(wheel, paths)

    assert_scheme_roots_empty(paths)


# ---------------------------------------------------------------------------
# Conflict detection after scheme mapping
# ---------------------------------------------------------------------------
def test_strict_rejects_data_mapping_conflict(tmp_path):
    paths = scheme_paths(tmp_path)
    wheel = build_wheel(
        tmp_path,
        [
            ("fancy/__init__.py", b"from root\n"),
            (
                "fancy-1.0.0.data/purelib/fancy/__init__.py",
                b"from data\n",
            ),
        ],
    )

    with pytest.raises(UnsafePathError, match="same installation target"):
        install_wheel(wheel, paths)

    assert_scheme_roots_empty(paths)


def test_strict_rejects_normalised_path_conflict(tmp_path):
    paths = scheme_paths(tmp_path)
    wheel = build_wheel(
        tmp_path,
        [
            ("pkg/mod.py", b"one\n"),
            ("pkg/./mod.py", b"two\n"),
        ],
    )

    with pytest.raises((UnsafePathError, InvalidWheelSource)):
        install_wheel(wheel, paths)

    assert_scheme_roots_empty(paths)


def test_strict_rejects_record_target_conflict(tmp_path):
    paths = scheme_paths(tmp_path)
    wheel = build_wheel(
        tmp_path,
        [
            (
                f"fancy-1.0.0.data/purelib/{DIST_INFO}/RECORD",
                b"fake record\n",
            )
        ],
    )

    with pytest.raises(UnsafePathError, match="same installation target"):
        install_wheel(wheel, paths)

    assert_scheme_roots_empty(paths)


def test_strict_rejects_entry_script_conflict(tmp_path):
    paths = scheme_paths(tmp_path)
    wheel = build_wheel(
        tmp_path,
        [
            ("fancy/__init__.py", b"x = 1\n"),
            ("fancy-1.0.0.data/scripts/fancy", b"wheel script\n"),
        ],
        entry_points="[console_scripts]\nfancy = fancy:main\n",
    )

    with pytest.raises(UnsafePathError, match="same installation target"):
        install_wheel(wheel, paths)

    assert_scheme_roots_empty(paths)


# ---------------------------------------------------------------------------
# Existing-target and symlink handling
# ---------------------------------------------------------------------------
def test_strict_rejects_existing_symlink_component(tmp_path):
    paths = scheme_paths(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (Path(paths["purelib"]) / "pkg").symlink_to(outside)
    wheel = build_wheel(
        tmp_path,
        [("pkg/mod.py", b"evil\n")],
        entry_points="[console_scripts]\nfancy = fancy:main\n",
    )

    with pytest.raises(UnsafePathError, match="symbolic link"):
        install_wheel(wheel, paths)

    assert list(outside.iterdir()) == []
    assert list(Path(paths["scripts"]).iterdir()) == []


@pytest.mark.parametrize("overwrite_existing", [False, True])
def test_strict_rejects_dangling_final_symlink(tmp_path, overwrite_existing):
    paths = scheme_paths(tmp_path)
    outside_target = tmp_path / "outside-target.py"
    pkg = Path(paths["purelib"]) / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").symlink_to(outside_target)
    wheel = build_wheel(tmp_path, [("pkg/mod.py", b"evil\n")])

    with pytest.raises(UnsafePathError, match="symbolic link"):
        install_wheel(wheel, paths, overwrite_existing=overwrite_existing)

    assert not outside_target.exists()
    assert (pkg / "mod.py").is_symlink()


def test_strict_rejects_symlink_to_regular_file_with_overwrite(tmp_path):
    paths = scheme_paths(tmp_path)
    outside_target = tmp_path / "outside-file.py"
    outside_target.write_bytes(b"keep me\n")
    pkg = Path(paths["purelib"]) / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").symlink_to(outside_target)
    wheel = build_wheel(tmp_path, [("pkg/mod.py", b"evil\n")])

    with pytest.raises(UnsafePathError, match="symbolic link"):
        install_wheel(wheel, paths, overwrite_existing=True)

    assert outside_target.read_bytes() == b"keep me\n"


def test_strict_rejects_multiply_linked_file_with_overwrite(tmp_path):
    paths = scheme_paths(tmp_path)
    pkg = Path(paths["purelib"]) / "pkg"
    pkg.mkdir()
    target = pkg / "mod.py"
    target.write_bytes(b"original\n")
    os.link(target, pkg / "other-link")

    wheel = build_wheel(tmp_path, [("pkg/mod.py", b"evil\n")])

    with pytest.raises(UnsafePathError, match="multiply-linked"):
        install_wheel(wheel, paths, overwrite_existing=True)

    assert target.read_bytes() == b"original\n"
    assert (pkg / "other-link").read_bytes() == b"original\n"


def test_strict_write_file_itself_does_not_follow_links(tmp_path):
    """Direct writes must also be safe, even without install() preflight."""
    paths = scheme_paths(tmp_path)
    outside_target = tmp_path / "outside-file.py"
    outside_target.write_bytes(b"keep me\n")
    pkg = Path(paths["purelib"]) / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").symlink_to(outside_target)
    destination = make_destination(paths, overwrite_existing=True)

    with pytest.raises(UnsafePathError, match="symbolic link"):
        destination.write_file(
            "purelib", "pkg/mod.py", io.BytesIO(b"evil\n"), is_executable=False
        )

    assert outside_target.read_bytes() == b"keep me\n"


def test_strict_open_does_not_follow_link_introduced_after_preflight(
    tmp_path, monkeypatch
):
    """Simulate a link appearing after the preflight check."""
    paths = scheme_paths(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    pkg = Path(paths["purelib"]) / "pkg"
    pkg.mkdir()
    wheel = build_wheel(tmp_path, [("pkg/mod.py", b"evil\n")])

    # Disable only the existing-target preflight; record validation and path
    # checks still run. Then introduce a symlink before the write happens.
    monkeypatch.setattr(
        SchemeDictionaryDestination, "_preflight_writes", lambda self, planned: None
    )
    (pkg / "mod.py").symlink_to(outside / "mod.py")

    with pytest.raises(UnsafePathError, match="symbolic link"):
        install_wheel(wheel, paths)

    assert list(outside.iterdir()) == []


# ---------------------------------------------------------------------------
# RECORD integrity
# ---------------------------------------------------------------------------
def test_strict_rejects_record_hash_mismatch_before_writing(tmp_path):
    paths = scheme_paths(tmp_path)
    good = b"good contents\n"
    record_text = f"fancy/__init__.py,sha256={'A' * 43},18\n{DIST_INFO}/RECORD,,\n"
    wheel = build_wheel(
        tmp_path,
        [("fancy/__init__.py", good)],
        entry_points="[console_scripts]\nfancy = fancy:main\n",
        record_text=record_text,
    )

    with pytest.raises(InvalidWheelSource, match="didn't match RECORD"):
        install_wheel(wheel, paths)

    assert_scheme_roots_empty(paths)


def test_strict_rejects_member_missing_from_record(tmp_path):
    paths = scheme_paths(tmp_path)
    wheel = build_wheel(
        tmp_path,
        [("fancy/__init__.py", b"x = 1\n")],
        record_text=f"{DIST_INFO}/RECORD,,\n",
    )

    with pytest.raises(InvalidWheelSource, match="not mentioned in RECORD"):
        install_wheel(wheel, paths)

    assert_scheme_roots_empty(paths)


def test_strict_rejects_record_listing_missing_member(tmp_path):
    paths = scheme_paths(tmp_path)
    data = b"missing\n"
    record_text = (
        f"fancy/__init__.py,sha256={_b64_hash(b'x = 1\\n')},6\n"
        f"fancy/missing.py,sha256={_b64_hash(data)},{len(data)}\n"
        f"{DIST_INFO}/RECORD,,\n"
    )
    wheel = build_wheel(
        tmp_path,
        [("fancy/__init__.py", b"x = 1\n")],
        record_text=record_text,
    )

    with pytest.raises(InvalidWheelSource, match="not present in the wheel"):
        install_wheel(wheel, paths)

    assert_scheme_roots_empty(paths)


def test_strict_rejects_record_self_hash(tmp_path):
    paths = scheme_paths(tmp_path)
    wheel = build_wheel(
        tmp_path,
        [("fancy/__init__.py", b"x = 1\n")],
        record_text=(
            f"fancy/__init__.py,sha256={_b64_hash(b'x = 1\\n')},6\n"
            f"{DIST_INFO}/RECORD,sha256={_b64_hash(b'fake')},4\n"
        ),
    )

    with pytest.raises(InvalidWheelSource, match="incorrectly contains hash"):
        install_wheel(wheel, paths)

    assert_scheme_roots_empty(paths)


def test_strict_allows_record_signature_files_absent_from_record(tmp_path):
    paths = scheme_paths(tmp_path)
    wheel = build_wheel(
        tmp_path,
        [
            ("fancy/__init__.py", b"x = 1\n"),
            (f"{DIST_INFO}/RECORD.jws", b'{"hash": "sha256=ignored"}'),
        ],
    )

    install_wheel(wheel, paths)

    assert (Path(paths["purelib"]) / "fancy" / "__init__.py").is_file()
    # Signature files absent from RECORD are accepted and installed as before.
    assert (Path(paths["purelib"]) / DIST_INFO / "RECORD.jws").is_file()


# ---------------------------------------------------------------------------
# Existing-target compatibility and old interfaces
# ---------------------------------------------------------------------------
def test_strict_false_keeps_legacy_path_behaviour(tmp_path):
    paths = scheme_paths(tmp_path)
    destination = SchemeDictionaryDestination(paths, "/my/python", "posix")
    with pytest.raises(ValueError, match="outside of the target directory"):
        destination._path_with_destdir("purelib", "subdir/../../outside.txt")


def test_strict_rejects_existing_regular_file_without_overwrite(tmp_path):
    paths = scheme_paths(tmp_path)
    pkg = Path(paths["purelib"]) / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_bytes(b"original\n")
    wheel = build_wheel(tmp_path, [("pkg/mod.py", b"evil\n")])

    with pytest.raises(FileExistsError):
        install_wheel(wheel, paths)

    assert (pkg / "mod.py").read_bytes() == b"original\n"


def test_strict_overwrites_single_link_regular_file_when_allowed(tmp_path):
    paths = scheme_paths(tmp_path)
    pkg = Path(paths["purelib"]) / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_bytes(b"original\n")
    wheel = build_wheel(tmp_path, [("pkg/mod.py", b"replacement\n")])

    install_wheel(wheel, paths, overwrite_existing=True)

    assert (pkg / "mod.py").read_bytes() == b"replacement\n"


def test_strict_does_not_add_parameters_to_custom_destination(tmp_path):
    from installer.destinations import WheelDestination

    class LegacyDestination(WheelDestination):
        def __init__(self):
            self.paths = []

        def write_script(self, name, module, attr, section):
            self.paths.append(("script", name))
            from installer.records import RecordEntry

            return RecordEntry(name, None, None)

        def write_file(self, scheme, path, stream, is_executable):
            self.paths.append((scheme, os.fspath(path)))
            from installer.records import RecordEntry

            return RecordEntry(os.fspath(path), None, None)

        def finalize_installation(self, scheme, record_file_path, records):
            self.paths.append(("record", record_file_path))

    wheel = build_wheel(tmp_path, [("fancy/__init__.py", b"x = 1\n")])
    destination = LegacyDestination()
    with WheelFile.open(wheel) as source:
        install(source, destination, {})

    assert destination.paths


# ---------------------------------------------------------------------------
# POSIX-specific guarantee: directory-fd based writes are actually used
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "posix", reason="POSIX-specific behaviour")
def test_strict_uses_directory_fd_open(tmp_path, monkeypatch):
    import installer.destinations as destinations_module

    assert destinations_module._DIR_FD_SUPPORTED
    seen = []
    real_open = os.open

    def recording_open(path, flags, *args, **kwargs):
        if kwargs.get("dir_fd") is not None:
            seen.append(os.fspath(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    paths = scheme_paths(tmp_path)
    wheel = build_wheel(tmp_path, [("fancy/__init__.py", b"x = 1\n")])
    install_wheel(wheel, paths)

    assert "fancy" in seen
    assert "__init__.py" in seen


# ---------------------------------------------------------------------------
# Bytecode compilation is not performed through unsafe path-based writes
# ---------------------------------------------------------------------------
def test_strict_skips_bytecode_compilation(tmp_path, recwarn):
    paths = scheme_paths(tmp_path)
    wheel = build_wheel(tmp_path, [("fancy/__init__.py", b"x = 1\n")])

    install_wheel(wheel, paths, bytecode_optimization_levels=(0, 1))

    assert not list(Path(paths["purelib"]).rglob("*.pyc"))
    assert any("strict_paths" in str(w.message) for w in recwarn)


# Keep the import used on Python versions where os.supports_dir_fd is checked.
if sys.version_info < (3, 10):  # pragma: no cover
    raise RuntimeError("installer requires Python 3.10+")
