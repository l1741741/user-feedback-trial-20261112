"""
Item Exporters are used to export/serialize items into different formats.
"""

from __future__ import annotations

import csv
import logging
import marshal
import pickle
import pprint
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from io import BytesIO, TextIOWrapper
from typing import TYPE_CHECKING, Any, cast
from xml.sax.saxutils import XMLGenerator
from xml.sax.xmlreader import AttributesImpl

from itemadapter import ItemAdapter, is_item

from scrapy.utils.python import is_listlike, to_unicode
from scrapy.utils.serialize import ScrapyJSONEncoder

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from json import JSONEncoder
    from typing import IO

    from scrapy.item import Field

logger = logging.getLogger(__name__)

_SCALAR_TYPES: frozenset[type] = frozenset({bool, bytes, float, int, str, type(None)})


def _annotate_exception(exc: Exception, info: str) -> Exception:
    """Return an exception like *exc* that records *info* as a note and, where
    the type allows it, in the message too, so that callers that only look at
    the text see it.

    Encoding errors build their message when created, from the text that
    failed to encode, which may contain a field value. Those get a replacement
    exception of the same type built from placeholder text, and the original is
    scrubbed too, since Python keeps it as the context of the replacement.
    Callers pass field names or limits, never field values.
    """
    if isinstance(exc, UnicodeError) and len(exc.args) == 5:
        encoding, text, start, end, reason = exc.args
        placeholders = "?" * len(text)
        exc.args = (encoding, placeholders, start, end, reason)
        cast("Any", exc).object = placeholders
        replacement = type(exc)(
            encoding, placeholders, start, end, f"{reason} ({info})"
        )
        replacement.add_note(info)
        return replacement
    exc.add_note(info)
    if len(exc.args) == 1 and isinstance(exc.args[0], str):
        exc.args = (f"{exc.args[0]} ({info})",)
    return exc


__all__ = [
    "BaseItemExporter",
    "CsvItemExporter",
    "JsonItemExporter",
    "JsonLinesItemExporter",
    "MarshalItemExporter",
    "PickleItemExporter",
    "PprintItemExporter",
    "XmlItemExporter",
]


