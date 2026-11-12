"""Integration tests: install real wheels into a temporary venv.

The wheels are built locally in temporary directories and installed only into
temporary locations -- nothing is installed into the system environment.
"""

import os
import subprocess
import sys
import sysconfig
import textwrap
import venv
import zipfile

import pytest

from installer import install
from installer.destinations import SchemeDictionaryDestination
from installer.sources import WheelFile
from installer.utils import InvalidEntryPoint

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX script generation is being tested"
)


@pytest.fixture()
def sample_venv(tmp_path):
    """A real venv in a temporary directory, with a space in its path."""
    venv_dir = tmp_path / "venv with space"
    venv.create(venv_dir, with_pip=False)
    return venv_dir


def _scheme_dict(venv_dir):
    scheme_dict = sysconfig.get_paths(
        vars={"base": str(venv_dir), "platbase": str(venv_dir)}
    )
    scheme_dict["headers"] = str(venv_dir / "headers")
    return scheme_dict


def _install_into_venv(wheel_path, venv_dir):
    destination = SchemeDictionaryDestination(
        scheme_dict=_scheme_dict(venv_dir),
        interpreter=str(venv_dir / "bin" / "python"),
        script_kind="posix",
    )
    with WheelFile.open(wheel_path) as source:
        install(source, destination, additional_metadata={})


def _build_wheel(path, *, entry_points_text):
    files = {
        "goodpkg/__init__.py": textwrap.dedent(
            """\
            def main():
                print("hello from goodpkg")
            """
        ),
        "goodpkg-1.0.0.dist-info/METADATA": textwrap.dedent(
            """\
            Metadata-Version: 2.1
            Name: goodpkg
            Version: 1.0.0
            """
        ),
        "goodpkg-1.0.0.dist-info/WHEEL": textwrap.dedent(
            """\
            Wheel-Version: 1.0
            Generator: installer-tests
            Root-Is-Purelib: true
            Tag: py3-none-any
            """
        ),
        "goodpkg-1.0.0.dist-info/entry_points.txt": entry_points_text,
        "goodpkg-1.0.0.dist-info/RECORD": "",
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def test_installed_console_script_runs_target_function(sample_venv, tmp_path):
    wheel = _build_wheel(
        tmp_path / "goodpkg-1.0.0-py3-none-any.whl",
        entry_points_text=textwrap.dedent(
            """\
            [console_scripts]
            good-tool = goodpkg:main
            """
        ),
    )

    _install_into_venv(wheel, sample_venv)

    script = sample_venv / "bin" / "good-tool"
    assert script.is_file()
    assert os.access(script, os.X_OK)

    result = subprocess.run(
        [str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "hello from goodpkg"


def test_malicious_entry_points_rejected_without_side_effects(
    sample_venv, tmp_path
):
    wheel = _build_wheel(
        tmp_path / "goodpkg-1.0.0-py3-none-any.whl",
        entry_points_text=textwrap.dedent(
            """\
            [console_scripts]
            good-tool = goodpkg:main
            evil = goodpkg:main'); import os; os.system("touch pwned") #
            """
        ),
    )

    with pytest.raises(InvalidEntryPoint):
        _install_into_venv(wheel, sample_venv)

    # Nothing may have been written: no scripts (not even the valid-looking
    # first declaration) and no package files.
    assert not (sample_venv / "bin" / "good-tool").exists()
    assert not (sample_venv / "bin" / "evil").exists()
    purelib = _scheme_dict(sample_venv)["purelib"]
    assert not os.path.exists(os.path.join(purelib, "goodpkg"))
    # The injected content was never executed.
    assert not (tmp_path / "pwned").exists()
    assert not os.path.exists("pwned")
