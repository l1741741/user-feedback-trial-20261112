import dataclasses
import json
import marshal
import pickle
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from io import BytesIO
from typing import IO, Any, cast

import lxml.etree
import pytest
from itemadapter import ItemAdapter

from scrapy.exporters import (
    BaseItemExporter,
    CsvItemExporter,
    JsonItemExporter,
    JsonLinesItemExporter,
    MarshalItemExporter,
    PickleItemExporter,
    PprintItemExporter,
    PythonItemExporter,
    XmlItemExporter,
)
from scrapy.item import Field, Item
from scrapy.utils.python import to_unicode
from scrapy.utils.serialize import ScrapyJSONEncoder


def custom_serializer(value):
    return str(int(value) + 2)


class MyItem(Item):
    name = Field()
    age = Field()


class CustomFieldItem(Item):
    name = Field()
    age = Field(serializer=custom_serializer)


class FriendsItem(Item):
    name = Field()
    friends = Field()


@dataclasses.dataclass
class MyDataClass:
    name: str
    age: int


@dataclasses.dataclass
class CustomFieldDataclass:
    name: str
    age: int = dataclasses.field(metadata={"serializer": custom_serializer})


class TestBaseItemExporter(ABC):
    item_class: type = MyItem
    custom_field_item_class: type = CustomFieldItem

    def setup_method(self):
        self.i = self.item_class(name="John\xa3", age="22")
        self.output = BytesIO()
        self.ie = self._get_exporter()

    @abstractmethod
    def _get_exporter(self, **kwargs: Any) -> BaseItemExporter:
        raise NotImplementedError

    def _check_output(self) -> None:  # noqa: B027
        pass

    def _assert_expected_item(self, exported_dict: dict[str, Any]) -> None:
        assert self.i == self.item_class(**exported_dict)

    @staticmethod
    def _get_nonstring_types_item() -> dict[str, Any]:
        return {
            "boolean": False,
            "number": 22,
            "time": datetime(2015, 1, 1, 1, 1, 1, tzinfo=UTC),
            "float": 3.14,
        }

    def assertItemExportWorks(self, item: Any) -> None:
        self.ie.start_exporting()
        self.ie.export_item(item)
        self.ie.finish_exporting()
        # Delete the item exporter object, so that if it causes the output
        # file handle to be closed, which should not be the case, follow-up
        # interactions with the output file handle will surface the issue.
        del self.ie
        self._check_output()

    def test_export_item(self) -> None:
        self.assertItemExportWorks(self.i)

    def test_export_dict_item(self):
        self.assertItemExportWorks(ItemAdapter(self.i).asdict())

    def test_serialize_field(self):
        a = ItemAdapter(self.i)
        res = self.ie.serialize_field(a.get_field_meta("name"), "name", a["name"])
        assert res == "John\xa3"

        res = self.ie.serialize_field(a.get_field_meta("age"), "age", a["age"])
        assert res == "22"

    def test_fields_to_export(self):
        ie = self._get_exporter(fields_to_export=["name"])
        assert list(ie.get_serialized_fields(self.i)) == [("name", "John\xa3")]

        ie = self._get_exporter(fields_to_export=["name"], encoding="latin-1")
        _, name = next(iter(ie.get_serialized_fields(self.i)))
        assert isinstance(name, str)
        assert name == "John\xa3"

        ie = self._get_exporter(fields_to_export={"name": "名稱"})
        assert list(ie.get_serialized_fields(self.i)) == [("名稱", "John\xa3")]

    def test_field_order(self):
        item = self.item_class(age="22", name="John\xa3")
        ie = self._get_exporter()
        assert [name for name, _ in ie.get_serialized_fields(item)] == ["name", "age"]

    def test_field_order_dict_item(self):
        ie = self._get_exporter()
        assert [name for name, _ in ie.get_serialized_fields({"age": "22"})] == ["age"]
        assert [
            name for name, _ in ie.get_serialized_fields({"age": "22", "name": "John"})
        ] == ["age", "name"]

    def test_field_custom_serializer(self):
        i = self.custom_field_item_class(name="John\xa3", age="22")
        a = ItemAdapter(i)
        ie = self._get_exporter()
        assert (
            ie.serialize_field(a.get_field_meta("name"), "name", a["name"])
            == "John\xa3"
        )
        assert ie.serialize_field(a.get_field_meta("age"), "age", a["age"]) == "24"

    def test_nested_item_field_custom_serializer(self):
        nested = self.custom_field_item_class(name="Nested\xa3", age="22")
        a = ItemAdapter(self.item_class(name="Top", age=nested))
        ie = self._get_exporter()
        assert ie.serialize_field(a.get_field_meta("age"), "age", a["age"]) == {
            "name": "Nested\xa3",
            "age": "24",
        }

    def test_nested_item_list_field_custom_serializer(self):
        nested = [
            self.custom_field_item_class(name="Nested\xa3", age="22"),
            self.custom_field_item_class(name="Other", age="1"),
        ]
        a = ItemAdapter(self.item_class(name="Top", age=nested))
        ie = self._get_exporter()
        assert ie.serialize_field(a.get_field_meta("age"), "age", a["age"]) == [
            {"name": "Nested\xa3", "age": "24"},
            {"name": "Other", "age": "3"},
        ]

    def test_nested_item_field_ignores_fields_to_export(self):
        nested = self.custom_field_item_class(name="Nested\xa3", age="22")
        a = ItemAdapter(self.item_class(name="Top", age=nested))
        ie = self._get_exporter(fields_to_export=["age"])
        assert ie.serialize_field(a.get_field_meta("age"), "age", a["age"]) == {
            "name": "Nested\xa3",
            "age": "24",
        }


