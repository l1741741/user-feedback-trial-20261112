from __future__ import annotations

import traceback
import warnings
from collections import defaultdict
from dataclasses import dataclass
from importlib import import_module
from pkgutil import iter_modules
from typing import TYPE_CHECKING, Protocol, Self, cast

# working around https://github.com/sphinx-doc/sphinx/issues/10400
from scrapy import Request, Spider  # noqa: TC001
from scrapy.utils.misc import load_object, walk_modules_iter
from scrapy.utils.spider import iter_spider_classes

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

    from scrapy.settings import BaseSettings


def _qualified_name(obj: type[object]) -> str:
    return f"{obj.__module__}.{obj.__qualname__}"


@dataclass(frozen=True)
class SpiderClassReport:
    """A spider class found during discovery, as
    :attr:`SpiderDiscoveryReport.classes` reports it.

    .. versionadded:: VERSION
    """

    spider_name: str
    """The spider name, as in :attr:`Spider.name <scrapy.Spider.name>`."""

    qualified_name: str
    """The qualified name of the class, e.g.
    ``"myproject.spiders.example.ExampleSpider"``."""

    published: bool
    """Whether the class is part of the published spiders. A class is not
    published when discovery failed, or when another class also defines its
    spider name and lenient mode leaves ambiguous names out."""


@dataclass(frozen=True)
class SpiderImportFailure:
    """A module that could not be imported or walked, as
    :attr:`SpiderDiscoveryReport.import_failures` reports it.

    .. versionadded:: VERSION
    """

    module: str
    """The module that failed."""

    exception: type[BaseException]
    """The class of the exception raised while importing or walking the
    module."""


@dataclass(frozen=True)
class SpiderNameConflict:
    """A spider name defined by more than one class, as
    :attr:`SpiderDiscoveryReport.conflicts` reports it.

    .. versionadded:: VERSION
    """

    name: str
    """The conflicting spider name."""

    classes: tuple[str, ...]
    """The sorted qualified names of the conflicting classes."""


@dataclass(frozen=True)
class SpiderDiscoveryReport:
    """The outcome of a spider discovery attempt, as
    :attr:`SpiderLoader.discovery_report
    <scrapy.spiderloader.SpiderLoader.discovery_report>` exposes it.

    Every discovery attempt of a loader, including a failed one, gets the next
    version of that loader, so the reports of a loader construction and of an
    explicit :meth:`~scrapy.spiderloader.SpiderLoader.reload` can be told
    apart. Loaders count versions independently, from ``1``.

    .. versionadded:: VERSION
    """

    version: int
    """The version of this report for the loader that produced it."""

    published: bool
    """Whether this attempt replaced the spiders of the loader. A failed
    attempt keeps the previously published spiders."""

    modules: tuple[str, ...]
    """The sorted names of the modules that were imported."""

    classes: tuple[SpiderClassReport, ...]
    """The found spider classes, sorted by qualified name."""

    import_failures: tuple[SpiderImportFailure, ...]
    """The modules that could not be imported or walked, sorted by module name
    and then by exception class."""

    conflicts: tuple[SpiderNameConflict, ...]
    """The spider names defined by more than one class, sorted by name."""


class SpiderDiscoveryError(Exception):
    """Raised by :class:`SpiderLoader` when a discovery attempt fails in
    :ref:`report mode <spider-loader-discovery-report>` in strict mode, i.e.
    with :setting:`SPIDER_LOADER_WARN_ONLY` disabled.

    The previously published spiders are kept, and :attr:`report` describes
    the failed attempt.

    .. versionadded:: VERSION
    """

    report: SpiderDiscoveryReport

    def __init__(self, report: SpiderDiscoveryReport):
        self.report = report
        super().__init__(
            f"Spider discovery failed (report version {report.version}): "
            f"{len(report.import_failures)} module(s) could not be imported, "
            f"and {len(report.conflicts)} spider name(s) are defined by more "
            "than one class. The previously published spiders were kept."
        )


