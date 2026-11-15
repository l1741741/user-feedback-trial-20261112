from __future__ import annotations

import re
import shutil
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from scrapy.http import Request
from scrapy.settings import Settings
from scrapy.spiderloader import (
    SpiderDiscoveryError,
    SpiderDiscoveryReport,
    SpiderLoader,
)
from scrapy.utils.spider import iter_spider_classes

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def package(tmp_path):
    """A temporary importable package, plus a loader factory for it."""
    sys.path.append(str(tmp_path))
    package_name = "spider_discovery_pkg"
    try:
        yield tmp_path, package_name
    finally:
        for name in list(sys.modules):
            if name == package_name or name.startswith(f"{package_name}."):
                del sys.modules[name]
        sys.path.remove(str(tmp_path))


def _write(root: Path, package_name: str, files: dict[str, str]) -> None:
    _forget(package_name)
    package_dir = root / package_name
    package_dir.mkdir(exist_ok=True)
    (package_dir / "__init__.py").touch()
    for name, source in files.items():
        path = package_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        # Rewritten files have no new timestamp, so a stale bytecode cache
        # would otherwise be used.
        cache_dir = path.parent / "__pycache__"
        if cache_dir.exists():
            shutil.rmtree(cache_dir)


def _snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _spider(name: str, spider_name: str | None = None, **attributes: str) -> str:
    lines = [
        "from scrapy.spiders import Spider",
        "",
        f"class {name}(Spider):",
        f"    name = {spider_name or _snake_case(name)!r}",
        '    allowed_domains = ["example.com"]',
    ]
    lines.extend(f"    {key} = {value}" for key, value in attributes.items())
    return "\n".join(lines) + "\n"


def _forget(package_name: str) -> None:
    """Drop *package_name* from the import cache, as if it had never been
    imported, so that rewritten modules are imported again."""
    for name in list(sys.modules):
        if name == package_name or name.startswith(f"{package_name}."):
            del sys.modules[name]


def _loader(package_name: str, **settings: object) -> SpiderLoader:
    return SpiderLoader.from_settings(
        Settings(
            {
                "SPIDER_MODULES": [package_name],
                "SPIDER_LOADER_DISCOVERY_REPORT": True,
                **settings,
            }
        )
    )


def test_report_lists_modules_and_classes_sorted(package):
    root, package_name = package
    _write(
        root,
        package_name,
        {
            "a.py": _spider("SpiderA"),
            "nested/__init__.py": "",
            "nested/b.py": _spider("SpiderB"),
        },
    )
    loader = _loader(package_name)
    report = loader.discovery_report
    assert report is not None
    assert report.version == 1
    assert report.published
    assert report.modules == (
        f"{package_name}",
        f"{package_name}.a",
        f"{package_name}.nested",
        f"{package_name}.nested.b",
    )
    assert report.classes == (
        report.classes[0],
        report.classes[1],
    )
    assert [item.qualified_name for item in report.classes] == [
        f"{package_name}.a.SpiderA",
        f"{package_name}.nested.b.SpiderB",
    ]
    assert all(item.published for item in report.classes)
    assert report.import_failures == ()
    assert report.conflicts == ()
    assert loader.list() == ["spider_a", "spider_b"]


def test_report_does_not_change_spider_names(package):
    root, package_name = package
    _write(
        root,
        package_name,
        {"a.py": _spider("SpiderA"), "b.py": _spider("SpiderA")},
    )
    with pytest.warns(UserWarning, match="several spiders with the same name"):
        loader = _loader(package_name, SPIDER_LOADER_WARN_ONLY=True)
    report = loader.discovery_report
    assert report is not None
    assert [item.spider_name for item in report.classes] == ["spider_a", "spider_a"]
    assert report.classes[0].qualified_name == f"{package_name}.a.SpiderA"
    assert report.classes[1].qualified_name == f"{package_name}.b.SpiderA"


