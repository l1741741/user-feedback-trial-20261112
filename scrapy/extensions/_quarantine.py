"""Per-item isolation for feed exports.

When isolation is enabled for a feed, an item whose serialization fails
because one of its fields cannot be converted or encoded is recorded in a
separate rejection manifest instead of aborting the whole feed. Failures of
the output itself (the feed file, its storage or a compression plugin) are
not attributed to items: they happen while writing already-encoded data, so
they propagate as they do without isolation.
"""

from __future__ import annotations

import json
from contextlib import contextmanager, suppress
from typing import IO, TYPE_CHECKING, Any
from urllib.parse import urlparse

from itemadapter import ItemAdapter
from w3lib.url import file_uri_to_path

from scrapy.utils.serialize import ScrapyJSONEncoder

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from scrapy.exporters import BaseItemExporter


class _SinkFailureMarker:
    """Wraps the file-like object exporters write into.

    Every method that emits bytes forwards to the wrapped object while
    flagging the duration of the call on :attr:`pending`. A failure whose
    traceback crosses one of these calls therefore originates in the output
    chain (disk, storage or compression), not in item serialization, and
    must never be treated as a bad item. Other file methods and attributes
    are delegated, so the wrapper keeps behaving like the wrapped object,
    including when an exporter wraps it again in :class:`io.TextIOWrapper`.
    """

    #: Methods that emit bytes, and hence mark the output chain as active.
    _emitting = frozenset({"write", "writelines", "flush"})

    def __init__(self, file: IO[Any]):
        self.file: IO[Any] = file
        self.pending: bool = False

    def _mark(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self.pending = True
        try:
            return getattr(self.file, name)(*args, **kwargs)
        finally:
            self.pending = False

    def write(self, data: Any) -> Any:
        return self._mark("write", data)

    def writelines(self, lines: Any) -> Any:
        return self._mark("writelines", lines)

    def flush(self) -> Any:
        return self._mark("flush")

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes not defined on this class, so the
        # emitting methods and self.file are never resolved through here.
        return getattr(self.file, name)


def _sink_failed(exc: BaseException, marker: _SinkFailureMarker) -> bool:
    """Return whether *exc* was raised by the marked output chain.

    A marker whose call is still active caught the failure itself, which
    also covers exceptions raised inside compression plugins and the
    storage file while they write already-encoded data. The traceback is
    walked as well so that a failure re-raised after the marker call
    unwound, or chained from another exception, is still recognized.
    """
    if marker.pending:
        return True
    cause: BaseException | None = exc
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        traceback = cause.__traceback__
        while traceback is not None:
            frame = traceback.tb_frame
            if frame.f_locals.get("self") is marker and frame.f_code.co_name in (
                "write",
                "writelines",
                "flush",
                "_mark",
            ):
                return True
            traceback = traceback.tb_next
        cause = cause.__cause__ or cause.__context__
    return False


@contextmanager
def _track_field_path(exporter: BaseItemExporter) -> Iterator[dict[str, Any]]:
    """Track the field currently being serialized by *exporter*.

    The returned mapping exposes, under ``"path"``, the best-effort field
    path: the name of the top-level field whose conversion raised when the
    exporter goes through
    :meth:`~scrapy.exporters.BaseItemExporter.serialize_field`, or ``None``
    for exporters that fail outside of field serialization.
    """
    original = exporter.serialize_field
    state: dict[str, Any] = {"path": None, "depth": 0}

    def tracking(field: Any, name: str, value: Any) -> Any:
        if state["depth"] == 0:
            state["path"] = str(name)
        state["depth"] += 1
        try:
            return original(field, name, value)
        finally:
            state["depth"] -= 1

    exporter.serialize_field = tracking  # type: ignore[method-assign]
    try:
        yield state
    finally:
        exporter.serialize_field = original  # type: ignore[method-assign]


def _json_safe(value: Any) -> bool:
    """Return whether *value* can be stored in a JSON document."""
    try:
        ScrapyJSONEncoder().encode(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return True


def _extract_item_id(item: Any, id_fields: tuple[str, ...]) -> dict[str, Any]:
    """Collect the JSON-safe values of the configured identifier fields.

    Only fields present on the item that serialize to JSON are kept, so the
    rejection manifest never embeds an arbitrary, possibly sensitive, item
    and never fails to be written itself.
    """
    if not id_fields:
        return {}
    adapter = ItemAdapter(item)
    item_id: dict[str, Any] = {}
    for name in id_fields:
        if name not in adapter:
            continue
        value = adapter[name]
        if _json_safe(value):
            item_id[name] = value
    return item_id


class _RejectionManifest:
    """JSON Lines document listing the items isolated from one feed slot."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path: Path = path
        self._file: IO[str] = path.open("a", encoding="utf-8")
        self.count: int = 0

    def record(
        self,
        *,
        batch_id: int,
        item_index: int,
        field_path: str | None,
        exception: BaseException,
        item_id: dict[str, Any],
    ) -> None:
        entry = {
            "batch_id": batch_id,
            "item_index": item_index,
            "field_path": field_path,
            "exception_class": type(exception).__name__,
            "exception_module": type(exception).__module__,
            "item_id": item_id,
        }
        self._file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._file.flush()
        self.count += 1

    def close(self, *, remove_if_empty: bool) -> None:
        self._file.close()
        if remove_if_empty and self.count == 0:
            with suppress(FileNotFoundError):
                self.path.unlink()


def rejection_manifest_path(feed_uri: str) -> str:
    """Return the local path of the rejection manifest for *feed_uri*.

    Only local filesystem targets (plain paths and ``file://`` URIs) are
    supported, since the manifest is written next to the feed data.
    """
    scheme = urlparse(feed_uri).scheme
    if scheme == "file":
        return f"{file_uri_to_path(feed_uri)}.rejected.jsonl"
    if scheme == "":
        return f"{feed_uri}.rejected.jsonl"
    raise ValueError(
        f"Cannot write the rejection manifest next to non-local feed URI "
        f"{feed_uri!r}; set the rejection_log_uri feed option to a local path."
    )