class _SpiderDiscovery:
    """Private candidate tables of a discovery attempt.

    Nothing collected here is visible through the loader until the whole
    attempt has been accepted.
    """

    def __init__(self) -> None:
        # Insertion order is kept for the legacy last-one-wins behavior;
        # reports always sort these tables.
        self.modules: dict[str, None] = {}
        self.classes: dict[int, tuple[str, type[Spider]]] = {}
        self.failures: list[tuple[str, BaseException]] = []

    def add_module(self, module: ModuleType) -> None:
        self.modules[module.__name__] = None

    def add_classes(self, module_name: str, classes: list[type[Spider]]) -> None:
        # Aliased attributes and overlapping SPIDER_MODULES entries can yield
        # the same class object more than once. A class must not be reported as
        # conflicting with itself.
        for spcls in classes:
            self.classes.setdefault(id(spcls), (module_name, spcls))

    def add_failure(self, module: str, exception: BaseException) -> None:
        self.failures.append((module, exception))

    def groups(self) -> dict[str, list[tuple[str, type[Spider]]]]:
        groups: defaultdict[str, list[tuple[str, type[Spider]]]] = defaultdict(list)
        for module_name, spcls in self.classes.values():
            groups[spcls.name].append((module_name, spcls))
        return dict(groups)


def _walk_spider_modules(
    path: str, discovery: _SpiderDiscovery
) -> Iterator[ModuleType]:
    """Like :func:`~scrapy.utils.misc.walk_modules_iter`, but a module that
    cannot be imported is recorded and skipped, so that the other modules of
    the same package are still discovered.

    Modules are imported by name, as the regular walk does; nothing outside
    the given package is ever imported. Exceptions raised by the walk itself,
    as opposed to by an import, are not caught.
    """
    try:
        module = import_module(path)
    except Exception as exc:
        discovery.add_failure(path, exc)
        return
    discovery.add_module(module)
    yield module
    if hasattr(module, "__path__"):
        try:
            subpaths = list(iter_modules(module.__path__))
        except Exception as exc:
            # A failure of the walk itself, not of an import. It aborts
            # discovery, after recording it so that the report shows it.
            discovery.add_failure(path, exc)
            raise
        for _, subpath, ispkg in subpaths:
            fullpath = f"{path}.{subpath}"
            if ispkg:
                yield from _walk_spider_modules(fullpath, discovery)
            else:
                try:
                    submodule = import_module(fullpath)
                except Exception as exc:
                    discovery.add_failure(fullpath, exc)
                else:
                    discovery.add_module(submodule)
                    yield submodule


def get_spider_loader(settings: BaseSettings) -> SpiderLoaderProtocol:
    """Get SpiderLoader instance from settings"""
    cls_path = settings.get("SPIDER_LOADER_CLASS")
    loader_cls = load_object(cls_path)
    return cast("SpiderLoaderProtocol", loader_cls.from_settings(settings.frozencopy()))


class SpiderLoaderProtocol(Protocol):
    """Protocol for spider loader implementations.

    See :setting:`SPIDER_LOADER_CLASS`.
    """

    @classmethod
    def from_settings(cls, settings: BaseSettings) -> Self:
        """Return an instance of the class for the given settings."""

    def load(self, spider_name: str) -> type[Spider]:
        """Return the spider class for the given spider name. If the spider
        name is not found, it must raise a :exc:`KeyError`."""

    def list(self) -> list[str]:
        """Return a list with the names of all spiders available in the
        project."""

    def find_by_request(self, request: Request) -> __builtins__.list[str]:
        """Return the list of spiders names that can handle the given request."""


