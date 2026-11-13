import builtins
import hashlib
import json
import pathlib
import posixpath
import zipfile
from base64 import urlsafe_b64encode
from hashlib import sha256
from zipfile import ZipExtFile

import pytest

from installer.exceptions import InstallerError
from installer.records import parse_record_file
from installer.sources import (
    _VALIDATION_CHUNK_SIZE,
    WheelFile,
    WheelSource,
)


class TestWheelSource:
    def test_takes_two_arguments(self):
        WheelSource("distribution", "version")
        WheelSource(distribution="distribution", version="version")

    def test_correctly_computes_properties(self):
        source = WheelSource(distribution="distribution", version="version")

        assert source.data_dir == "distribution-version.data"
        assert source.dist_info_dir == "distribution-version.dist-info"

    def test_raises_not_implemented_error(self):
        source = WheelSource(distribution="distribution", version="version")

        with pytest.raises(NotImplementedError):
            _ = source.dist_info_filenames

        with pytest.raises(NotImplementedError):
            source.read_dist_info("METADATA")

        with pytest.raises(NotImplementedError):
            source.get_contents()

        with pytest.raises(NotImplementedError):
            source.validate_record()


def replace_file_in_zip(path: str, filename: str, content: "str | None") -> None:
    """Helper function for replacing a file in the zip.

    Exists because ZipFile doesn't support remove.
    """
    files = {}
    # Copy everything except `filename`, and replace it with `content`.
    with zipfile.ZipFile(path) as archive:
        for file in archive.namelist():
            if file == filename:
                if content is None:
                    continue  # Remove the file
                files[file] = content.encode()
            else:
                files[file] = archive.read(file)
    # Replace original archive
    with zipfile.ZipFile(path, mode="w") as archive:
        for name, file_content in files.items():
            archive.writestr(name, file_content)


def make_record_line(path: str, data: bytes) -> str:
    """Build one CSV line of a RECORD file for `data`."""
    digest = urlsafe_b64encode(sha256(data).digest()).decode("ascii").rstrip("=")
    return f"{path},sha256={digest},{len(data)}"


def build_wheel(
    path: pathlib.Path, files: dict[str, bytes], record_lines: list[str]
) -> None:
    """Write a wheel with the given members and an explicit RECORD body."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        # RECORD always lives inside the wheel's dist-info directory.
        archive.writestr(
            "fancy-1.0.0.dist-info/RECORD", "\n".join(record_lines) + "\n"
        )


def make_simple_wheel(
    path: pathlib.Path,
    *,
    extra_files: dict[str, bytes] | None = None,
    record_excludes: set[str] | None = None,
) -> tuple[dict[str, bytes], list[str]]:
    """Generate a small, fully valid wheel from local data.

    ``extra_files`` adds members; names in ``record_excludes`` are written to
    the ZIP but deliberately left out of RECORD. Returns the mapping of
    member name to contents and the RECORD lines used.
    """
    excludes = set(record_excludes or ())
    files = {
        "fancy/__init__.py": b"def main():\n    print('hello')\n",
        "fancy-1.0.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: fancy\n",
        "fancy-1.0.0.dist-info/WHEEL": b"Wheel-Version: 1.0\n",
    }
    if extra_files:
        files.update(extra_files)
    record_lines = [
        make_record_line(name, data)
        for name, data in files.items()
        if not name.endswith(".dist-info/RECORD") and name not in excludes
    ]
    record_lines.append("fancy-1.0.0.dist-info/RECORD,,")
    build_wheel(path, files, record_lines)
    return files, record_lines


def corrupt_member(path: pathlib.Path, filename: str, replacement: bytes) -> None:
    """Overwrite a stored member's bytes without updating its CRC header."""
    with open(path, "rb") as f:
        raw = f.read()
    payload: bytes | None = None
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(filename)
        assert info.compress_type == zipfile.ZIP_STORED
        with archive.open(filename) as stream:
            payload = stream.read()
    assert payload is not None and len(replacement) == len(payload)
    # The stored payload is unique enough within this tiny archive.
    corrupted = raw.replace(payload, replacement, 1)
    assert corrupted != raw
    with open(path, "wb") as f:
        f.write(corrupted)


def duplicate_zip_member(path: pathlib.Path, filename: str) -> None:
    """Append a second copy of `filename` to the ZIP."""
    with zipfile.ZipFile(path) as archive:
        data = archive.read(filename)
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(filename, data)