class TestPythonItemExporter(TestBaseItemExporter):
    def _get_exporter(self, **kwargs: Any) -> PythonItemExporter:
        return PythonItemExporter(**kwargs)

    def test_invalid_option(self):
        with pytest.raises(TypeError, match="Unexpected options: invalid_option"):
            PythonItemExporter(invalid_option="something")

    def test_nested_item(self):
        i1 = self.item_class(name="Joseph", age="22")
        i2 = {"name": "Maria", "age": i1}
        i3 = self.item_class(name="Jesus", age=i2)
        ie = self._get_exporter()
        exported = ie.export_item(i3)
        assert isinstance(exported, dict)
        assert exported == {
            "age": {"age": {"age": "22", "name": "Joseph"}, "name": "Maria"},
            "name": "Jesus",
        }
        assert isinstance(exported["age"], dict)
        assert isinstance(exported["age"]["age"], dict)

    def test_export_list(self):
        i1 = self.item_class(name="Joseph", age="22")
        i2 = self.item_class(name="Maria", age=[i1])
        i3 = self.item_class(name="Jesus", age=[i2])
        ie = self._get_exporter()
        exported = ie.export_item(i3)
        assert exported == {
            "age": [{"age": [{"age": "22", "name": "Joseph"}], "name": "Maria"}],
            "name": "Jesus",
        }
        assert exported is not None
        assert isinstance(exported["age"][0], dict)
        assert isinstance(exported["age"][0]["age"][0], dict)

    def test_export_item_dict_list(self):
        i1 = self.item_class(name="Joseph", age="22")
        i2 = {"name": "Maria", "age": [i1]}
        i3 = self.item_class(name="Jesus", age=[i2])
        ie = self._get_exporter()
        exported = ie.export_item(i3)
        assert exported == {
            "age": [{"age": [{"age": "22", "name": "Joseph"}], "name": "Maria"}],
            "name": "Jesus",
        }
        assert exported is not None
        assert isinstance(exported["age"][0], dict)
        assert isinstance(exported["age"][0]["age"][0], dict)

    def test_nonstring_types_item(self):
        item = self._get_nonstring_types_item()
        ie = self._get_exporter()
        exported = ie.export_item(item)
        assert exported == item


class TestPythonItemExporterDataclass(TestPythonItemExporter):
    item_class = MyDataClass
    custom_field_item_class = CustomFieldDataclass


class TestPprintItemExporter(TestBaseItemExporter):
    def _get_exporter(self, **kwargs: Any) -> BaseItemExporter:
        return PprintItemExporter(self.output, **kwargs)

    def _check_output(self) -> None:
        self._assert_expected_item(eval(self.output.getvalue()))


class TestPprintItemExporterDataclass(TestPprintItemExporter):
    item_class = MyDataClass
    custom_field_item_class = CustomFieldDataclass


class TestPickleItemExporter(TestBaseItemExporter):
    def _get_exporter(self, **kwargs: Any) -> BaseItemExporter:
        return PickleItemExporter(self.output, **kwargs)

    def _check_output(self) -> None:
        self._assert_expected_item(pickle.loads(self.output.getvalue()))

    def test_export_multiple_items(self):
        i1 = self.item_class(name="hello", age="world")
        i2 = self.item_class(name="bye", age="world")
        f = BytesIO()
        ie = PickleItemExporter(f)
        ie.start_exporting()
        ie.export_item(i1)
        ie.export_item(i2)
        ie.finish_exporting()
        del ie  # See the first “del self.ie” in this file for context.
        f.seek(0)
        assert self.item_class(**pickle.load(f)) == i1
        assert self.item_class(**pickle.load(f)) == i2

    def test_nonstring_types_item(self):
        item = self._get_nonstring_types_item()
        fp = BytesIO()
        ie = PickleItemExporter(fp)
        ie.start_exporting()
        ie.export_item(item)
        ie.finish_exporting()
        del ie  # See the first “del self.ie” in this file for context.
        assert pickle.loads(fp.getvalue()) == item


class TestPickleItemExporterDataclass(TestPickleItemExporter):
    item_class = MyDataClass
    custom_field_item_class = CustomFieldDataclass


class TestMarshalItemExporter(TestBaseItemExporter):
    def _get_exporter(self, **kwargs: Any) -> BaseItemExporter:
        return MarshalItemExporter(self.output, **kwargs)

    def _check_output(self) -> None:
        self.output.seek(0)
        self._assert_expected_item(marshal.load(self.output))

    def test_nonstring_types_item(self):
        item = self._get_nonstring_types_item()
        item.pop("time")  # datetime is not marshallable
        fp = BytesIO()
        ie = MarshalItemExporter(fp)
        ie.start_exporting()
        ie.export_item(item)
        ie.finish_exporting()
        del ie  # See the first “del self.ie” in this file for context.
        fp.seek(0)
        assert marshal.load(fp) == item
        fp.close()


class TestMarshalItemExporterDataclass(TestMarshalItemExporter):
    item_class = MyDataClass
    custom_field_item_class = CustomFieldDataclass


