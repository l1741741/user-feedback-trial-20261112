from __future__ import annotations

import base64
import copy
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)

_BYTES_TAG = "__scrapy_bytes_base64__"


class _UnsupportedValue:
    """A JSON ``null`` placeholder for a value that cannot be serialized."""


def _convert_value(
    value: Any,
    path: str,
    unsupported: list[dict[str, str]],
    seen: set[int] | None = None,
) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, bytes):
        return {_BYTES_TAG: base64.b64encode(value).decode("ascii")}
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, dict | list | tuple):
        value_id = id(value)
        if seen is None:
            seen = set()
        if value_id in seen:
            type_name = f"{type(value).__module__}.{type(value).__qualname__}"
            logger.warning(
                f"Cannot serialize circular reference at stat {path!r} "
                f"of type {type_name!r}; writing null to the JSON stats "
                "snapshot"
            )
            unsupported.append({"path": path, "type": type_name})
            return _UnsupportedValue()
        seen.add(value_id)
        try:
            if isinstance(value, dict):
                return {
                    str(key): _convert_value(item, f"{path}.{key}", unsupported, seen)
                    for key, item in value.items()
                }
            return [
                _convert_value(item, f"{path}[{index}]", unsupported, seen)
                for index, item in enumerate(value)
            ]
        finally:
            seen.remove(value_id)
    type_name = f"{type(value).__module__}.{type(value).__qualname__}"
    logger.warning(
        f"Cannot serialize stat {path!r} of type {type_name!r}; "
        "writing null to the JSON stats snapshot"
    )
    unsupported.append({"path": path, "type": type_name})
    return _UnsupportedValue()


def _build_snapshot(
    spider_name: str | None, reason: str | None, stats: dict[str, Any]
) -> dict[str, Any]:
    unsupported: list[dict[str, str]] = []
    converted_stats = _convert_value(copy.deepcopy(stats), "stats", unsupported)
    return {
        "spider_name": spider_name,
        "close_reason": reason,
        "stats": converted_stats,
        "unsupported_types": unsupported,
    }


class _StatsJSONEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        # Every value reachable from the stats is converted by
        # _convert_value(); a marker object becomes null, and anything else
        # reaching this point is a bug in the conversion.
        if isinstance(obj, _UnsupportedValue):
            return None
        raise TypeError(
            f"Object of type {type(obj).__module__}."
            f"{type(obj).__qualname__} is not JSON serializable"
        )


def _write_snapshot_atomic(snapshot: dict[str, Any], path: Path) -> None:
    payload = json.dumps(
        snapshot,
        sort_keys=True,
        ensure_ascii=False,
        indent=2,
        cls=_StatsJSONEncoder,
    ).encode("utf-8")
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(payload)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        temp_path.replace(path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                logger.warning(
                    f"Could not remove temporary stats snapshot file {temp_path!r}",
                    exc_info=True,
                )


def _resolve_snapshot_path(template: str, spider_name: str | None) -> Path | None:
    if "%(spider)s" not in template:
        return Path(template)
    if not spider_name:
        logger.warning(
            "STATS_JSON_PATH contains the %(spider)s placeholder but no "
            "spider name is available; skipping the JSON stats snapshot"
        )
        return None
    # Percent-encode every character that is not unreserved so the spider
    # name can never contribute path separators or other path components.
    return Path(template % {"spider": quote(spider_name, safe="")})


def _write_stats_snapshot(
    template: str,
    spider_name: str | None,
    reason: str | None,
    stats: dict[str, Any],
) -> bool:
    """Write a JSON stats snapshot and return whether a file was replaced."""
    path = _resolve_snapshot_path(template, spider_name)
    if path is None:
        return False
    snapshot = _build_snapshot(spider_name, reason, stats)
    _write_snapshot_atomic(snapshot, path)
    return True