class TestWheelFile:
    def test_rejects_not_okay_name(self, tmp_path):
        # Create an empty zipfile
        path = tmp_path / "not_a_valid_name.whl"
        with zipfile.ZipFile(str(path), "w"):
            pass

        with (
            pytest.raises(ValueError, match=r"Not a valid wheel filename: .+"),
            WheelFile.open(str(path)),
        ):
            pass

    def test_provides_correct_dist_info_filenames(self, fancy_wheel):
        with WheelFile.open(fancy_wheel) as source:
            assert sorted(source.dist_info_filenames) == [
                "METADATA",
                "RECORD",
                "WHEEL",
                "entry_points.txt",
                "top_level.txt",
            ]

    def test_correctly_reads_from_dist_info_files(self, fancy_wheel):
        files = {}
        with zipfile.ZipFile(fancy_wheel) as archive:
            for file in archive.namelist():
                if ".dist-info" not in file:
                    continue
                files[posixpath.basename(file)] = archive.read(file).decode("utf-8")

        got_files = {}
        with WheelFile.open(fancy_wheel) as source:
            for file in files:
                got_files[file] = source.read_dist_info(file)

        assert got_files == files

    def test_provides_correct_contents(self, fancy_wheel):
        # Know the contents of the wheel
        files = {}
        with zipfile.ZipFile(fancy_wheel) as archive:
            for file in archive.namelist():
                if file[-1:] == "/":
                    continue
                files[file] = archive.read(file)

        expected_record_lines = (
            files["fancy-1.0.0.dist-info/RECORD"].decode("utf-8").splitlines()
        )
        expected_records = list(parse_record_file(expected_record_lines))

        # Check that the object's output is appropriate
        got_records = []
        got_files = {}
        with WheelFile.open(fancy_wheel) as source:
            for record_elements, stream, is_executable in source.get_contents():
                got_records.append(record_elements)
                got_files[record_elements[0]] = stream.read()
                assert not is_executable

        assert sorted(got_records) == sorted(expected_records)
        assert got_files == files

    def test_finds_dist_info(self, fancy_wheel):
        denorm = fancy_wheel.rename(fancy_wheel.parent / "Fancy-1.0.0-py3-none-any.whl")
        # Python 3.7: rename doesn't return the new name:
        denorm = fancy_wheel.parent / "Fancy-1.0.0-py3-none-any.whl"
        with WheelFile.open(denorm) as source:
            assert source.dist_info_filenames

    def test_requires_dist_info_name_match(self, fancy_wheel):
        misnamed = fancy_wheel.rename(
            fancy_wheel.parent / "misnamed-1.0.0-py3-none-any.whl"
        )
        # Python 3.7: rename doesn't return the new name:
        misnamed = fancy_wheel.parent / "misnamed-1.0.0-py3-none-any.whl"
        with pytest.raises(InstallerError) as ctx, WheelFile.open(misnamed) as source:
            _ = source.dist_info_filenames

        error = ctx.value
        assert error.filename == str(misnamed)
        assert error.dist_info == "fancy-1.0.0.dist-info"
        assert "" in error.reason
        assert error.dist_info in str(error)

    def test_enforces_single_dist_info(self, fancy_wheel):
        with zipfile.ZipFile(fancy_wheel, "a") as archive:
            archive.writestr(
                "name-1.0.0.dist-info/random.txt",
                b"This is a random file.",
            )

        with (
            pytest.raises(InstallerError) as ctx,
            WheelFile.open(fancy_wheel) as source,
        ):
            _ = source.dist_info_filenames

        error = ctx.value
        assert error.filename == str(fancy_wheel)
        assert error.dist_info == str(["fancy-1.0.0.dist-info", "name-1.0.0.dist-info"])
        assert "exactly one .dist-info" in error.reason
        assert error.dist_info in str(error)

    def test_rejects_no_record_on_validate(self, fancy_wheel):
        # Remove RECORD
        replace_file_in_zip(
            fancy_wheel,
            filename="fancy-1.0.0.dist-info/RECORD",
            content=None,
        )
        with (
            WheelFile.open(fancy_wheel) as source,
            pytest.raises(
                WheelFile.validation_error, match="Unable to retrieve `RECORD`"
            ),
        ):
            source.validate_record(validate_contents=False)

    def test_rejects_invalid_record_entry(self, fancy_wheel):
        # Strip the "sha256=" prefix from each hash column, leaving rows with
        # a malformed hash value. The issue must surface through `.issues`
        # rather than aborting the whole parse with a retrieval error.
        with WheelFile.open(fancy_wheel) as source:
            record_file_contents = source.read_dist_info("RECORD")

        broken_lines = []
        for line in record_file_contents.splitlines():
            path, hash_, size = line.split(",")
            if hash_.startswith("sha256="):
                hash_ = hash_[len("sha256=") :]
            broken_lines.append(",".join((path, hash_, size)))

        replace_file_in_zip(
            fancy_wheel,
            filename="fancy-1.0.0.dist-info/RECORD",
            content="\n".join(broken_lines),
        )
        with (
            WheelFile.open(fancy_wheel) as source,
            pytest.raises(WheelFile.validation_error) as exc_info,
        ):
            source.validate_record()

        issues = exc_info.value.issues
        assert len(issues) == 7  # one issue per regular file in RECORD
        assert all(
            "is invalid: `hash` does not follow the required format" in issue
            for issue in issues
        )
        # The RECORD row itself stays valid and never shows up as an issue.
        assert not any(
            "fancy-1.0.0.dist-info/RECORD " in issue or "RECORD is" in issue
            for issue in issues
        )

    def test_rejects_record_missing_file_on_validate(self, fancy_wheel):
        with WheelFile.open(fancy_wheel) as source:
            record_file_contents = source.read_dist_info("RECORD")

        # Remove the first two entries from the RECORD file
        new_record_file_contents = "\n".join(record_file_contents.split("\n")[2:])
        replace_file_in_zip(
            fancy_wheel,
            filename="fancy-1.0.0.dist-info/RECORD",
            content=new_record_file_contents,
        )
        with (
            WheelFile.open(fancy_wheel) as source,
            pytest.raises(WheelFile.validation_error, match="not mentioned in RECORD"),
        ):
            source.validate_record(validate_contents=False)

    def test_rejects_record_missing_hash(self, fancy_wheel):
        with WheelFile.open(fancy_wheel) as source:
            record_file_contents = source.read_dist_info("RECORD")

        new_record_file_contents = "\n".join(
            line.split(",")[0] + ",,"  # file name with empty size and hash
            for line in record_file_contents.split("\n")
        )
        replace_file_in_zip(
            fancy_wheel,
            filename="fancy-1.0.0.dist-info/RECORD",
            content=new_record_file_contents,
        )
        with (
            WheelFile.open(fancy_wheel) as source,
            pytest.raises(
                WheelFile.validation_error,
                match=r"hash / size of (.+) is not included in RECORD",
            ),
        ):
            source.validate_record(validate_contents=False)

    def test_accept_wheel_with_signature_file(self, fancy_wheel):
        with WheelFile.open(fancy_wheel) as source:
            record_file_contents = source.read_dist_info("RECORD")
        hash_b64_nopad = (
            urlsafe_b64encode(sha256(record_file_contents.encode()).digest())
            .decode("utf-8")
            .rstrip("=")
        )
        jws_content = json.dumps({"hash": f"sha256={hash_b64_nopad}"})
        with zipfile.ZipFile(fancy_wheel, "a") as archive:
            archive.writestr("fancy-1.0.0.dist-info/RECORD.jws", jws_content)
        with WheelFile.open(fancy_wheel) as source:
            source.validate_record()

    def test_reject_signature_file_in_record(self, fancy_wheel):
        with WheelFile.open(fancy_wheel) as source:
            record_file_contents = source.read_dist_info("RECORD")
        record_hash_nopad = (
            urlsafe_b64encode(sha256(record_file_contents.encode()).digest())
            .decode("utf-8")
            .rstrip("=")
        )
        jws_content = json.dumps({"hash": f"sha256={record_hash_nopad}"})
        with zipfile.ZipFile(fancy_wheel, "a") as archive:
            archive.writestr("fancy-1.0.0.dist-info/RECORD.jws", jws_content)

        # Add signature file to RECORD
        jws_content = jws_content.encode()
        jws_hash_nopad = (
            urlsafe_b64encode(sha256(jws_content).digest()).decode("utf-8").rstrip("=")
        )
        replace_file_in_zip(
            fancy_wheel,
            filename="fancy-1.0.0.dist-info/RECORD",
            content=record_file_contents.rstrip("\n")
            + f"\nfancy-1.0.0.dist-info/RECORD.jws,sha256={jws_hash_nopad},{len(jws_content)}\n",
        )
        with (
            WheelFile.open(fancy_wheel) as source,
            pytest.raises(
                WheelFile.validation_error,
                match=r"digital signature file (.+) is incorrectly contained in RECORD.",
            ),
        ):
            source.validate_record(validate_contents=False)

    def test_rejects_record_contain_self_hash(self, fancy_wheel):
        with WheelFile.open(fancy_wheel) as source:
            record_file_contents = source.read_dist_info("RECORD")

        new_record_file_lines = []
        for line in record_file_contents.split("\n"):
            if not line:
                continue
            filename, hash_, size = line.split(",")
            if filename.split("/")[-1] == "RECORD":
                hash_ = "sha256=pREiHcl39jRySUXMCOrwmSsnOay8FB7fOJP5mZQ3D3A"
                size = str(len(record_file_contents))
            new_record_file_lines.append(f"{filename},{hash_},{size}")

        replace_file_in_zip(
            fancy_wheel,
            filename="fancy-1.0.0.dist-info/RECORD",
            content="\n".join(new_record_file_lines),
        )
        with (
            WheelFile.open(fancy_wheel) as source,
            pytest.raises(
                WheelFile.validation_error,
                match=r"RECORD file incorrectly contains hash / size.",
            ),
        ):
            source.validate_record(validate_contents=False)

    def test_rejects_record_validation_failed(self, fancy_wheel):
        with WheelFile.open(fancy_wheel) as source:
            record_file_contents = source.read_dist_info("RECORD")

        new_record_file_lines = []
        for line in record_file_contents.split("\n"):
            if not line:
                continue
            filename, hash_, size = line.split(",")
            if filename.split("/")[-1] != "RECORD":
                hash_ = "sha256=pREiHcl39jRySUXMCOrwmSsnOay8FB7fOJP5mZQ3D3A"
            new_record_file_lines.append(f"{filename},{hash_},{size}")

        replace_file_in_zip(
            fancy_wheel,
            filename="fancy-1.0.0.dist-info/RECORD",
            content="\n".join(new_record_file_lines),
        )
        with (
            WheelFile.open(fancy_wheel) as source,
            pytest.raises(
                WheelFile.validation_error,
                match=r"hash / size of (.+) didn't match RECORD",
            ),
        ):
            source.validate_record()

    def test_rejects_record_containing_unknown_hash(self, fancy_wheel):
        with WheelFile.open(fancy_wheel) as source:
            record_file_contents = source.read_dist_info("RECORD")

        new_record_file_contents = record_file_contents.replace("sha256=", "sha=")
        replace_file_in_zip(
            fancy_wheel,
            filename="fancy-1.0.0.dist-info/RECORD",
            content=new_record_file_contents,
        )

        with (
            WheelFile.open(fancy_wheel) as source,
            pytest.raises(
                WheelFile.validation_error,
                match=(
                    r"In .+, entry in RECORD file for .+ is invalid: "
                    "invalid hash algorithm 'sha'"
                ),
            ),
        ):
            source.validate_record(validate_contents=True)