class BaseItemExporter(ABC):
    def __init__(self, *, dont_fail: bool = False, **kwargs: Any):
        self._kwargs: dict[str, Any] = kwargs
        self._configure(kwargs, dont_fail=dont_fail)

    def _configure(self, options: dict[str, Any], dont_fail: bool = False) -> None:
        """Configure the exporter by popping options from the ``options`` dict.
        If dont_fail is set, it won't raise an exception on unexpected options
        (useful for using with keyword arguments in subclasses ``__init__`` methods)
        """
        self.encoding: str | None = options.pop("encoding", None)
        self.fields_to_export: Mapping[str, str] | Iterable[str] | None = options.pop(
            "fields_to_export", None
        )
        self.export_empty_fields: bool = options.pop("export_empty_fields", False)
        self.indent: int | None = options.pop("indent", None)
        if not dont_fail and options:
            raise TypeError(f"Unexpected options: {', '.join(options.keys())}")

    @abstractmethod
    def export_item(self, item: Any) -> Coroutine[Any, Any, None] | None:
        """Exports the given item. This method must be implemented in
        subclasses.

        .. versionchanged:: 2.20.0
           This method may now be a coroutine function (``async def``). Scrapy
           awaits its result when the exporter is used by the :ref:`feed
           exports <topics-feed-exports>`.
        """
        raise NotImplementedError

    def serialize_field(
        self, field: Mapping[str, Any] | Field, name: str, value: Any
    ) -> Any:
        serializer: Callable[[Any], Any] = field.get(
            "serializer", self._serialize_value
        )
        return serializer(value)

    def _serialize_value(self, value: Any) -> Any:
        if type(value) in _SCALAR_TYPES:
            return value
        if is_item(value):
            adapter = ItemAdapter(value)
            field_names = (
                adapter.field_names()
                if self.export_empty_fields
                else self._get_populated_field_names(adapter)
            )
            return {
                name: self.serialize_field(
                    adapter.get_field_meta(name), name, adapter[name]
                )
                for name in field_names
            }
        if is_listlike(value):
            return [self._serialize_value(v) for v in value]
        return value

    def start_exporting(self) -> Coroutine[Any, Any, None] | None:  # noqa: B027
        """Signal the beginning of the exporting process. Some exporters may
        use this to generate some required header (for example, the
        :class:`XmlItemExporter`). You must call this method before exporting
        any items.

        .. versionchanged:: 2.20.0
           This method may now be a coroutine function (``async def``). Scrapy
           awaits its result when the exporter is used by the :ref:`feed
           exports <topics-feed-exports>`.
        """

    def finish_exporting(self) -> Coroutine[Any, Any, None] | None:  # noqa: B027
        """Signal the end of the exporting process. Some exporters may use this
        to generate some required footer (for example, the
        :class:`XmlItemExporter`). You must always call this method after you
        have no more items to export.

        .. versionchanged:: 2.20.0
           This method may now be a coroutine function (``async def``). Scrapy
           awaits its result when the exporter is used by the :ref:`feed
           exports <topics-feed-exports>`.
        """

    @staticmethod
    def _get_populated_field_names(adapter: ItemAdapter) -> Iterable[str]:
        """Return the populated field names of *adapter*, in declaration order.

        Populated fields that are not declared, which some item types allow,
        come last, in item order.
        """
        populated = set(adapter.keys())
        declared = (name for name in adapter.field_names() if name in populated)
        return dict.fromkeys([*declared, *adapter.keys()])

    def _iter_field_names(
        self, item: ItemAdapter, include_empty: bool
    ) -> Iterable[tuple[str, str]]:
        """Yield the ``(item_field, output_field)`` pairs to export for *item*,
        taking :attr:`fields_to_export` and *include_empty* into account."""
        field_iter: Iterable[str] | Iterable[tuple[str, str]]
        if self.fields_to_export is None:
            field_iter = (
                item.field_names()
                if include_empty
                else self._get_populated_field_names(item)
            )
        elif isinstance(self.fields_to_export, Mapping):
            if include_empty:
                field_iter = self.fields_to_export.items()
            else:
                field_iter = (
                    (x, y) for x, y in self.fields_to_export.items() if x in item
                )
        elif include_empty:
            field_iter = self.fields_to_export
        else:
            field_iter = (x for x in self.fields_to_export if x in item)

        for field_name in field_iter:
            if isinstance(field_name, str):
                yield field_name, field_name
            else:
                yield field_name

    def _iter_serialized_fields(
        self,
        item: Any,
        default_value: Any = None,
        include_empty: bool | None = None,
        *,
        annotate_errors: bool = False,
    ) -> Iterable[tuple[str, Any]]:
        """Yield the ``(name, serialized_value)`` pairs to export for *item*.

        If *annotate_errors* is true, exceptions raised while serializing a
        field name the field. The field value is never included.
        """
        adapter = ItemAdapter(item)

        if include_empty is None:
            include_empty = self.export_empty_fields

        for item_field, output_field in self._iter_field_names(adapter, include_empty):
            if item_field in adapter:
                field_meta = adapter.get_field_meta(item_field)
                try:
                    value = self.serialize_field(
                        field_meta, output_field, adapter[item_field]
                    )
                except Exception as exc:
                    if annotate_errors:
                        raise (
                            _annotate_exception(
                                exc, f"While serializing field {output_field!r}"
                            )
                        ) from None
                    raise
            else:
                value = default_value

            yield output_field, value

    def get_serialized_fields(
        self, item: Any, default_value: Any = None, include_empty: bool | None = None
    ) -> Iterable[tuple[str, Any]]:
        """Return the fields of *item* to export, as an iterable of
        ``(name, serialized_value)`` tuples, taking :attr:`fields_to_export`
        into account and applying :meth:`serialize_field` to every value.

        Fields missing from *item* are exported with *default_value*.

        *include_empty* overrides :attr:`export_empty_fields`.
        """
        yield from self._iter_serialized_fields(item, default_value, include_empty)