def test_strict_import_failure_keeps_previous_registry(package):
    root, package_name = package
    _write(root, package_name, {"a.py": _spider("SpiderA")})
    loader = _loader(package_name)
    assert loader.list() == ["spider_a"]
    first = loader.discovery_report
    assert first is not None
    assert first.version == 1

    _write(root, package_name, {"broken.py": "def oops(:\n"})
    with pytest.raises(SpiderDiscoveryError) as excinfo:
        loader.reload()
    report = excinfo.value.report
    assert report.version == 2
    assert not report.published
    assert loader.discovery_report == report
    # The registry published by the previous attempt is untouched.
    assert loader.list() == ["spider_a"]
    assert loader.load("spider_a").__name__ == "SpiderA"
    assert loader.find_by_request(Request("http://example.com/")) == ["spider_a"]
    assert report.import_failures == (report.import_failures[0],)
    assert report.import_failures[0].module == f"{package_name}.broken"
    assert issubclass(report.import_failures[0].exception, SyntaxError)


def test_lenient_skips_failed_module_and_reports_it(package):
    root, package_name = package
    _write(
        root,
        package_name,
        {"a.py": _spider("SpiderA"), "broken.py": "raise ImportError('boom')\n"},
    )
    with pytest.warns(RuntimeWarning, match="Could not load spiders from module"):
        loader = _loader(package_name, SPIDER_LOADER_WARN_ONLY=True)
    report = loader.discovery_report
    assert report is not None
    assert report.version == 1
    assert report.published
    assert report.modules == (package_name, f"{package_name}.a")
    assert [item.qualified_name for item in report.classes] == [
        f"{package_name}.a.SpiderA"
    ]
    assert report.import_failures == (report.import_failures[0],)
    assert report.import_failures[0].module == f"{package_name}.broken"
    assert report.import_failures[0].exception is ImportError
    assert loader.list() == ["spider_a"]
    assert loader.load("spider_a").__name__ == "SpiderA"


def test_lenient_circular_import_is_reported(package):
    root, package_name = package
    _write(
        root,
        package_name,
        {
            "cycle_a.py": "from .cycle_b import marker\n",
            "cycle_b.py": "from .cycle_a import marker\n",
            "a.py": _spider("SpiderA"),
        },
    )
    with pytest.warns(RuntimeWarning, match="Could not load spiders from module"):
        loader = _loader(package_name, SPIDER_LOADER_WARN_ONLY=True)
    report = loader.discovery_report
    assert report is not None
    assert report.published
    assert [item.module for item in report.import_failures] == [
        f"{package_name}.cycle_a",
        f"{package_name}.cycle_b",
    ]
    assert loader.list() == ["spider_a"]


def test_strict_circular_import_fails_without_publishing(package):
    root, package_name = package
    _write(
        root,
        package_name,
        {
            "cycle_a.py": "from .cycle_b import marker\n",
            "cycle_b.py": "from .cycle_a import marker\n",
            "a.py": _spider("SpiderA"),
        },
    )
    with pytest.raises(SpiderDiscoveryError) as excinfo:
        _loader(package_name)
    assert not excinfo.value.report.published
    assert [item.module for item in excinfo.value.report.import_failures] == [
        f"{package_name}.cycle_a",
        f"{package_name}.cycle_b",
    ]


def test_lenient_same_class_reexport_is_not_a_conflict(package):
    root, package_name = package
    _write(
        root,
        package_name,
        {
            "real.py": _spider("RealSpider"),
            "reexport.py": "from .real import RealSpider\n",
        },
    )
    loader = _loader(package_name, SPIDER_LOADER_WARN_ONLY=True)
    report = loader.discovery_report
    assert report is not None
    assert report.published
    assert report.classes == (report.classes[0],)
    assert report.classes[0].qualified_name == f"{package_name}.real.RealSpider"
    assert report.conflicts == ()
    assert loader.list() == ["real_spider"]


def test_strict_same_class_reexport_is_not_a_conflict(package):
    root, package_name = package
    _write(
        root,
        package_name,
        {
            "real.py": _spider("RealSpider"),
            "reexport.py": "from .real import RealSpider\n",
        },
    )
    loader = _loader(package_name)
    report = loader.discovery_report
    assert report is not None
    assert report.published
    assert report.conflicts == ()
    assert loader.list() == ["real_spider"]
    assert loader.load("real_spider").__name__ == "RealSpider"