class SpiderLoader:
    """
    SpiderLoader is a class which locates and loads spiders
    in a Scrapy project.

    .. _spider-loader-discovery-report:

    Discovery reports
    =================

    With :setting:`SPIDER_LOADER_DISCOVERY_REPORT` enabled, every discovery
    attempt first collects the imported modules, the spider classes defined in
    them, and the import failures in private tables. The spiders of the loader
    are replaced with the result only after the whole attempt has been
    collected and accepted, so a failed attempt keeps the previously published
    spiders.

    :attr:`discovery_report` describes the latest attempt, including its
    classes, import failures, name conflicts, and whether it was published.
    For example:

    .. code-block:: python

        from scrapy.spiderloader import get_spider_loader

        loader = get_spider_loader(settings)
        report = loader.discovery_report
        print(report.version, report.published)
        for spider_class in report.classes:
            print(spider_class.qualified_name, spider_class.published)
        for failure in report.import_failures:
            print(failure.module, failure.exception.__name__)

    With :setting:`SPIDER_LOADER_WARN_ONLY` disabled (strict mode), any import
    failure or spider name defined by more than one class makes discovery fail
    with :exc:`SpiderDiscoveryError`, also during :meth:`reload`.

    With :setting:`SPIDER_LOADER_WARN_ONLY` enabled (lenient mode), modules
    that cannot be imported are skipped, and no class of an ambiguous spider
    name is published; both are reported. Without a report, the historical
    behavior is kept instead: import failures are skipped with a warning, and
    the last class found wins an ambiguous name.
    """

    def __init__(self, settings: BaseSettings):
        self.spider_modules: list[str] = settings.getlist("SPIDER_MODULES")
        self.warn_only: bool = settings.getbool("SPIDER_LOADER_WARN_ONLY")
        self._report_enabled: bool = settings.getbool("SPIDER_LOADER_DISCOVERY_REPORT")
        self._spiders: dict[str, type[Spider]] = {}
        self._discovery_report: SpiderDiscoveryReport | None = None
        self._report_version: int = 0
        self._reload()

    @property
    def discovery_report(self) -> SpiderDiscoveryReport | None:
        """The report of the latest discovery attempt, or ``None`` when
        :setting:`SPIDER_LOADER_DISCOVERY_REPORT` is disabled.

        Calling :meth:`load` does not run discovery, so it changes neither
        this report nor its version.

        .. versionadded:: VERSION
        """
        return self._discovery_report

    def reload(self) -> SpiderDiscoveryReport | None:
        """Run discovery again and replace the published spiders with the
        result.

        Return the report of the attempt when
        :setting:`SPIDER_LOADER_DISCOVERY_REPORT` is enabled, and ``None``
        otherwise.

        Each attempt of a loader has the next report version. On failure, the
        previously published spiders are kept: strict mode raises
        :exc:`SpiderDiscoveryError`, while lenient mode publishes the spiders
        that were discovered and reports what was skipped or left out.

        .. versionadded:: VERSION
        """
        return self._reload()

    def _reload(self) -> SpiderDiscoveryReport | None:
        if self._report_enabled:
            self._report_version += 1
        version = self._report_version
        discovery = _SpiderDiscovery()
        try:
            self._collect(discovery)
        except Exception:
            # A failure of discovery itself, not of a module import. Nothing
            # collected so far is published.
            if self._report_enabled:
                self._discovery_report = self._build_report(
                    version, discovery, published=False, excluded=frozenset()
                )
            raise

        groups = discovery.groups()
        conflicts = {
            name: entries for name, entries in groups.items() if len(entries) > 1
        }
        strict = not self.warn_only
        if self._report_enabled and strict and (discovery.failures or conflicts):
            report = self._build_report(
                version, discovery, published=False, excluded=frozenset()
            )
            self._discovery_report = report
            error = SpiderDiscoveryError(report)
            if discovery.failures:
                # Chain the first import failure, while the report keeps them
                # all.
                raise error from discovery.failures[0][1]
            raise error

        # In report mode, lenient discovery does not pick a winner among
        # classes that share a spider name: none of them is published.
        excluded = frozenset(conflicts) if self._report_enabled else frozenset()
        self._publish(discovery, excluded=excluded)
        published_report: SpiderDiscoveryReport | None = None
        if self._report_enabled:
            published_report = self._build_report(
                version, discovery, published=True, excluded=excluded
            )
            self._discovery_report = published_report
        self._check_name_duplicates(groups)
        return published_report

    def _collect(self, discovery: _SpiderDiscovery) -> None:
        for name in self.spider_modules:
            if not self._report_enabled:
                try:
                    for module in walk_modules_iter(name):
                        discovery.add_module(module)
                        self._load_spiders(module, discovery)
                except (ImportError, SyntaxError) as exc:
                    discovery.add_failure(name, exc)
                    if self.warn_only:
                        self._warn_module_failure(name, exc)
                    else:
                        raise
                continue
            for module in _walk_spider_modules(name, discovery):
                try:
                    self._load_spiders(module, discovery)
                except Exception as exc:
                    # A module is collected as a unit: if enumerating its
                    # classes fails, none of them reaches the candidate tables,
                    # and nothing is published.
                    discovery.add_failure(module.__name__, exc)
                    raise
        if self._report_enabled and self.warn_only:
            for failed_module, exception in discovery.failures:
                self._warn_module_failure(failed_module, exception)

    def _load_spiders(self, module: ModuleType, discovery: _SpiderDiscovery) -> None:
        discovery.add_classes(module.__name__, list(iter_spider_classes(module)))

    def _warn_module_failure(self, module: str, exception: BaseException) -> None:
        traceback_text = "".join(
            traceback.format_exception(
                type(exception), exception, exception.__traceback__
            )
        )
        warnings.warn(
            f"\n{traceback_text}Could not load spiders "
            f"from module '{module}'. "
            "See above traceback for details.",
            stacklevel=4,
            category=RuntimeWarning,
        )

    def _publish(
        self, discovery: _SpiderDiscovery, *, excluded: frozenset[str]
    ) -> None:
        spiders: dict[str, type[Spider]] = {}
        for _, spcls in discovery.classes.values():
            name = spcls.name
            if name in excluded:
                continue
            # In report mode there is at most one class per name. Otherwise the
            # historical last-one-wins behavior is kept.
            spiders[name] = spcls
        # Replaced in a single assignment, after everything has been collected
        # and accepted, so readers never see a partial registry.
        self._spiders = spiders

    def _build_report(
        self,
        version: int,
        discovery: _SpiderDiscovery,
        *,
        published: bool,
        excluded: frozenset[str],
    ) -> SpiderDiscoveryReport:
        groups = discovery.groups()
        classes = [
            SpiderClassReport(
                spider_name=spcls.name,
                qualified_name=_qualified_name(spcls),
                published=published and spcls.name not in excluded,
            )
            for _, spcls in discovery.classes.values()
        ]
        classes.sort(key=lambda item: (item.qualified_name, item.spider_name))
        failures = [
            SpiderImportFailure(module, type(exception))
            for module, exception in discovery.failures
        ]
        failures.sort(key=lambda item: (item.module, _qualified_name(item.exception)))
        conflicts = [
            SpiderNameConflict(
                name=name,
                classes=tuple(sorted(_qualified_name(spcls) for _, spcls in entries)),
            )
            for name, entries in groups.items()
            if len(entries) > 1
        ]
        conflicts.sort(key=lambda item: item.name)
        return SpiderDiscoveryReport(
            version=version,
            published=published,
            modules=tuple(sorted(discovery.modules)),
            classes=tuple(classes),
            import_failures=tuple(failures),
            conflicts=tuple(conflicts),
        )

    def _check_name_duplicates(
        self, groups: dict[str, list[tuple[str, type[Spider]]]]
    ) -> None:
        dupes = []
        for name, locations in groups.items():
            dupes.extend(
                [
                    f"  {spcls.__name__} named {name!r} (in {module})"
                    for module, spcls in locations
                    if len(locations) > 1
                ]
            )

        if dupes:
            dupes_string = "\n\n".join(dupes)
            warnings.warn(
                "There are several spiders with the same name:\n\n"
                f"{dupes_string}\n\n  This can cause unexpected behavior.",
                stacklevel=3,
                category=UserWarning,
            )

    @classmethod
    def from_settings(cls, settings: BaseSettings) -> Self:
        """Create an instance of the class.

        It's called with the current project settings, and it loads the spiders
        found recursively in the modules of the :setting:`SPIDER_MODULES`
        setting.
        """
        return cls(settings)

    def load(self, spider_name: str) -> type[Spider]:
        """Return the spider class for the given spider name.

        If the name is not found, raise a :exc:`KeyError`.
        """
        try:
            return self._spiders[spider_name]
        except KeyError:
            raise KeyError(f"Spider not found: {spider_name}") from None

    def find_by_request(self, request: Request) -> list[str]:
        """
        Return the list of spider names that can handle the given request.

        It will try to match the request's url against the domains of
        the spiders.
        """
        return [
            name for name, cls in self._spiders.items() if cls.handles_request(request)
        ]

    def list(self) -> list[str]:
        """Return a list with the names of all spiders available in the project."""
        return list(self._spiders.keys())


class DummySpiderLoader:
    """A dummy spider loader that does not load any spiders."""

    @classmethod
    def from_settings(cls, settings: BaseSettings) -> Self:
        return cls()

    def load(self, spider_name: str) -> type[Spider]:
        raise KeyError("DummySpiderLoader doesn't load any spiders")

    def list(self) -> list[str]:
        return []

    def find_by_request(self, request: Request) -> __builtins__.list[str]:
        return []