class TestCsvItemExporter(TestBaseItemExporter):
    def _get_exporter(self, **kwargs: Any) -> BaseItemExporter:
        # We need a fresh instance for each exporter, because
        # CsvItemExporter.stream.__del__() closes the underlying file
        # (CsvItemExporter.finish_exporting() calls detach() but not all tests
        # call it).
        self.output = BytesIO()
        return CsvItemExporter(self.output, **kwargs)

    def assertCsvEqual(
        self, first: bytes | str, second: bytes | str, msg: str | None = None
    ) -> None:
        def split_csv(csv: bytes | str) -> list[list[str]]:
            return [
                sorted(re.split(r"(,|\s+)", line))
                for line in to_unicode(csv).splitlines(True)
            ]

        assert split_csv(first) == split_csv(second), msg

    def _check_output(self) -> None:
        self.output.seek(0)
        self.assertCsvEqual(self.output.read(), "age,name\r\n22,John\xa3\r\n")

    def assertExportResult(
        self, item: Any, expected: bytes | str = b"", **kwargs: Any
    ) -> None:
        fp = BytesIO()
        ie = CsvItemExporter(fp, **kwargs)
        ie.start_exporting()
        ie.export_item(item)
        ie.finish_exporting()
        del ie  # See the first “del self.ie” in this file for context.
        self.assertCsvEqual(fp.getvalue(), expected)

    def test_header_export_all(self):
        self.assertExportResult(
            item=self.i,
            fields_to_export=ItemAdapter(self.i).field_names(),
            expected=b"age,name\r\n22,John\xc2\xa3\r\n",
        )

    def test_header_export_all_dict(self):
        self.assertExportResult(
            item=ItemAdapter(self.i).asdict(),
            expected=b"age,name\r\n22,John\xc2\xa3\r\n",
        )

    @pytest.mark.parametrize("as_dict", [False, True], ids=["item", "dict"])
    def test_header_export_single_field(self, as_dict: bool) -> None:
        self.assertExportResult(
            item=ItemAdapter(self.i).asdict() if as_dict else self.i,
            fields_to_export=["age"],
            expected=b"age\r\n22\r\n",
        )

    @pytest.mark.parametrize("as_dict", [False, True], ids=["item", "dict"])
    def test_header_export_two_items(self, as_dict: bool) -> None:
        item = ItemAdapter(self.i).asdict() if as_dict else self.i
        output = BytesIO()
        ie = CsvItemExporter(output)
        ie.start_exporting()
        ie.export_item(item)
        ie.export_item(item)
        ie.finish_exporting()
        del ie  # See the first “del self.ie” in this file for context.
        self.assertCsvEqual(
            output.getvalue(), b"age,name\r\n22,John\xc2\xa3\r\n22,John\xc2\xa3\r\n"
        )

    @pytest.mark.parametrize("as_dict", [False, True], ids=["item", "dict"])
    def test_header_no_header_line(self, as_dict: bool) -> None:
        self.assertExportResult(
            item=ItemAdapter(self.i).asdict() if as_dict else self.i,
            include_headers_line=False,
            expected=b"22,John\xc2\xa3\r\n",
        )

    @pytest.mark.parametrize("cls", [FriendsItem, dict])
    def test_join_multivalue(self, cls: type) -> None:
        self.assertExportResult(
            item=cls(name="John", friends=["Mary", "Paul"]),
            include_headers_line=False,
            expected='"Mary,Paul",John\r\n',
        )

    def test_join_multivalue_not_strings(self):
        self.assertExportResult(
            item={"name": "John", "friends": [4, 8]},
            include_headers_line=False,
            expected='"[4, 8]",John\r\n',
        )

    def test_nonstring_types_item(self):
        self.assertExportResult(
            item=self._get_nonstring_types_item(),
            include_headers_line=False,
            expected="22,False,3.14,2015-01-01 01:01:01+00:00\r\n",
        )

    def test_errors_default(self):
        with pytest.raises(UnicodeEncodeError):
            self.assertExportResult(
                item={"text": "W\u0275\u200brd"},
                encoding="windows-1251",
            )

    def test_errors_xmlcharrefreplace(self):
        self.assertExportResult(
            item={"text": "W\u0275\u200brd"},
            include_headers_line=False,
            expected="W&#629;&#8203;rd\r\n",
            encoding="windows-1251",
            errors="xmlcharrefreplace",
        )

    def test_csv_dropped_fields_warning(self, caplog):
        out = BytesIO()
        exporter = CsvItemExporter(out)
        exporter.start_exporting()

        exporter.export_item({"name": "Apple"})

        with caplog.at_level("WARNING", logger="scrapy.exporters"):
            exporter.export_item({"name": "Banana", "price": 2.00})

        assert len(caplog.records) == 1
        assert "CSVExporter dropped fields" in caplog.text
        assert "price" in caplog.text


class TestCsvItemExporterDataclass(TestCsvItemExporter):
    item_class = MyDataClass
    custom_field_item_class = CustomFieldDataclass