class JsonLinesItemExporter(BaseItemExporter):
    """Exports items as one JSON-encoded line each.

    With *atomic_items*, the exporter serializes, encodes and terminates an
    item in memory before writing it with a single ``write()`` call, so that a
    failure in any of those steps writes nothing for that item. This is item
    validation atomicity only: bytes already written by a failing or short
    ``write()`` call cannot be taken back, and the exporter never seeks or
    truncates the stream to try. After such a write error the exporter refuses
    further items.

    *max_item_bytes*, which only applies with *atomic_items*, limits the size
    of the complete encoded line, including the trailing newline; ``0`` means
    no limit.

    .. versionadded:: VERSION
       The *atomic_items* and *max_item_bytes* parameters.
    """

    def __init__(
        self,
        file: IO[bytes],
        *,
        atomic_items: bool = False,
        max_item_bytes: int = 0,
        **kwargs: Any,
    ):
        if isinstance(max_item_bytes, bool) or not isinstance(max_item_bytes, int):
            raise ValueError(
                "max_item_bytes must be 0 or a positive integer, "
                f"got {max_item_bytes!r}"
            )
        if max_item_bytes < 0:
            raise ValueError(
                "max_item_bytes must be 0 or a positive integer, "
                f"got {max_item_bytes!r}"
            )
        super().__init__(dont_fail=True, **kwargs)
        self.file: IO[bytes] = file
        self._atomic_items = atomic_items
        self._max_item_bytes = max_item_bytes
        self._failed = False
        self._kwargs.setdefault("ensure_ascii", not self.encoding)
        self.encoder: JSONEncoder = ScrapyJSONEncoder(**self._kwargs)

    def export_item(self, item: Any) -> None:
        if not self._atomic_items:
            itemdict = dict(self.get_serialized_fields(item))
            data = self.encoder.encode(itemdict) + "\n"
            self.file.write(data.encode(self.encoding or "utf-8"))
            return
        if self._failed:
            raise RuntimeError(
                "Cannot export items: an earlier write to the output stream "
                "failed or was incomplete."
            )
        line = self._encode_item(item)
        try:
            written = self.file.write(line)
        except Exception:
            self._failed = True
            raise
        if isinstance(written, int) and written < len(line):
            self._failed = True
            raise OSError(
                f"Short write: {written} of {len(line)} bytes went into the "
                "output stream."
            )

    def _encode_item(self, item: Any) -> bytes:
        """Return the complete encoded line for *item*, without writing.

        Failures identify the field or the limit that caused them, never the
        field value.
        """
        fields = list(self._get_field_pairs(item))
        try:
            line = self.encoder.encode(dict(fields)) + "\n"
        except Exception as exc:
            raise self._add_error_info(exc, fields, "JSON-encoding") from None
        try:
            data = line.encode(self.encoding or "utf-8")
        except Exception as exc:
            raise (
                self._add_error_info(exc, fields, "encoding", self.encoding)
            ) from None
        if self._max_item_bytes and len(data) > self._max_item_bytes:
            raise ValueError(
                f"The encoded line for this item is {len(data)} bytes long, "
                f"over the max_item_bytes limit of {self._max_item_bytes}."
            )
        return data

    def _get_field_pairs(self, item: Any) -> Iterable[tuple[str, Any]]:
        """Return the serialized fields of *item*, naming the field in
        serialization errors.

        An overridden :meth:`~BaseItemExporter.get_serialized_fields` decides
        the fields, so it is called instead.
        """
        if (
            type(self).get_serialized_fields
            is not BaseItemExporter.get_serialized_fields
        ):
            return self.get_serialized_fields(item)
        return self._iter_serialized_fields(item, annotate_errors=True)

    def _add_error_info(
        self,
        exc: Exception,
        fields: list[tuple[str, Any]],
        action: str,
        encoding: str | None = None,
    ) -> Exception:
        """Return an exception that names the field whose value made *exc*
        happen.

        Encoding the whole item failed, so each field is encoded on its own to
        find the culprit. A value that only fails in combination with others
        leaves all the field names in the message.
        """
        for name, value in fields:
            try:
                self.encoder.encode({name: value}).encode(encoding or "utf-8")
            except Exception:
                return _annotate_exception(exc, f"While {action} field {name!r}")
        names = ", ".join(map(str, (name for name, _ in fields)))
        return _annotate_exception(exc, f"While {action} fields: {names}")