class TestWheelFileRecordIntegrity:
    """Integrity checks for ``WheelFile.validate_record``."""

    # -- normal wheels -----------------------------------------------------
    def test_valid_wheel_passes_both_modes(self, tmp_path):
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        make_simple_wheel(wheel)

        with WheelFile.open(wheel) as source:
            source.validate_record(validate_contents=False)
            source.validate_record(validate_contents=True)
            # Repeated validation of the same opened file keeps working and
            # must not close the caller-owned ZIP.
            source.validate_record(validate_contents=True)

    def test_large_member_is_hashed_in_chunks(self, tmp_path, monkeypatch):
        # 200 KiB guarantees more than one 64 KiB read.
        big = b"a" * (200 * 1024)
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        make_simple_wheel(wheel, extra_files={"fancy/big.bin": big})

        observed_sizes = []
        real_read = ZipExtFile.read

        def tracking_read(self, n=-1):
            chunk = real_read(self, n)
            observed_sizes.append(len(chunk))
            return chunk

        monkeypatch.setattr(ZipExtFile, "read", tracking_read)
        with WheelFile.open(wheel) as source:
            source.validate_record(validate_contents=True)

        # Every read of the big member stays within the 64 KiB cap, and the
        # member was actually read in more than one chunk.
        assert observed_sizes
        assert max(observed_sizes) <= _VALIDATION_CHUNK_SIZE
        assert sum(observed_sizes) >= len(big)

    def test_validation_does_not_extract_to_filesystem(self, tmp_path, monkeypatch):
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        make_simple_wheel(wheel, extra_files={"fancy/big.bin": b"x" * 128 * 1024})

        real_open = builtins.open

        def fail_on_filesystem_writes(*args, **kwargs):
            mode = args[1] if len(args) > 1 else kwargs.get("mode", "r")
            if any(flag in mode for flag in ("w", "a", "+")):
                raise AssertionError(f"validation tried to write through open(): {args}")
            return real_open(*args, **kwargs)

        monkeypatch.setattr(builtins, "open", fail_on_filesystem_writes)
        with WheelFile.open(wheel) as source:
            source.validate_record(validate_contents=True)

    # -- duplicates and omissions -----------------------------------------
    def test_duplicate_zip_member_is_reported(self, tmp_path):
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        make_simple_wheel(wheel)
        duplicate_zip_member(wheel, "fancy/__init__.py")

        with (
            WheelFile.open(wheel) as source,
            pytest.raises(WheelFile.validation_error) as exc_info,
        ):
            source.validate_record(validate_contents=True)

        issues = exc_info.value.issues
        assert any("fancy/__init__.py is duplicated in the ZIP" in i for i in issues)
        # The first copy is still validated, not shadowed by the duplicate.
        assert not any("didn't match RECORD" in i for i in issues)

    def test_duplicate_record_path_is_reported(self, tmp_path):
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        files, record_lines = make_simple_wheel(wheel)
        # Insert a second row for the same path (different claimed hash).
        line = make_record_line("fancy/__init__.py", b"different contents here!!")
        record_lines.insert(1, line)
        build_wheel(wheel, files, record_lines)

        with (
            WheelFile.open(wheel) as source,
            pytest.raises(WheelFile.validation_error) as exc_info,
        ):
            source.validate_record(validate_contents=False)

        assert any(
            "Duplicate path 'fancy/__init__.py' in RECORD" in i
            for i in exc_info.value.issues
        )

    def test_record_only_file_is_reported(self, tmp_path):
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        files, record_lines = make_simple_wheel(wheel)
        record_lines.insert(
            0, make_record_line("fancy/missing.py", b"never added to the zip")
        )
        build_wheel(wheel, files, record_lines)

        with (
            WheelFile.open(wheel) as source,
            pytest.raises(WheelFile.validation_error) as exc_info,
        ):
            source.validate_record(validate_contents=False)

        assert any(
            "fancy/missing.py is mentioned in RECORD but not present in the ZIP" in i
            for i in exc_info.value.issues
        )

    def test_unlisted_member_is_reported(self, tmp_path):
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        make_simple_wheel(
            wheel,
            extra_files={"fancy/unlisted.py": b"# not in RECORD\n"},
            record_excludes={"fancy/unlisted.py"},
        )

        with (
            WheelFile.open(wheel) as source,
            pytest.raises(WheelFile.validation_error) as exc_info,
        ):
            source.validate_record(validate_contents=False)

        assert any(
            "fancy/unlisted.py is not mentioned in RECORD" in i
            for i in exc_info.value.issues
        )

    def test_non_three_column_row_is_reported(self, tmp_path):
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        files, record_lines = make_simple_wheel(wheel)
        record_lines.insert(0, "fancy/broken.py,sha256=abc")  # only two columns
        build_wheel(wheel, files, record_lines)

        with (
            WheelFile.open(wheel) as source,
            pytest.raises(WheelFile.validation_error) as exc_info,
        ):
            source.validate_record(validate_contents=False)

        assert any(
            "Row Index 0: expected 3 elements, got 2" in i
            for i in exc_info.value.issues
        )

    def test_backslash_path_does_not_match_zip_member(self, tmp_path):
        # Paths are matched exactly as stored: backslashes are not rewritten.
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        files, record_lines = make_simple_wheel(wheel)
        record_lines[0] = record_lines[0].replace("fancy/__init__.py", "fancy\\__init__.py")
        build_wheel(wheel, files, record_lines)

        with (
            WheelFile.open(wheel) as source,
            pytest.raises(WheelFile.validation_error) as exc_info,
        ):
            source.validate_record(validate_contents=False)

        joined = "\n".join(exc_info.value.issues)
        assert "fancy/__init__.py is not mentioned in RECORD" in joined
        assert "fancy\\__init__.py is mentioned in RECORD but not present in the ZIP" in joined

    # -- signature files ---------------------------------------------------
    @pytest.mark.parametrize("signature_name", ["RECORD.jws", "RECORD.p7s"])
    def test_dist_info_signature_file_may_be_unlisted(self, tmp_path, signature_name):
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        signature_path = f"fancy-1.0.0.dist-info/{signature_name}"
        make_simple_wheel(
            wheel,
            extra_files={signature_path: b"signature bytes"},
            record_excludes={signature_path},
        )

        with WheelFile.open(wheel) as source:
            source.validate_record(validate_contents=False)
            source.validate_record(validate_contents=True)

    @pytest.mark.parametrize("signature_name", ["RECORD.jws", "RECORD.p7s"])
    def test_signature_file_in_record_is_still_an_error(
        self, tmp_path, signature_name
    ):
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        signature_path = f"fancy-1.0.0.dist-info/{signature_name}"
        files, record_lines = make_simple_wheel(
            wheel, extra_files={signature_path: b"signature bytes"}
        )
        record_lines.insert(-1, make_record_line(signature_path, b"signature bytes"))
        build_wheel(wheel, files, record_lines)

        with (
            WheelFile.open(wheel) as source,
            pytest.raises(WheelFile.validation_error) as exc_info,
        ):
            source.validate_record(validate_contents=False)

        assert any(
            f"digital signature file {signature_path} is incorrectly contained "
            "in RECORD" in i
            for i in exc_info.value.issues
        )

    @pytest.mark.parametrize("signature_name", ["RECORD.jws", "RECORD.p7s"])
    def test_signature_name_outside_dist_info_is_not_exempt(
        self, tmp_path, signature_name
    ):
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        nested = f"fancy/signatures/{signature_name}"
        make_simple_wheel(
            wheel,
            extra_files={nested: b"not really a signature"},
            record_excludes={nested},
        )

        with (
            WheelFile.open(wheel) as source,
            pytest.raises(WheelFile.validation_error) as exc_info,
        ):
            source.validate_record(validate_contents=False)

        assert any(
            f"{nested} is not mentioned in RECORD" in i
            for i in exc_info.value.issues
        )

    # -- malicious hashes and sizes ---------------------------------------
    @pytest.mark.parametrize(
        "bad_digest",
        [
            "abc def",  # embedded whitespace
            "abc+def",  # standard Base64 alphabet, not URL-safe
            "abc/def",  # standard Base64 alphabet, not URL-safe
            "abc=def",  # padding character in the middle
            "Y0sCextp4SQtQNU+MSs7SsdxD1W-gfKJtUlEbvZ3i-4=",  # trailing padding
            "qZ2qq7x",  # decodes to fewer bytes than the sha256 digest size
        ],
    )
    def test_malformed_digest_is_reported_in_both_modes(self, tmp_path, bad_digest):
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        files, record_lines = make_simple_wheel(wheel)
        record_lines[0] = f"fancy/__init__.py,sha256={bad_digest},{len(files['fancy/__init__.py'])}"
        build_wheel(wheel, files, record_lines)

        for validate_contents in (False, True):
            with (
                WheelFile.open(wheel) as source,
                pytest.raises(WheelFile.validation_error) as exc_info,
            ):
                source.validate_record(validate_contents=validate_contents)

            joined = "\n".join(exc_info.value.issues)
            assert "fancy/__init__.py is invalid" in joined

    @pytest.mark.parametrize("algorithm", ["md5", "sha1", "sha224", "blake2b"])
    def test_unsupported_algorithm_is_reported(self, tmp_path, algorithm):
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        files, record_lines = make_simple_wheel(wheel)
        digest = (
            urlsafe_b64encode(hashlib.new(algorithm, files["fancy/__init__.py"]).digest())
            .decode("ascii")
            .rstrip("=")
        )
        record_lines[0] = (
            f"fancy/__init__.py,{algorithm}={digest},"
            f"{len(files['fancy/__init__.py'])}"
        )
        build_wheel(wheel, files, record_lines)

        with (
            WheelFile.open(wheel) as source,
            pytest.raises(WheelFile.validation_error) as exc_info,
        ):
            source.validate_record(validate_contents=False)

        assert any(
            f"invalid hash algorithm '{algorithm}'" in i
            for i in exc_info.value.issues
        )

    @pytest.mark.parametrize(
        "bad_size",
        [
            "+10",  # sign
            "-10",  # sign
            " 10",  # whitespace
            "1 0",  # embedded whitespace
            "１０",  # Unicode fullwidth digits
            "1.0",  # non-integer
            "0x10",  # hex notation
        ],
    )
    def test_malformed_size_is_reported_in_both_modes(self, tmp_path, bad_size):
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        files, record_lines = make_simple_wheel(wheel)
        record_lines[0] = (
            f"fancy/__init__.py,{record_lines[0].split(',')[1]},{bad_size}"
        )
        build_wheel(wheel, files, record_lines)

        for validate_contents in (False, True):
            with (
                WheelFile.open(wheel) as source,
                pytest.raises(WheelFile.validation_error) as exc_info,
            ):
                source.validate_record(validate_contents=validate_contents)

            assert any(
                "fancy/__init__.py is invalid" in i for i in exc_info.value.issues
            )

    def test_leading_zero_size_is_accepted(self, tmp_path):
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        files, record_lines = make_simple_wheel(wheel)
        size = len(files["fancy/__init__.py"])
        record_lines[0] = record_lines[0].replace(f",{size}", f",00{size}")
        build_wheel(wheel, files, record_lines)

        with WheelFile.open(wheel) as source:
            source.validate_record(validate_contents=False)
            source.validate_record(validate_contents=True)

    @pytest.mark.parametrize("algorithm", ["sha384", "sha512"])
    def test_accepted_algorithms_with_correct_digest_length(
        self, tmp_path, algorithm
    ):
        data = b"multi-algorithm content\n"
        digest = (
            urlsafe_b64encode(hashlib.new(algorithm, data).digest())
            .decode("ascii")
            .rstrip("=")
        )
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        files, record_lines = make_simple_wheel(
            wheel,
            extra_files={"fancy/alg.bin": data},
            record_excludes={"fancy/alg.bin"},
        )
        record_lines.insert(0, f"fancy/alg.bin,{algorithm}={digest},{len(data)}")
        build_wheel(wheel, files, record_lines)

        with WheelFile.open(wheel) as source:
            source.validate_record(validate_contents=True)

    # -- structure-only vs content validation ------------------------------
    def test_structure_only_skips_content_bytes(self, tmp_path):
        # A wrong hash is fine for entries mode; the mismatch only surfaces
        # when actual contents are read.
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        files, record_lines = make_simple_wheel(wheel)
        size = len(files["fancy/__init__.py"])
        record_lines[0] = (
            "fancy/__init__.py,"
            "sha256=pREiHcl39jRySUXMCOrwmSsnOay8FB7fOJP5mZQ3D3A,"
            f"{size}"
        )
        build_wheel(wheel, files, record_lines)

        with WheelFile.open(wheel) as source:
            source.validate_record(validate_contents=False)
            with pytest.raises(WheelFile.validation_error, match="didn't match RECORD"):
                source.validate_record(validate_contents=True)

    def test_wrong_size_with_correct_format_still_fails_content_check(
        self, tmp_path
    ):
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        files, record_lines = make_simple_wheel(wheel)
        hash_part = record_lines[0].split(",")[1]
        record_lines[0] = f"fancy/__init__.py,{hash_part},9999"
        build_wheel(wheel, files, record_lines)

        with WheelFile.open(wheel) as source:
            source.validate_record(validate_contents=False)
            with pytest.raises(WheelFile.validation_error, match="didn't match RECORD"):
                source.validate_record(validate_contents=True)

    # -- error isolation and stable reporting ------------------------------
    def test_one_broken_member_does_not_stop_other_checks(self, tmp_path):
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        files, record_lines = make_simple_wheel(
            wheel, extra_files={"fancy/crc.bin": b"crc protected payload"}
        )
        # Give __init__.py a bogus RECORD hash while everything else is valid.
        size = len(files["fancy/__init__.py"])
        record_lines[0] = (
            f"fancy/__init__.py,sha256=pREiHcl39jRySUXMCOrwmSsnOay8FB7fOJP5mZQ3D3A,{size}"
        )
        build_wheel(wheel, files, record_lines)
        # Only then corrupt the stored bytes of crc.bin at the raw ZIP level,
        # so no re-archive step recomputes its CRC header.
        corrupt_member(wheel, "fancy/crc.bin", b"crc protected PAYLOAD")

        with (
            WheelFile.open(wheel) as source,
            pytest.raises(WheelFile.validation_error) as exc_info,
        ):
            source.validate_record(validate_contents=True)

        issues = exc_info.value.issues
        joined = "\n".join(issues)
        # The unreadable member is reported with its name and the reason.
        assert any("unable to read fancy/crc.bin" in i for i in issues)
        # The other bad member is still independently reported.
        assert "fancy/__init__.py" in joined
        assert any("didn't match RECORD" in i for i in issues)

    def test_crc_error_does_not_escape_as_other_exception(self, tmp_path):
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        make_simple_wheel(wheel, extra_files={"fancy/crc.bin": b"crc protected payload"})
        corrupt_member(wheel, "fancy/crc.bin", b"crc protected PAYLOAD")

        with (
            WheelFile.open(wheel) as source,
            pytest.raises(WheelFile.validation_error) as exc_info,
        ):
            source.validate_record(validate_contents=True)

        assert any("unable to read fancy/crc.bin" in i for i in exc_info.value.issues)

    def test_issue_order_is_stable_across_runs(self, tmp_path):
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        files, record_lines = make_simple_wheel(
            wheel,
            extra_files={"fancy/unlisted.py": b"# unlisted\n"},
            record_excludes={"fancy/unlisted.py"},
        )
        record_lines.insert(0, make_record_line("fancy/missing.py", b"gone"))
        build_wheel(wheel, files, record_lines)

        captured = []
        with WheelFile.open(wheel) as source:
            for _ in range(3):
                with pytest.raises(WheelFile.validation_error) as exc_info:
                    source.validate_record(validate_contents=False)
                captured.append(exc_info.value.issues)

            assert captured[0] == captured[1] == captured[2]
            # The caller's ZIP is still open and usable afterwards.
            assert source.read_dist_info("WHEEL") == files[
                "fancy-1.0.0.dist-info/WHEEL"
            ].decode()

    def test_record_self_hash_and_size_must_be_empty(self, tmp_path):
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        files, record_lines = make_simple_wheel(wheel)
        record_lines[-1] = (
            "fancy-1.0.0.dist-info/RECORD,"
            "sha256=pREiHcl39jRySUXMCOrwmSsnOay8FB7fOJP5mZQ3D3A,12"
        )
        build_wheel(wheel, files, record_lines)

        with (
            WheelFile.open(wheel) as source,
            pytest.raises(
                WheelFile.validation_error,
                match="RECORD file incorrectly contains hash / size",
            ),
        ):
            source.validate_record(validate_contents=False)

    def test_record_must_exist(self, tmp_path):
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        make_simple_wheel(wheel)
        replace_file_in_zip(
            wheel, "fancy-1.0.0.dist-info/RECORD", content=None
        )

        with (
            WheelFile.open(wheel) as source,
            pytest.raises(
                WheelFile.validation_error, match="Unable to retrieve `RECORD`"
            ),
        ):
            source.validate_record(validate_contents=False)

    def test_validation_error_is_value_error_and_installer_error(self, tmp_path):
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        make_simple_wheel(
            wheel,
            extra_files={"fancy/unlisted.py": b"# unlisted\n"},
            record_excludes={"fancy/unlisted.py"},
        )

        with WheelFile.open(wheel) as source:
            with pytest.raises(ValueError) as value_error_info:
                source.validate_record(validate_contents=False)
            assert isinstance(value_error_info.value, InstallerError)
            assert value_error_info.value.issues

    def test_zipfile_passed_directly_is_not_closed(self, tmp_path):
        wheel = tmp_path / "fancy-1.0.0-py3-none-any.whl"
        make_simple_wheel(wheel)

        archive = zipfile.ZipFile(wheel)
        try:
            source = WheelFile(archive)
            source.validate_record(validate_contents=True)
            source.validate_record(validate_contents=False)
            # Still usable: the validator must never close the caller's ZIP.
            assert archive.namelist()
        finally:
            archive.close()