class TestXmlItemExporter(TestBaseItemExporter):
    def _get_exporter(self, **kwargs: Any) -> BaseItemExporter:
        # We need a fresh instance for each exporter, because
        # XmlItemExporter.stream.__del__() closes the underlying file
        # (XmlItemExporter.finish_exporting() calls detach() but not all tests
        # call it).
        self.output = BytesIO()
        return XmlItemExporter(self.output, **kwargs)

    def assertXmlEquivalent(
        self, first: bytes, second: bytes, msg: str | None = None
    ) -> None:
        def xmltuple(elem: Any) -> list[Any]:
            children = list(elem.iterchildren())
            if children:
                return [(child.tag, sorted(xmltuple(child))) for child in children]
            return [(elem.tag, [(elem.text, ())])]

        def xmlsplit(xmlcontent: bytes) -> list[Any]:
            doc = lxml.etree.fromstring(xmlcontent)
            return xmltuple(doc)

        assert xmlsplit(first) == xmlsplit(second), msg

    def assertExportResult(self, item: Any, expected_value: bytes) -> None:
        fp = BytesIO()
        ie = XmlItemExporter(fp)
        ie.start_exporting()
        ie.export_item(item)
        ie.finish_exporting()
        del ie  # See the first “del self.ie” in this file for context.
        self.assertXmlEquivalent(fp.getvalue(), expected_value)

    def _check_output(self) -> None:
        expected_value = (
            b'<?xml version="1.0" encoding="utf-8"?>\n'
            b"<items><item><age>22</age><name>John\xc2\xa3</name></item></items>"
        )
        self.assertXmlEquivalent(self.output.getvalue(), expected_value)

    def test_multivalued_fields(self):
        self.assertExportResult(
            self.item_class(name=["John\xa3", "Doe"], age=[1, 2, 3]),
            b"""<?xml version="1.0" encoding="utf-8"?>\n
            <items>
                <item>
                    <name><value>John\xc2\xa3</value><value>Doe</value></name>
                    <age><value>1</value><value>2</value><value>3</value></age>
                </item>
            </items>
            """,
        )

    def test_nested_item(self):
        i1 = {"name": "foo\xa3hoo", "age": "22"}
        i2 = {"name": "bar", "age": i1}
        i3 = self.item_class(name="buz", age=i2)

        self.assertExportResult(
            i3,
            b"""<?xml version="1.0" encoding="utf-8"?>\n
                <items>
                    <item>
                        <age>
                            <age>
                                <age>22</age>
                                <name>foo\xc2\xa3hoo</name>
                            </age>
                            <name>bar</name>
                        </age>
                        <name>buz</name>
                    </item>
                </items>
            """,
        )

    def test_nested_list_item(self):
        i1 = {"name": "foo"}
        i2 = {"name": "bar", "v2": {"egg": ["spam"]}}
        i3 = self.item_class(name="buz", age=[i1, i2])

        self.assertExportResult(
            i3,
            b"""<?xml version="1.0" encoding="utf-8"?>\n
                <items>
                    <item>
                        <age>
                            <value><name>foo</name></value>
                            <value><name>bar</name><v2><egg><value>spam</value></egg></v2></value>
                        </age>
                        <name>buz</name>
                    </item>
                </items>
            """,
        )

    def test_nonstring_types_item(self):
        item = self._get_nonstring_types_item()
        self.assertExportResult(
            item,
            b"""<?xml version="1.0" encoding="utf-8"?>\n
                <items>
                   <item>
                       <float>3.14</float>
                       <boolean>False</boolean>
                       <number>22</number>
                       <time>2015-01-01 01:01:01+00:00</time>
                   </item>
                </items>
            """,
        )


class TestXmlItemExporterDataclass(TestXmlItemExporter):
    item_class = MyDataClass
    custom_field_item_class = CustomFieldDataclass


class TestJsonLinesItemExporter(TestBaseItemExporter):
    _expected_nested: Any = {
        "name": "Jesus",
        "age": {"name": "Maria", "age": {"name": "Joseph", "age": "22"}},
    }

    def _get_exporter(self, **kwargs: Any) -> BaseItemExporter:
        return JsonLinesItemExporter(self.output, **kwargs)

    def _check_output(self) -> None:
        exported = json.loads(self.output.getvalue().strip())
        assert exported == ItemAdapter(self.i).asdict()

    def test_nested_item(self):
        i1 = self.item_class(name="Joseph", age="22")
        i2 = {"name": "Maria", "age": i1}
        i3 = self.item_class(name="Jesus", age=i2)
        self.ie.start_exporting()
        self.ie.export_item(i3)
        self.ie.finish_exporting()
        del self.ie  # See the first “del self.ie” in this file for context.
        exported = json.loads(self.output.getvalue())
        assert exported == self._expected_nested

    def test_extra_keywords(self):
        self.ie = self._get_exporter(sort_keys=True)
        self.test_export_item()
        self._check_output()
        with pytest.raises(TypeError):
            self._get_exporter(foo_unknown_keyword_bar=True)

    def test_nonstring_types_item(self):
        item = self._get_nonstring_types_item()
        self.ie.start_exporting()
        self.ie.export_item(item)
        self.ie.finish_exporting()
        del self.ie  # See the first “del self.ie” in this file for context.
        exported = json.loads(self.output.getvalue())
        item["time"] = item["time"].isoformat()
        assert exported == item


class TestJsonLinesItemExporterDataclass(TestJsonLinesItemExporter):
    item_class = MyDataClass
    custom_field_item_class = CustomFieldDataclass


class _NonSeekableStream:
    """Collects bytes like a socket, which has no seek() or truncate()."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> int:
        self.chunks.append(bytes(data))
        return len(data)

    @property
    def value(self) -> bytes:
        return b"".join(self.chunks)


class _ShortWritingStream(_NonSeekableStream):
    """Accepts only *limit* bytes per write() call."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit

    def write(self, data: bytes) -> int:
        self.chunks.append(bytes(data[: self.limit]))
        return min(len(data), self.limit)


class _FailingStream(_NonSeekableStream):
    """Raises *exc* from every write() call."""

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc

    def write(self, data: bytes) -> int:
        raise self.exc


def _file(stream: Any) -> IO[bytes]:
    """Present a duck-typed test stream as the binary file an exporter takes."""
    return cast("IO[bytes]", stream)