class JsonItemExporter(BaseItemExporter):
    def __init__(self, file: BytesIO, **kwargs: Any):
        super().__init__(dont_fail=True, **kwargs)
        self.file: BytesIO = file
        # there is a small difference between the behaviour or JsonItemExporter.indent
        # and ScrapyJSONEncoder.indent. ScrapyJSONEncoder.indent=None is needed to prevent
        # the addition of newlines everywhere
        json_indent = (
            self.indent if self.indent is not None and self.indent > 0 else None
        )
        self._kwargs.setdefault("indent", json_indent)
        self._kwargs.setdefault("ensure_ascii", not self.encoding)
        self.encoder = ScrapyJSONEncoder(**self._kwargs)
        self.first_item = True

    def _beautify_newline(self) -> None:
        if self.indent is not None:
            self.file.write(b"\n")

    def _add_comma_after_first(self) -> None:
        if self.first_item:
            self.first_item = False
        else:
            self.file.write(b",")
            self._beautify_newline()

    def start_exporting(self) -> None:
        self.file.write(b"[")
        self._beautify_newline()

    def finish_exporting(self) -> None:
        self._beautify_newline()
        self.file.write(b"]")

    def export_item(self, item: Any) -> None:
        itemdict = dict(self.get_serialized_fields(item))
        data = self.encoder.encode(itemdict).encode(self.encoding or "utf-8")
        self._add_comma_after_first()
        self.file.write(data)


class XmlItemExporter(BaseItemExporter):
    def __init__(self, file: BytesIO, **kwargs: Any):
        self.item_element = kwargs.pop("item_element", "item")
        self.root_element = kwargs.pop("root_element", "items")
        super().__init__(**kwargs)
        if not self.encoding:
            self.encoding = "utf-8"
        # copied from xml.sax.saxutils._gettextwriter()
        self.stream = TextIOWrapper(
            file,
            encoding=self.encoding,
            errors="xmlcharrefreplace",
            newline="\n",
            write_through=True,
        )
        self.xg = XMLGenerator(self.stream, encoding=self.encoding)

    def _beautify_newline(self, new_item: bool = False) -> None:
        if self.indent is not None and (self.indent > 0 or new_item):
            self.xg.characters("\n")

    def _beautify_indent(self, depth: int = 1) -> None:
        if self.indent:
            self.xg.characters(" " * self.indent * depth)

    def start_exporting(self) -> None:
        self.xg.startDocument()
        self.xg.startElement(self.root_element, AttributesImpl({}))
        self._beautify_newline(new_item=True)

    def export_item(self, item: Any) -> None:
        self._beautify_indent(depth=1)
        self.xg.startElement(self.item_element, AttributesImpl({}))
        self._beautify_newline()
        for name, value in self.get_serialized_fields(item, default_value=""):
            self._export_xml_field(name, value, depth=2)
        self._beautify_indent(depth=1)
        self.xg.endElement(self.item_element)
        self._beautify_newline(new_item=True)

    def finish_exporting(self) -> None:
        self.xg.endElement(self.root_element)
        self.xg.endDocument()
        self.stream.detach()  # Avoid closing the wrapped file.

    def _export_xml_field(self, name: str, serialized_value: Any, depth: int) -> None:
        self._beautify_indent(depth=depth)
        self.xg.startElement(name, AttributesImpl({}))
        if hasattr(serialized_value, "items"):
            self._beautify_newline()
            for subname, value in serialized_value.items():
                self._export_xml_field(subname, value, depth=depth + 1)
            self._beautify_indent(depth=depth)
        elif is_listlike(serialized_value):
            self._beautify_newline()
            for value in serialized_value:
                self._export_xml_field("value", value, depth=depth + 1)
            self._beautify_indent(depth=depth)
        elif isinstance(serialized_value, str):
            self.xg.characters(serialized_value)
        else:
            self.xg.characters(str(serialized_value))
        self.xg.endElement(name)
        self._beautify_newline()