def test_lenient_distinct_classes_same_name_are_reported_not_published(package):
    root, package_name = package
    _write(
        root,
        package_name,
        {"a.py": _spider("SpiderA"), "b.py": _spider("SpiderA")},
    )
    with pytest.warns(UserWarning, match="several spiders with the same name"):
        loader = _loader(package_name, SPIDER_LOADER_WARN_ONLY=True)
    report = loader.discovery_report
    assert report is not None
    assert report.published
    assert report.conflicts == (report.conflicts[0],)
    assert report.conflicts[0].name == "spider_a"
    assert report.conflicts[0].classes == (
        f"{package_name}.a.SpiderA",
        f"{package_name}.b.SpiderA",
    )
    # No class of the ambiguous name is published, and neither class is
    # reported as published.
    assert not any(item.published for item in report.classes)
    assert loader.list() == []
    with pytest.raises(KeyError, match="Spider not found: spider_a"):
        loader.load("spider_a")


def test_strict_distinct_classes_same_name_fail(package):
    root, package_name = package
    _write(
        root,
        package_name,
        {"a.py": _spider("SpiderA"), "b.py": _spider("SpiderA")},
    )
    with pytest.raises(SpiderDiscoveryError) as excinfo:
        _loader(package_name)
    report = excinfo.value.report
    assert not report.published
    assert report.conflicts == (report.conflicts[0],)
    assert report.conflicts[0].classes == (
        f"{package_name}.a.SpiderA",
        f"{package_name}.b.SpiderA",
    )


def test_fix_after_failure_publishes_new_registry(package):
    root, package_name = package
    _write(root, package_name, {"broken.py": "raise ImportError('boom')\n"})
    with pytest.warns(RuntimeWarning, match="Could not load spiders from module"):
        loader = _loader(package_name, SPIDER_LOADER_WARN_ONLY=True)
    failed = loader.discovery_report
    assert failed is not None
    assert failed.version == 1
    assert failed.import_failures
    assert loader.list() == []

    _write(root, package_name, {"broken.py": _spider("SpiderB")})
    fixed = loader.reload()
    assert fixed is not None
    assert fixed.version == 2
    assert fixed.published
    assert fixed.import_failures == ()
    assert fixed.modules == (package_name, f"{package_name}.broken")
    assert loader.discovery_report == fixed
    assert loader.list() == ["spider_b"]
    assert loader.find_by_request(Request("http://example.com/")) == ["spider_b"]


def test_repeated_loads_do_not_change_report_version(package):
    root, package_name = package
    _write(root, package_name, {"a.py": _spider("SpiderA")})
    loader = _loader(package_name)
    first = loader.discovery_report
    assert first is not None
    for _ in range(3):
        assert loader.load("spider_a").__name__ == "SpiderA"
    assert loader.discovery_report is first
    assert loader.discovery_report.version == 1


def test_conflict_reload_in_strict_mode_keeps_registry_and_versions(package):
    root, package_name = package
    _write(root, package_name, {"a.py": _spider("SpiderA")})
    loader = _loader(package_name)
    assert loader.list() == ["spider_a"]

    _write(
        root,
        package_name,
        {"a.py": _spider("SpiderA"), "b.py": _spider("SpiderA")},
    )
    with pytest.raises(SpiderDiscoveryError) as excinfo:
        loader.reload()
    assert excinfo.value.report.version == 2
    assert not excinfo.value.report.published
    assert excinfo.value.report.conflicts[0].name == "spider_a"
    assert loader.list() == ["spider_a"]

    _write(
        root,
        package_name,
        {"a.py": _spider("SpiderA"), "b.py": _spider("SpiderB")},
    )
    report = loader.reload()
    assert report is not None
    assert report.version == 3
    assert report.published
    assert report.conflicts == ()
    assert loader.list() == ["spider_a", "spider_b"]


def test_two_loaders_have_independent_reports(package):
    root, package_name = package
    _write(root, package_name, {"a.py": _spider("SpiderA")})
    first_loader = _loader(package_name)
    second_loader = _loader(package_name)
    assert first_loader.discovery_report is not None
    assert second_loader.discovery_report is not None
    assert first_loader.discovery_report.version == 1
    assert second_loader.discovery_report.version == 1

    first_loader.reload()
    assert first_loader.discovery_report.version == 2
    assert second_loader.discovery_report.version == 1