class TestJsonLinesItemExporterAtomic:
    """Tests for ``atomic_items=True`` and ``max_item_bytes``."""

    def _get_exporter(self, **kwargs: Any) -> tuple[JsonLinesItemExporter, BytesIO]:
        output = BytesIO()
        exporter = JsonLinesItemExporter(output, atomic_items=True, **kwargs)
        return exporter, output

    @staticmethod
    def _lines(output: BytesIO) -> list[Any]:
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_dict_item(self):
        exporter, output = self._get_exporter()
        exporter.start_exporting()
        exporter.export_item({"name": "John\xa3", "age": "22"})
        exporter.finish_exporting()
        assert self._lines(output) == [{"name": "John\xa3", "age": "22"}]

    def test_item(self):
        exporter, output = self._get_exporter()
        exporter.export_item(MyItem(name="John\xa3", age="22"))
        assert self._lines(output) == [{"name": "John\xa3", "age": "22"}]

    def test_output_matches_default_path(self):
        item = {"name": "John\xa3", "age": "22"}
        exporter, output = self._get_exporter()
        exporter.export_item(item)
        default_output = BytesIO()
        JsonLinesItemExporter(default_output).export_item(item)
        assert output.getvalue() == default_output.getvalue()

    def test_single_write_per_item(self):
        calls: list[bytes] = []

        class RecordingStream(_NonSeekableStream):
            def write(self, data: bytes) -> int:
                calls.append(bytes(data))
                return super().write(data)

        exporter = JsonLinesItemExporter(_file(RecordingStream()), atomic_items=True)
        exporter.export_item({"name": "John", "age": "22"})
        assert len(calls) == 1
        assert calls[0].endswith(b"\n")
        assert calls[0].count(b"\n") == 1

    def test_custom_serializer(self):
        exporter, output = self._get_exporter()
        exporter.export_item(CustomFieldItem(name="John", age="22"))
        assert self._lines(output) == [{"name": "John", "age": "24"}]

    def test_custom_encoder_option(self):
        exporter, output = self._get_exporter(sort_keys=True)
        exporter.export_item({"b": "2", "a": "1"})
        assert output.getvalue() == b'{"a": "1", "b": "2"}\n'

    def test_custom_encoder(self):
        class UpperEncoder(ScrapyJSONEncoder):
            def default(self, o: Any) -> Any:
                if isinstance(o, bytes):
                    return o.decode().upper()
                return super().default(o)

        item = {"name": b"john"}
        default_output = BytesIO()
        default_exporter = JsonLinesItemExporter(default_output)
        default_exporter.encoder = UpperEncoder()
        default_exporter.export_item(item)
        exporter, output = self._get_exporter()
        exporter.encoder = UpperEncoder()
        exporter.export_item(item)
        assert output.getvalue() == default_output.getvalue()
        assert output.getvalue() == b'{"name": "JOHN"}\n'

    def test_ensure_ascii(self):
        exporter, output = self._get_exporter()
        exporter.export_item({"name": "John\xa3"})
        assert output.getvalue() == b'{"name": "John\\u00a3"}\n'

    def test_utf8_encoding(self):
        exporter, output = self._get_exporter(encoding="utf-8")
        exporter.export_item({"name": "John\xa3"})
        assert output.getvalue() == '{"name": "John£"}\n'.encode()

    def test_latin1_encoding(self):
        exporter, output = self._get_exporter(encoding="latin-1")
        exporter.export_item({"name": "John\xa3"})
        assert output.getvalue() == b'{"name": "John\xa3"}\n'

    def test_non_ascii_encoding(self):
        exporter, output = self._get_exporter(encoding="utf-16")
        exporter.export_item({"name": "John"})
        assert json.loads(output.getvalue().decode("utf-16")) == {"name": "John"}

    def test_fields_to_export_order(self):
        exporter, output = self._get_exporter(fields_to_export={"name": "Name"})
        exporter.export_item(MyItem(name="John", age="22"))
        assert output.getvalue() == b'{"Name": "John"}\n'

    def test_item_not_modified(self):
        item = MyItem(name="John", age="22")
        exporter, _ = self._get_exporter(fields_to_export=["name"])
        exporter.export_item(item)
        exporter.export_item({"name": "Maria", "age": "33"})
        assert dict(item) == {"name": "John", "age": "22"}

    @pytest.mark.parametrize(
        "options",
        [
            pytest.param({}, id="all-fields"),
            pytest.param({"fields_to_export": ["age"]}, id="list"),
            pytest.param({"fields_to_export": {"name": "Name"}}, id="mapping"),
            pytest.param({"export_empty_fields": True}, id="empty-fields"),
            pytest.param(
                {
                    "fields_to_export": {"name": "Name", "age": "Age"},
                    "export_empty_fields": True,
                },
                id="mapping-and-empty-fields",
            ),
            pytest.param({"fields_to_export": ["name", "missing"]}, id="missing-field"),
            pytest.param(
                {"fields_to_export": ["name", "missing"], "export_empty_fields": True},
                id="missing-field-with-empty-fields",
            ),
            pytest.param({"encoding": "latin-1"}, id="latin-1"),
            pytest.param({"sort_keys": True}, id="sort-keys"),
        ],
    )
    def test_matches_default_path(self, options: dict[str, Any]):
        items = [MyItem(name="John\xa3", age="22"), {"age": "33", "name": "Maria"}]
        default_output = BytesIO()
        default_exporter = JsonLinesItemExporter(default_output, **options)
        for item in items:
            default_exporter.export_item(item)
        exporter, output = self._get_exporter(**options)
        for item in items:
            exporter.export_item(item)
        assert output.getvalue() == default_output.getvalue()

    def test_field_iteration_errors_propagate(self):
        def fields() -> Iterator[str]:
            yield "name"
            raise RuntimeError("field iteration failed")

        exporter, output = self._get_exporter(fields_to_export=fields())
        with pytest.raises(RuntimeError, match="field iteration failed"):
            exporter.export_item({"name": "John"})
        assert output.getvalue() == b""

    def test_get_serialized_fields_override(self):
        class Exporter(JsonLinesItemExporter):
            def get_serialized_fields(
                self,
                item: Any,
                default_value: Any = None,
                include_empty: bool | None = None,
            ) -> Iterable[tuple[str, Any]]:
                yield from super().get_serialized_fields(
                    item, default_value, include_empty
                )
                yield "extra", "1"

        output = BytesIO()
        Exporter(output, atomic_items=True).export_item({"name": "John"})
        assert self._lines(output) == [{"name": "John", "extra": "1"}]

    def test_serialization_failure_writes_nothing(self):
        exporter, output = self._get_exporter()
        with pytest.raises(TypeError, match="field 'age'"):
            exporter.export_item({"name": "Maria", "age": 1j})
        assert output.getvalue() == b""

    def test_serialization_failure_hides_value(self):
        secret = "s3cr3t"
        exporter, output = self._get_exporter()
        with pytest.raises(TypeError) as excinfo:
            exporter.export_item({"name": "Maria", "age": 1j, "note": secret})
        assert secret not in str(excinfo.value)
        assert secret not in "\n".join(excinfo.value.__notes__)
        assert output.getvalue() == b""

    def test_serializer_failure_writes_nothing(self):
        def serializer(value):
            raise ValueError("cannot serialize")

        class BrokenItem(Item):
            name = Field(serializer=serializer)

        exporter, output = self._get_exporter()
        with pytest.raises(ValueError, match="cannot serialize") as excinfo:
            exporter.export_item(BrokenItem(name="John"))
        assert "While serializing field 'name'" in "\n".join(excinfo.value.__notes__)
        assert output.getvalue() == b""

    def test_encoding_failure_names_all_fields(self):
        class DictFailingEncoder(ScrapyJSONEncoder):
            def encode(self, o: Any) -> str:
                if isinstance(o, dict) and len(o) > 1:
                    raise TypeError("cannot encode this item")
                return super().encode(o)

        exporter, output = self._get_exporter()
        exporter.encoder = DictFailingEncoder()
        with pytest.raises(TypeError, match="fields: name, age"):
            exporter.export_item({"name": "John", "age": "22"})
        assert output.getvalue() == b""

    def test_serializer_failure_with_several_args(self):
        def serializer(value):
            raise RuntimeError("cannot", "serialize")

        class BrokenItem(Item):
            name = Field(serializer=serializer)

        exporter, output = self._get_exporter()
        with pytest.raises(RuntimeError) as excinfo:
            exporter.export_item(BrokenItem(name="John"))
        assert "While serializing field 'name'" in "\n".join(excinfo.value.__notes__)
        assert "While serializing field 'name'" not in str(excinfo.value)
        assert output.getvalue() == b""

    def test_default_path_serializer_failure(self):
        def serializer(value):
            raise ValueError("cannot serialize")

        class BrokenItem(Item):
            name = Field(serializer=serializer)

        output = BytesIO()
        exporter = JsonLinesItemExporter(output)
        with pytest.raises(ValueError, match="cannot serialize") as excinfo:
            exporter.export_item(BrokenItem(name="John"))
        assert getattr(excinfo.value, "__notes__", []) == []
        assert output.getvalue() == b""

    def test_encoding_failure_writes_nothing(self):
        exporter, output = self._get_exporter(encoding="latin-1")
        with pytest.raises(UnicodeEncodeError) as excinfo:
            exporter.export_item({"name": "☺", "age": "22"})
        assert "field 'name'" in str(excinfo.value)
        assert "☺" not in str(excinfo.value)
        assert "☺" not in repr(excinfo.value)
        context = excinfo.value.__context__
        assert context is not None
        assert "☺" not in repr(context)
        assert output.getvalue() == b""

    @pytest.mark.parametrize(
        ("offset", "expected"),
        [
            pytest.param(0, "accepted", id="exact"),
            pytest.param(1, "accepted", id="one-over"),
            pytest.param(-1, "rejected", id="one-under"),
        ],
    )
    def test_max_item_bytes_boundary(self, offset: int, expected: str):
        item = {"name": "John\xa3"}
        default_output = BytesIO()
        JsonLinesItemExporter(default_output).export_item(item)
        line = default_output.getvalue()
        exporter, output = self._get_exporter(max_item_bytes=len(line) + offset)
        if expected == "accepted":
            exporter.export_item(item)
            assert output.getvalue() == line
        else:
            with pytest.raises(ValueError, match="max_item_bytes"):
                exporter.export_item(item)
            assert output.getvalue() == b""

    def test_max_item_bytes_counts_newline(self):
        item = {"name": "ok"}
        without_newline = len(json.dumps(item).encode())
        exporter, output = self._get_exporter(max_item_bytes=without_newline)
        with pytest.raises(ValueError, match=f"{without_newline + 1} bytes long"):
            exporter.export_item(item)
        assert output.getvalue() == b""

    def test_max_item_bytes_counts_encoded_length(self):
        item = {"name": "John\xa3"}
        default_output = BytesIO()
        JsonLinesItemExporter(default_output, encoding="utf-8").export_item(item)
        # "£" takes two bytes in UTF-8 and six as an ensure_ascii escape.
        assert len(default_output.getvalue()) == 19
        exporter, output = self._get_exporter(encoding="utf-8", max_item_bytes=18)
        with pytest.raises(ValueError, match="19 bytes long"):
            exporter.export_item(item)
        assert output.getvalue() == b""

    def test_max_item_bytes_zero_is_unlimited(self):
        exporter, output = self._get_exporter(max_item_bytes=0)
        exporter.export_item({"name": "x" * 10_000})
        assert len(self._lines(output)) == 1

    def test_max_item_bytes_hides_value(self):
        exporter, output = self._get_exporter(max_item_bytes=5)
        with pytest.raises(ValueError, match="max_item_bytes") as excinfo:
            exporter.export_item({"name": "s3cr3t"})
        assert "s3cr3t" not in str(excinfo.value)
        assert output.getvalue() == b""

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(-1, id="negative"),
            pytest.param(True, id="bool-true"),
            pytest.param(False, id="bool-false"),
            pytest.param(1.5, id="float"),
            pytest.param("10", id="str"),
            pytest.param(None, id="none"),
        ],
    )
    def test_max_item_bytes_invalid(self, value: Any):
        output = BytesIO()
        with pytest.raises(ValueError, match="max_item_bytes"):
            JsonLinesItemExporter(output, atomic_items=True, max_item_bytes=value)
        assert output.getvalue() == b""

    def test_max_item_bytes_ignored_without_atomic_items(self):
        output = BytesIO()
        exporter = JsonLinesItemExporter(output, max_item_bytes=1)
        exporter.export_item({"name": "John"})
        assert self._lines(output) == [{"name": "John"}]

    def test_failure_then_normal_item(self):
        exporter, output = self._get_exporter()
        exporter.export_item({"name": "Joseph", "age": "22"})
        with pytest.raises(TypeError):
            exporter.export_item({"name": "Maria", "age": 1j})
        exporter.export_item({"name": "Jesus", "age": "44"})
        assert self._lines(output) == [
            {"name": "Joseph", "age": "22"},
            {"name": "Jesus", "age": "44"},
        ]

    def test_limit_failure_then_normal_item(self):
        exporter, output = self._get_exporter(max_item_bytes=20)
        with pytest.raises(ValueError, match="max_item_bytes"):
            exporter.export_item({"name": "a" * 100})
        exporter.export_item({"name": "ok"})
        assert self._lines(output) == [{"name": "ok"}]

    def test_non_seekable_stream(self):
        stream = _NonSeekableStream()
        exporter = JsonLinesItemExporter(_file(stream), atomic_items=True)
        exporter.export_item({"name": "Joseph"})
        with pytest.raises(TypeError):
            exporter.export_item({"name": "Maria", "age": 1j})
        exporter.export_item({"name": "Jesus"})
        exporter.finish_exporting()
        assert [json.loads(line) for line in stream.value.splitlines()] == [
            {"name": "Joseph"},
            {"name": "Jesus"},
        ]

    def test_never_seeks_or_truncates(self):
        class StrictStream(_NonSeekableStream):
            def seek(self, *args: Any) -> int:
                raise AssertionError("seek() called")

            def truncate(self, *args: Any) -> int:
                raise AssertionError("truncate() called")

        stream = StrictStream()
        exporter = JsonLinesItemExporter(
            _file(stream), atomic_items=True, max_item_bytes=50
        )
        exporter.export_item({"name": "Joseph"})
        with pytest.raises(ValueError, match="max_item_bytes"):
            exporter.export_item({"name": "a" * 100})
        exporter.finish_exporting()
        assert [json.loads(line) for line in stream.value.splitlines()] == [
            {"name": "Joseph"}
        ]

    def test_short_write(self):
        line = b'{"name": "John"}\n'
        stream = _ShortWritingStream(limit=len(line) // 2)
        exporter = JsonLinesItemExporter(_file(stream), atomic_items=True)
        with pytest.raises(OSError, match="Short write"):
            exporter.export_item({"name": "John"})
        assert stream.value == line[: len(line) // 2]

    def test_write_failure(self):
        stream = _FailingStream(OSError("disk full"))
        exporter = JsonLinesItemExporter(_file(stream), atomic_items=True)
        with pytest.raises(OSError, match="disk full"):
            exporter.export_item({"name": "John"})
        assert stream.value == b""

    @pytest.mark.parametrize(
        "stream",
        [
            pytest.param(_ShortWritingStream(limit=1), id="short-write"),
            pytest.param(_FailingStream(OSError("disk full")), id="write-error"),
        ],
    )
    def test_export_rejected_after_write_failure(self, stream):
        exporter = JsonLinesItemExporter(_file(stream), atomic_items=True)
        with pytest.raises(OSError, match=r"Short write|disk full"):
            exporter.export_item({"name": "John"})
        with pytest.raises(RuntimeError, match="failed or was incomplete"):
            exporter.export_item({"name": "Joseph"})

    def test_finish_exporting_after_write_failure(self):
        stream = _FailingStream(OSError("disk full"))
        exporter = JsonLinesItemExporter(_file(stream), atomic_items=True)
        with pytest.raises(OSError, match="disk full"):
            exporter.export_item({"name": "John"})
        exporter.finish_exporting()
        assert not stream.value

    def test_finish_exporting_leaves_stream_open(self):
        output = BytesIO()
        exporter = JsonLinesItemExporter(output, atomic_items=True)
        exporter.export_item({"name": "John"})
        exporter.finish_exporting()
        assert not output.closed
        output.write(b"")

    def test_no_batch_buffering(self):
        exporter, output = self._get_exporter()
        exporter.export_item({"n": "1"})
        assert output.getvalue() == b'{"n": "1"}\n'


class TestJsonItemExporter(TestJsonLinesItemExporter):
    _expected_nested = [TestJsonLinesItemExporter._expected_nested]

    def _get_exporter(self, **kwargs: Any) -> BaseItemExporter:
        return JsonItemExporter(self.output, **kwargs)

    def _check_output(self) -> None:
        exported = json.loads(self.output.getvalue().strip())
        assert exported == [ItemAdapter(self.i).asdict()]

    def assertTwoItemsExported(self, item: Any) -> None:
        self.ie.start_exporting()
        self.ie.export_item(item)
        self.ie.export_item(item)
        self.ie.finish_exporting()
        del self.ie  # See the first “del self.ie” in this file for context.
        exported = json.loads(self.output.getvalue())
        assert exported == [ItemAdapter(item).asdict(), ItemAdapter(item).asdict()]

    def test_two_items(self):
        self.assertTwoItemsExported(self.i)

    def test_two_dict_items(self):
        self.assertTwoItemsExported(ItemAdapter(self.i).asdict())

    def test_two_items_with_failure_between(self):
        i1 = MyItem(name="Joseph\xa3", age="22")
        i2 = MyItem(
            name="Maria", age=1j
        )  # Invalid datetimes didn't consistently fail between Python versions
        i3 = MyItem(name="Jesus", age="44")
        self.ie.start_exporting()
        self.ie.export_item(i1)
        with pytest.raises(TypeError):
            self.ie.export_item(i2)
        self.ie.export_item(i3)
        self.ie.finish_exporting()
        exported = json.loads(self.output.getvalue())
        assert exported == [dict(i1), dict(i3)]

    def test_nested_item(self):
        i1 = self.item_class(name="Joseph\xa3", age="22")
        i2 = self.item_class(name="Maria", age=i1)
        i3 = self.item_class(name="Jesus", age=i2)
        self.ie.start_exporting()
        self.ie.export_item(i3)
        self.ie.finish_exporting()
        del self.ie  # See the first “del self.ie” in this file for context.
        exported = json.loads(self.output.getvalue())
        expected = {
            "name": "Jesus",
            "age": {"name": "Maria", "age": ItemAdapter(i1).asdict()},
        }
        assert exported == [expected]

    def test_nested_dict_item(self):
        i1 = {"name": "Joseph\xa3", "age": "22"}
        i2 = self.item_class(name="Maria", age=i1)
        i3 = {"name": "Jesus", "age": i2}
        self.ie.start_exporting()
        self.ie.export_item(i3)
        self.ie.finish_exporting()
        del self.ie  # See the first “del self.ie” in this file for context.
        exported = json.loads(self.output.getvalue())
        expected = {"name": "Jesus", "age": {"name": "Maria", "age": i1}}
        assert exported == [expected]

    def test_nonstring_types_item(self):
        item = self._get_nonstring_types_item()
        self.ie.start_exporting()
        self.ie.export_item(item)
        self.ie.finish_exporting()
        del self.ie  # See the first “del self.ie” in this file for context.
        exported = json.loads(self.output.getvalue())
        item["time"] = item["time"].isoformat()
        assert exported == [item]


class TestJsonItemExporterToBytes(TestBaseItemExporter):
    def _get_exporter(self, **kwargs: Any) -> BaseItemExporter:
        kwargs["encoding"] = "latin"
        return JsonItemExporter(self.output, **kwargs)

    def test_two_items_with_failure_between(self):
        i1 = MyItem(name="Joseph", age="22")
        i2 = MyItem(name="\u263a", age="11")
        i3 = MyItem(name="Jesus", age="44")
        self.ie.start_exporting()
        self.ie.export_item(i1)
        with pytest.raises(UnicodeEncodeError):
            self.ie.export_item(i2)
        self.ie.export_item(i3)
        self.ie.finish_exporting()
        exported = json.loads(self.output.getvalue().decode(encoding="latin-1"))
        assert exported == [dict(i1), dict(i3)]


class TestJsonItemExporterDataclass(TestJsonItemExporter):
    item_class = MyDataClass
    custom_field_item_class = CustomFieldDataclass


class TestCustomExporterItem:
    item_class: type = MyItem

    def setup_method(self):
        if self.item_class is None:
            pytest.skip("item class is None")

    def test_exporter_custom_serializer(self):
        class CustomItemExporter(BaseItemExporter):
            def serialize_field(
                self, field: Mapping[str, Any] | Field, name: str, value: Any
            ) -> Any:
                if name == "age":
                    return str(int(value) + 1)
                return super().serialize_field(field, name, value)

            def export_item(self, item: Any) -> None:
                pass

        i = self.item_class(name="John", age="22")
        a = ItemAdapter(i)
        ie = CustomItemExporter()

        assert ie.serialize_field(a.get_field_meta("name"), "name", a["name"]) == "John"
        assert ie.serialize_field(a.get_field_meta("age"), "age", a["age"]) == "23"

        i2 = {"name": "John", "age": "22"}
        assert ie.serialize_field({}, "name", i2["name"]) == "John"
        assert ie.serialize_field({}, "age", i2["age"]) == "23"


class TestCustomExporterDataclass(TestCustomExporterItem):
    item_class = MyDataClass