class CsvItemExporter(BaseItemExporter):
    def __init__(
        self,
        file: BytesIO,
        include_headers_line: bool = True,
        join_multivalued: str = ",",
        errors: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(dont_fail=True, **kwargs)
        if not self.encoding:
            self.encoding = "utf-8"
        self.include_headers_line = include_headers_line
        self.stream = TextIOWrapper(
            file,
            line_buffering=False,
            write_through=True,
            encoding=self.encoding,
            newline="",  # Windows needs this https://github.com/scrapy/scrapy/issues/3034
            errors=errors,
        )
        self.csv_writer = csv.writer(self.stream, **self._kwargs)
        self._headers_not_written = True
        self._join_multivalued = join_multivalued
        self._autodetected_fields = False
        self._data_loss_warned = False

    def serialize_field(
        self, field: Mapping[str, Any] | Field, name: str, value: Any
    ) -> Any:
        serializer: Callable[[Any], Any] = field.get("serializer", self._join_if_needed)
        return serializer(value)

    def _join_if_needed(self, value: Any) -> Any:
        value = self._serialize_value(value)
        if isinstance(value, (list, tuple)):
            try:
                return self._join_multivalued.join(value)
            except TypeError:  # list in value may not contain strings
                pass
        return value

    def export_item(self, item: Any) -> None:
        if self._headers_not_written:
            self._headers_not_written = False
            self._write_headers_and_set_fields_to_export(item)

        if (
            self._autodetected_fields
            and self.fields_to_export is not None
            and not self._data_loss_warned
        ):
            item_fields = ItemAdapter(item).field_names()
            dropped_fields = set(item_fields) - set(self.fields_to_export)

            if dropped_fields:
                dropped_fields_display = sorted(dropped_fields)
                logger.warning(
                    f"CSVExporter dropped fields {dropped_fields_display}. "
                    f"To avoid this, fully configure your FEED_EXPORT_FIELDS setting. "
                    f"See: https://docs.scrapy.org/en/latest/topics/feed-exports.html#feed-export-fields",
                )
                self._data_loss_warned = True
        fields = self.get_serialized_fields(item, default_value="", include_empty=True)
        values = list(self._build_row(x for _, x in fields))
        self.csv_writer.writerow(values)

    def finish_exporting(self) -> None:
        self.stream.detach()  # Avoid closing the wrapped file.

    def _build_row(self, values: Iterable[Any]) -> Iterable[Any]:
        for s in values:
            try:
                yield to_unicode(s, self.encoding)
            except TypeError:
                yield s

    def _write_headers_and_set_fields_to_export(self, item: Any) -> None:
        if self.include_headers_line:
            if not self.fields_to_export:
                # use declared field names, or keys if the item is a dict
                self.fields_to_export = ItemAdapter(item).field_names()
                self._autodetected_fields = True
            fields: Iterable[str]
            if isinstance(self.fields_to_export, Mapping):
                fields = self.fields_to_export.values()
            else:
                assert self.fields_to_export
                fields = self.fields_to_export
            row = list(self._build_row(fields))
            self.csv_writer.writerow(row)


class PickleItemExporter(BaseItemExporter):
    def __init__(self, file: BytesIO, protocol: int = 4, **kwargs: Any):
        super().__init__(**kwargs)
        self.file: BytesIO = file
        self.protocol: int = protocol

    def export_item(self, item: Any) -> None:
        d = dict(self.get_serialized_fields(item))
        pickle.dump(d, self.file, self.protocol)


class MarshalItemExporter(BaseItemExporter):
    """Exports items in a Python-specific binary format (see
    :mod:`marshal`).

    :param file: The file-like object to use for exporting the data. Its
                 ``write`` method should accept :class:`bytes` (a disk file
                 opened in binary mode, a :class:`~io.BytesIO` object, etc)
    """

    def __init__(self, file: BytesIO, **kwargs: Any):
        super().__init__(**kwargs)
        self.file: BytesIO = file

    def export_item(self, item: Any) -> None:
        marshal.dump(dict(self.get_serialized_fields(item)), self.file)


class PprintItemExporter(BaseItemExporter):
    def __init__(self, file: BytesIO, **kwargs: Any):
        super().__init__(**kwargs)
        self.file: BytesIO = file

    def export_item(self, item: Any) -> None:
        itemdict = dict(self.get_serialized_fields(item))
        self.file.write(pprint.pformat(itemdict).encode() + b"\n")


class PythonItemExporter(BaseItemExporter):
    """It serializes items to built-in Python types, so that any
    serialization library (e.g. :mod:`json` or msgpack_) can be used on top
    of it.

    .. _msgpack: https://pypi.org/project/msgpack/
    """

    def _configure(self, options: dict[str, Any], dont_fail: bool = False) -> None:
        super()._configure(options, dont_fail)
        if not self.encoding:
            self.encoding = "utf-8"

    def _serialize_value(self, value: Any) -> Any:
        if isinstance(value, (str, bytes)):
            return to_unicode(value, encoding=self.encoding)
        return super()._serialize_value(value)

    def export_item(self, item: Any) -> dict[str | bytes, Any]:  # type: ignore[override]
        result: dict[str | bytes, Any] = dict(self.get_serialized_fields(item))
        return result