def test_reload_during_discovery_iterator_failure_does_not_partially_publish(
    package, monkeypatch
):
    root, package_name = package
    _write(
        root,
        package_name,
        {"a.py": _spider("SpiderA"), "b.py": _spider("SpiderB")},
    )
    loader = _loader(package_name)
    assert loader.list() == ["spider_a", "spider_b"]

    def _failing_iter(module):
        yield from iter_spider_classes(module)
        raise RuntimeError("discovery iterator failed")

    monkeypatch.setattr("scrapy.spiderloader.iter_spider_classes", _failing_iter)
    with pytest.raises(RuntimeError, match="discovery iterator failed"):
        loader.reload()
    assert loader.discovery_report is not None
    assert loader.discovery_report.version == 2
    assert not loader.discovery_report.published
    # Nothing from the failed attempt is visible, and the previous registry is
    # intact.
    assert loader.list() == ["spider_a", "spider_b"]


def test_report_mode_disabled_keeps_historical_behavior(package):
    root, package_name = package
    _write(
        root,
        package_name,
        {"a.py": _spider("SpiderA"), "b.py": _spider("SpiderA")},
    )
    with pytest.warns(UserWarning, match="several spiders with the same name"):
        loader = SpiderLoader.from_settings(
            Settings(
                {
                    "SPIDER_MODULES": [package_name],
                    "SPIDER_LOADER_WARN_ONLY": True,
                }
            )
        )
    assert loader.discovery_report is None
    with pytest.warns(UserWarning, match="several spiders with the same name"):
        assert loader.reload() is None
    assert loader.list() == ["spider_a"]
    # The historical behavior publishes the last class found.
    assert loader.load("spider_a").__name__ == "SpiderA"
    assert sys.modules[f"{package_name}.b"].SpiderA is loader.load("spider_a")


def test_report_includes_qualified_module_for_syntax_error_name(package):
    root, package_name = package
    _write(root, package_name, {"broken.py": "def oops(:\n"})
    with pytest.warns(RuntimeWarning, match="Could not load spiders from module"):
        loader = _loader(package_name, SPIDER_LOADER_WARN_ONLY=True)
    report = loader.discovery_report
    assert report is not None
    assert report.import_failures[0].module == f"{package_name}.broken"
    assert report.import_failures[0].exception is SyntaxError


def test_report_is_a_frozen_dataclass(package):
    root, package_name = package
    _write(root, package_name, {"a.py": _spider("SpiderA")})
    report = _loader(package_name).discovery_report
    assert isinstance(report, SpiderDiscoveryReport)
    with pytest.raises(AttributeError):
        report.version = 2  # type: ignore[misc]


def test_custom_spider_modules_overlap_does_not_conflict_with_itself(package):
    root, package_name = package
    _write(root, package_name, {"a.py": _spider("SpiderA")})
    settings = Settings(
        {
            "SPIDER_MODULES": [f"{package_name}.a", f"{package_name}.a"],
            "SPIDER_LOADER_DISCOVERY_REPORT": True,
        }
    )
    loader = SpiderLoader.from_settings(settings)
    report = loader.discovery_report
    assert report is not None
    assert report.conflicts == ()
    assert report.modules == (f"{package_name}.a",)
    assert loader.list() == ["spider_a"]


def test_walk_failure_does_not_publish_partial_registry(package, monkeypatch):
    root, package_name = package
    _write(root, package_name, {"a.py": _spider("SpiderA")})
    loader = _loader(package_name)
    assert loader.list() == ["spider_a"]

    def _failing_iter_modules(paths):
        raise OSError("cannot list the package directory")

    monkeypatch.setattr("scrapy.spiderloader.iter_modules", _failing_iter_modules)
    with pytest.raises(OSError, match="cannot list the package directory"):
        loader.reload()
    assert loader.list() == ["spider_a"]
    report = loader.discovery_report
    assert report is not None
    assert report.version == 2
    assert not report.published
    assert report.import_failures[0].module == package_name
    assert report.import_failures[0].exception is OSError


def test_unexpected_iter_failure_is_not_skipped_in_lenient_mode(package, monkeypatch):
    root, package_name = package
    _write(root, package_name, {"a.py": _spider("SpiderA")})
    loader = _loader(package_name, SPIDER_LOADER_WARN_ONLY=True)
    assert loader.list() == ["spider_a"]

    def _failing_iter(module):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("scrapy.spiderloader.iter_spider_classes", _failing_iter)
    with pytest.raises(RuntimeError, match="unexpected"):
        loader.reload()
    assert loader.list() == ["spider_a"]
    report = loader.discovery_report
    assert report is not None
    assert report.version == 2
    assert not report.published
    # The unexpected failure is reported for the module being walked, so that
    # it is visible in the report, and it is not treated as a skippable import
    # failure.
    assert len(report.import_failures) == 1
    assert report.import_failures[0].module == package_name
    assert report.import_failures[0].exception is RuntimeError


def test_missing_top_level_module_is_reported(package):
    root, package_name = package
    _write(root, package_name, {"a.py": _spider("SpiderA")})
    settings = Settings(
        {
            "SPIDER_MODULES": [package_name, "spider_discovery_missing"],
            "SPIDER_LOADER_DISCOVERY_REPORT": True,
            "SPIDER_LOADER_WARN_ONLY": True,
        }
    )
    with pytest.warns(RuntimeWarning, match="Could not load spiders from module"):
        loader = SpiderLoader.from_settings(settings)
    report = loader.discovery_report
    assert report is not None
    assert [item.module for item in report.import_failures] == [
        "spider_discovery_missing"
    ]
    assert report.import_failures[0].exception is ModuleNotFoundError
    assert loader.list() == ["spider_a"]


def test_circular_import_of_the_package_init_is_reported(package):
    root, package_name = package
    _write(
        root,
        package_name,
        {"a.py": "from . import b\n", "b.py": "raise ImportError('boom')\n"},
    )
    with pytest.warns(RuntimeWarning, match="Could not load spiders from module"):
        loader = _loader(package_name, SPIDER_LOADER_WARN_ONLY=True)
    report = loader.discovery_report
    assert report is not None
    assert report.import_failures[0].exception is ImportError


@pytest.mark.parametrize("warn_only", [False, True])
def test_report_mode_modules_are_the_imported_ones(package, warn_only):
    root, package_name = package
    _write(root, package_name, {"a.py": _spider("SpiderA")})
    loader = _loader(package_name, SPIDER_LOADER_WARN_ONLY=warn_only)
    assert loader.list() == ["spider_a"]
    report = loader.discovery_report
    assert report is not None
    assert report.modules == (package_name, f"{package_name}.a")


def test_reload_without_report_mode_is_none_and_keeps_registry(package):
    root, package_name = package
    _write(root, package_name, {"a.py": _spider("SpiderA")})
    loader = SpiderLoader.from_settings(Settings({"SPIDER_MODULES": [package_name]}))
    assert loader.reload() is None
    assert loader.list() == ["spider_a"]
    assert loader.discovery_report is None


def test_report_mode_does_not_import_extra_modules(package, monkeypatch):
    root, package_name = package
    _write(root, package_name, {"a.py": _spider("SpiderA")})
    imported: list[str] = []
    real_import_module = __import__("importlib").import_module

    def _tracking_import_module(name: str, package: str | None = None):
        imported.append(name)
        return real_import_module(name, package)

    monkeypatch.setattr("scrapy.spiderloader.import_module", _tracking_import_module)
    loader = _loader(package_name)
    assert "spider_discovery_extra" not in imported
    assert set(imported) == {
        package_name,
        f"{package_name}.a",
    }
    assert loader.list() == ["spider_a"]


def test_legacy_warnings_are_still_emitted_in_report_mode(package):
    root, package_name = package
    _write(
        root,
        package_name,
        {"a.py": _spider("SpiderA"), "b.py": _spider("SpiderA")},
    )
    with pytest.warns(UserWarning, match="several spiders with the same name"):
        _loader(package_name, SPIDER_LOADER_WARN_ONLY=True)


def test_report_of_failed_strict_attempt_is_kept_for_next_reload(package):
    root, package_name = package
    _write(root, package_name, {"a.py": _spider("SpiderA")})
    loader = _loader(package_name)
    _write(root, package_name, {"b.py": _spider("SpiderA")})
    with pytest.raises(SpiderDiscoveryError) as excinfo:
        loader.reload()
    assert excinfo.value.report.version == 2
    _write(root, package_name, {"b.py": _spider("SpiderB")})
    report = loader.reload()
    assert report is not None
    assert report.version == 3
    assert report.published
    assert loader.list() == ["spider_a", "spider_b"]
