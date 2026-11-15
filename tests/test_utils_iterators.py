from __future__ import annotations

import json
from typing import Any

import pytest

from scrapy.http import Response, TextResponse, XmlResponse
from scrapy.utils.iterators import (
    JsonlBudgetError,
    JsonlError,
    _body_or_str,
    csviter,
    jsonl_iter,
    xmliter_lxml,
)
from tests import get_testdata


class TestXmliter:
    def test_xmliter(self):
        body = b"""
            <?xml version="1.0" encoding="UTF-8"?>
            <products xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                      xsi:noNamespaceSchemaLocation="someschmea.xsd">
              <product id="001">
                <type>Type 1</type>
                <name>Name 1</name>
              </product>
              <product id="002">
                <type>Type 2</type>
                <name>Name 2</name>
              </product>
            </products>
        """

        response = XmlResponse(url="http://example.com", body=body)
        attrs = [
            (
                x.attrib["id"],
                x.xpath("name/text()").getall(),
                x.xpath("./type/text()").getall(),
            )
            for x in xmliter_lxml(response, "product")
        ]

        assert attrs == [
            ("001", ["Name 1"], ["Type 1"]),
            ("002", ["Name 2"], ["Type 2"]),
        ]

    def test_xmliter_unusual_node(self):
        body = b"""<?xml version="1.0" encoding="UTF-8"?>
            <root>
                <matchme...></matchme...>
                <matchmenot></matchmenot>
            </root>
        """
        response = XmlResponse(url="http://example.com", body=body)
        nodenames = [
            e.xpath("name()").getall() for e in xmliter_lxml(response, "matchme...")
        ]
        assert nodenames == [["matchme..."]]

    def test_xmliter_unicode(self):
        # example taken from https://github.com/scrapy/scrapy/issues/1665
        body = """<?xml version="1.0" encoding="UTF-8"?>
            <þingflokkar>
               <þingflokkur id="26">
                  <heiti />
                  <skammstafanir>
                     <stuttskammstöfun>-</stuttskammstöfun>
                     <löngskammstöfun />
                  </skammstafanir>
                  <tímabil>
                     <fyrstaþing>80</fyrstaþing>
                  </tímabil>
               </þingflokkur>
               <þingflokkur id="21">
                  <heiti>Alþýðubandalag</heiti>
                  <skammstafanir>
                     <stuttskammstöfun>Ab</stuttskammstöfun>
                     <löngskammstöfun>Alþb.</löngskammstöfun>
                  </skammstafanir>
                  <tímabil>
                     <fyrstaþing>76</fyrstaþing>
                     <síðastaþing>123</síðastaþing>
                  </tímabil>
               </þingflokkur>
               <þingflokkur id="27">
                  <heiti>Alþýðuflokkur</heiti>
                  <skammstafanir>
                     <stuttskammstöfun>A</stuttskammstöfun>
                     <löngskammstöfun>Alþfl.</löngskammstöfun>
                  </skammstafanir>
                  <tímabil>
                     <fyrstaþing>27</fyrstaþing>
                     <síðastaþing>120</síðastaþing>
                  </tímabil>
               </þingflokkur>
            </þingflokkar>"""

        for r in (
            # with bytes
            XmlResponse(url="http://example.com", body=body.encode()),
            # Unicode body needs encoding information
            XmlResponse(url="http://example.com", body=body, encoding="utf-8"),
        ):
            attrs = [
                (
                    x.attrib["id"],
                    x.xpath("./skammstafanir/stuttskammstöfun/text()").getall(),
                    x.xpath("./tímabil/fyrstaþing/text()").getall(),
                )
                for x in xmliter_lxml(r, "þingflokkur")
            ]

            assert attrs == [
                ("26", ["-"], ["80"]),
                ("21", ["Ab"], ["76"]),
                ("27", ["A"], ["27"]),
            ]

    def test_xmliter_text(self):
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<products><product>one</product><product>two</product></products>"
        )

        assert [x.xpath("text()").getall() for x in xmliter_lxml(body, "product")] == [
            ["one"],
            ["two"],
        ]

    def test_xmliter_namespaces(self):
        body = b"""
            <?xml version="1.0" encoding="UTF-8"?>
            <rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">
                <channel>
                <title>My Dummy Company</title>
                <link>http://www.mydummycompany.com</link>
                <description>This is a dummy company. We do nothing.</description>
                <item>
                    <title>Item 1</title>
                    <description>This is item 1</description>
                    <link>http://www.mydummycompany.com/items/1</link>
                    <g:image_link>http://www.mydummycompany.com/images/item1.jpg</g:image_link>
                    <g:id>ITEM_1</g:id>
                    <g:price>400</g:price>
                </item>
                </channel>
            </rss>
        """
        response = XmlResponse(url="http://mydummycompany.com", body=body)
        my_iter = xmliter_lxml(response, "item")
        node = next(my_iter)
        node.register_namespace("g", "http://base.google.com/ns/1.0")
        assert node.xpath("title/text()").getall() == ["Item 1"]
        assert node.xpath("description/text()").getall() == ["This is item 1"]
        assert node.xpath("link/text()").getall() == [
            "http://www.mydummycompany.com/items/1"
        ]
        assert node.xpath("g:image_link/text()").getall() == [
            "http://www.mydummycompany.com/images/item1.jpg"
        ]
        assert node.xpath("g:id/text()").getall() == ["ITEM_1"]
        assert node.xpath("g:price/text()").getall() == ["400"]
        assert node.xpath("image_link/text()").getall() == []
        assert node.xpath("id/text()").getall() == []
        assert node.xpath("price/text()").getall() == []

    def test_xmliter_namespaced_nodename(self):
        body = b"""
            <?xml version="1.0" encoding="UTF-8"?>
            <rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">
                <channel>
                <title>My Dummy Company</title>
                <link>http://www.mydummycompany.com</link>
                <description>This is a dummy company. We do nothing.</description>
                <item>
                    <title>Item 1</title>
                    <description>This is item 1</description>
                    <link>http://www.mydummycompany.com/items/1</link>
                    <g:image_link>http://www.mydummycompany.com/images/item1.jpg</g:image_link>
                    <g:id>ITEM_1</g:id>
                    <g:price>400</g:price>
                </item>
                </channel>
            </rss>
        """
        response = XmlResponse(url="http://mydummycompany.com", body=body)
        my_iter = xmliter_lxml(response, "g:image_link")
        node = next(my_iter)
        node.register_namespace("g", "http://base.google.com/ns/1.0")
        assert node.xpath("text()").extract() == [
            "http://www.mydummycompany.com/images/item1.jpg"
        ]

    def test_xmliter_namespaced_nodename_missing(self):
        body = b"""
            <?xml version="1.0" encoding="UTF-8"?>
            <rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">
                <channel>
                <title>My Dummy Company</title>
                <link>http://www.mydummycompany.com</link>
                <description>This is a dummy company. We do nothing.</description>
                <item>
                    <title>Item 1</title>
                    <description>This is item 1</description>
                    <link>http://www.mydummycompany.com/items/1</link>
                    <g:image_link>http://www.mydummycompany.com/images/item1.jpg</g:image_link>
                    <g:id>ITEM_1</g:id>
                    <g:price>400</g:price>
                </item>
                </channel>
            </rss>
        """
        response = XmlResponse(url="http://mydummycompany.com", body=body)
        my_iter = xmliter_lxml(response, "g:link_image")
        with pytest.raises(StopIteration):
            next(my_iter)

    def test_xmliter_namespaced_nodename_other_prefixes(self):
        body = b"""<?xml version="1.0" encoding="UTF-8"?>
            <rss version="2.0"
                 xmlns:a="http://example.com/ns/a"
                 xmlns:g="http://base.google.com/ns/1.0">
                <item>
                    <a:id>A_1</a:id>
                    <g:id>ITEM_1</g:id>
                </item>
            </rss>
        """
        response = XmlResponse(url="http://example.com", body=body)
        node = next(xmliter_lxml(response, "g:id"))
        assert node.xpath("text()").getall() == ["ITEM_1"]

    def test_xmliter_non_text_response(self):
        body = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b"<products><product>one</product></products>"
        )
        response = Response(url="http://example.com", body=body)
        assert [
            node.xpath("text()").get() for node in xmliter_lxml(response, "product")
        ] == ["one"]

    def test_xmliter_exception(self):
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<products><product>one</product><product>two</product></products>"
        )

        my_iter = xmliter_lxml(body, "product")
        next(my_iter)
        next(my_iter)
        with pytest.raises(StopIteration):
            next(my_iter)

    def test_xmliter_objtype_exception(self):
        i = xmliter_lxml(42, "product")  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            next(i)

    def test_xmliter_encoding(self):
        body = (
            b'<?xml version="1.0" encoding="ISO-8859-9"?>\n'
            b"<xml>\n"
            b"    <item>Some Turkish Characters \xd6\xc7\xde\xdd\xd0\xdc \xfc\xf0\xfd\xfe\xe7\xf6</item>\n"
            b"</xml>\n\n"
        )
        response = XmlResponse("http://www.example.com", body=body)
        assert (
            next(xmliter_lxml(response, "item")).get()
            == "<item>Some Turkish Characters \xd6\xc7\u015e\u0130\u011e\xdc \xfc\u011f\u0131\u015f\xe7\xf6</item>"
        )

    def test_xmliter_iterate_namespace(self):
        body = b"""
            <?xml version="1.0" encoding="UTF-8"?>
            <rss version="2.0" xmlns="http://base.google.com/ns/1.0">
                <channel>
                <title>My Dummy Company</title>
                <link>http://www.mydummycompany.com</link>
                <description>This is a dummy company. We do nothing.</description>
                <item>
                    <title>Item 1</title>
                    <description>This is item 1</description>
                    <link>http://www.mydummycompany.com/items/1</link>
                    <image_link>http://www.mydummycompany.com/images/item1.jpg</image_link>
                    <image_link>http://www.mydummycompany.com/images/item2.jpg</image_link>
                </item>
                </channel>
            </rss>
        """
        response = XmlResponse(url="http://mydummycompany.com", body=body)

        no_namespace_iter = xmliter_lxml(response, "image_link")
        assert len(list(no_namespace_iter)) == 0

        namespace_iter = xmliter_lxml(
            response, "image_link", "http://base.google.com/ns/1.0"
        )
        node = next(namespace_iter)
        assert node.xpath("text()").getall() == [
            "http://www.mydummycompany.com/images/item1.jpg"
        ]
        node = next(namespace_iter)
        assert node.xpath("text()").getall() == [
            "http://www.mydummycompany.com/images/item2.jpg"
        ]

    def test_xmliter_namespaces_prefix(self):
        body = b"""
        <?xml version="1.0" encoding="UTF-8"?>
        <root>
            <h:table xmlns:h="http://www.w3.org/TR/html4/">
              <h:tr>
                <h:td>Apples</h:td>
                <h:td>Bananas</h:td>
              </h:tr>
            </h:table>

            <f:table xmlns:f="http://www.w3schools.com/furniture">
              <f:name>African Coffee Table</f:name>
              <f:width>80</f:width>
              <f:length>120</f:length>
            </f:table>

        </root>
        """
        response = XmlResponse(url="http://mydummycompany.com", body=body)
        my_iter = xmliter_lxml(response, "table", "http://www.w3.org/TR/html4/", "h")

        node = next(my_iter)
        assert len(node.xpath("h:tr/h:td").getall()) == 2
        assert node.xpath("h:tr/h:td[1]/text()").getall() == ["Apples"]
        assert node.xpath("h:tr/h:td[2]/text()").getall() == ["Bananas"]

        my_iter = xmliter_lxml(
            response, "table", "http://www.w3schools.com/furniture", "f"
        )

        node = next(my_iter)
        assert node.xpath("f:name/text()").getall() == ["African Coffee Table"]


class TestUtilsCsv:
    def test_csviter_defaults(self):
        body = get_testdata("feeds", "feed-sample3.csv")
        response = TextResponse(url="http://example.com/", body=body)
        csv = csviter(response)

        result = list(csv)
        assert result == [
            {"id": "1", "name": "alpha", "value": "foobar"},
            {"id": "2", "name": "unicode", "value": "\xfan\xedc\xf3d\xe9\u203d"},
            {"id": "3", "name": "multi", "value": "foo\nbar"},
            {"id": "4", "name": "empty", "value": ""},
        ]

        # explicit type check cuz' we no like stinkin' autocasting! yarrr
        for result_row in result:
            assert all(isinstance(k, str) for k in result_row)
            assert all(isinstance(v, str) for v in result_row.values())

    def test_csviter_delimiter(self):
        body = get_testdata("feeds", "feed-sample3.csv").replace(b",", b"\t")
        response = TextResponse(url="http://example.com/", body=body)
        csv = csviter(response, delimiter="\t")

        assert list(csv) == [
            {"id": "1", "name": "alpha", "value": "foobar"},
            {"id": "2", "name": "unicode", "value": "\xfan\xedc\xf3d\xe9\u203d"},
            {"id": "3", "name": "multi", "value": "foo\nbar"},
            {"id": "4", "name": "empty", "value": ""},
        ]

    def test_csviter_quotechar(self):
        body1 = get_testdata("feeds", "feed-sample6.csv")
        body2 = get_testdata("feeds", "feed-sample6.csv").replace(b",", b"|")

        response1 = TextResponse(url="http://example.com/", body=body1)
        csv1 = csviter(response1, quotechar="'")

        assert list(csv1) == [
            {"id": "1", "name": "alpha", "value": "foobar"},
            {"id": "2", "name": "unicode", "value": "\xfan\xedc\xf3d\xe9\u203d"},
            {"id": "3", "name": "multi", "value": "foo\nbar"},
            {"id": "4", "name": "empty", "value": ""},
        ]

        response2 = TextResponse(url="http://example.com/", body=body2)
        csv2 = csviter(response2, delimiter="|", quotechar="'")

        assert list(csv2) == [
            {"id": "1", "name": "alpha", "value": "foobar"},
            {"id": "2", "name": "unicode", "value": "\xfan\xedc\xf3d\xe9\u203d"},
            {"id": "3", "name": "multi", "value": "foo\nbar"},
            {"id": "4", "name": "empty", "value": ""},
        ]

    def test_csviter_wrong_quotechar(self):
        body = get_testdata("feeds", "feed-sample6.csv")
        response = TextResponse(url="http://example.com/", body=body)
        csv = csviter(response)

        assert list(csv) == [
            {"'id'": "1", "'name'": "'alpha'", "'value'": "'foobar'"},
            {
                "'id'": "2",
                "'name'": "'unicode'",
                "'value'": "'\xfan\xedc\xf3d\xe9\u203d'",
            },
            {"'id'": "'3'", "'name'": "'multi'", "'value'": "'foo"},
            {"'id'": "4", "'name'": "'empty'", "'value'": ""},
        ]

    def test_csviter_delimiter_binary_response_assume_utf8_encoding(self):
        body = get_testdata("feeds", "feed-sample3.csv").replace(b",", b"\t")
        response = Response(url="http://example.com/", body=body)
        csv = csviter(response, delimiter="\t")

        assert list(csv) == [
            {"id": "1", "name": "alpha", "value": "foobar"},
            {"id": "2", "name": "unicode", "value": "\xfan\xedc\xf3d\xe9\u203d"},
            {"id": "3", "name": "multi", "value": "foo\nbar"},
            {"id": "4", "name": "empty", "value": ""},
        ]

    def test_csviter_headers(self):
        sample = get_testdata("feeds", "feed-sample3.csv").splitlines()
        headers, body = sample[0].split(b","), b"\n".join(sample[1:])

        response = TextResponse(url="http://example.com/", body=body)
        csv = csviter(response, headers=[h.decode() for h in headers])

        assert list(csv) == [
            {"id": "1", "name": "alpha", "value": "foobar"},
            {"id": "2", "name": "unicode", "value": "\xfan\xedc\xf3d\xe9\u203d"},
            {"id": "3", "name": "multi", "value": "foo\nbar"},
            {"id": "4", "name": "empty", "value": ""},
        ]

    def test_csviter_falserow(self):
        body = get_testdata("feeds", "feed-sample3.csv")
        body = b"\n".join((body, b"a,b", b"a,b,c,d"))

        response = TextResponse(url="http://example.com/", body=body)
        csv = csviter(response)

        assert list(csv) == [
            {"id": "1", "name": "alpha", "value": "foobar"},
            {"id": "2", "name": "unicode", "value": "\xfan\xedc\xf3d\xe9\u203d"},
            {"id": "3", "name": "multi", "value": "foo\nbar"},
            {"id": "4", "name": "empty", "value": ""},
        ]

    def test_csviter_empty(self):
        assert not list(csviter(""))

    def test_csviter_exception(self):
        body = get_testdata("feeds", "feed-sample3.csv")

        response = TextResponse(url="http://example.com/", body=body)
        my_iter = csviter(response)
        next(my_iter)
        next(my_iter)
        next(my_iter)
        next(my_iter)
        with pytest.raises(StopIteration):
            next(my_iter)

    def test_csviter_encoding(self):
        body1 = get_testdata("feeds", "feed-sample4.csv")
        body2 = get_testdata("feeds", "feed-sample5.csv")

        response = TextResponse(
            url="http://example.com/", body=body1, encoding="latin1"
        )
        csv = csviter(response)
        assert list(csv) == [
            {"id": "1", "name": "latin1", "value": "test"},
            {"id": "2", "name": "something", "value": "\xf1\xe1\xe9\xf3"},
        ]

        response = TextResponse(url="http://example.com/", body=body2, encoding="cp866")
        csv = csviter(response)
        assert list(csv) == [
            {"id": "1", "name": "cp866", "value": "test"},
            {
                "id": "2",
                "name": "something",
                "value": "\u255a\u2569\u2569\u2569\u2550\u2550\u2557",
            },
        ]


class _OneShotStream:
    """A forward-only binary stream with short reads and no seek."""

    def __init__(self, data: bytes, max_read: int | None = None) -> None:
        self._data = data
        self._max_read = max_read
        self.read_sizes: list[int] = []
        self.close_calls = 0

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        if size < 0:
            raise AssertionError("jsonl_iter() must bound every read")
        limit = size if self._max_read is None else min(size, self._max_read)
        chunk = self._data[:limit]
        self._data = self._data[limit:]
        return chunk

    def close(self) -> None:
        self.close_calls += 1


class TestJsonlIter:
    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (b'{"a": 1}\n{"b": 2}\n', [{"a": 1}, {"b": 2}]),
            (b'{"a": 1}\n{"b": 2}', [{"a": 1}, {"b": 2}]),
            (b'{"a": 1}\r\n{"b": 2}\r\n', [{"a": 1}, {"b": 2}]),
            (b'\xef\xbb\xbf{"a": 1}\n', [{"a": 1}]),
            (b'{"a": 1}\n\n\n{"b": 2}\n', [{"a": 1}, {"b": 2}]),
            (b"", []),
            (b"\n\r\n\n", []),
            (b'{"a": {"b": [1, 2, {"c": "}"}]}}\n', [{"a": {"b": [1, 2, {"c": "}"}]}}]),
            (b'{"a": "\xc3\xa9\xe2\x82\xac"}\n', [{"a": "é€"}]),
        ],
    )
    def test_rows(self, data: bytes, expected: list[dict[str, Any]]) -> None:
        assert list(jsonl_iter(data)) == expected

    def test_bytes_and_text_response_are_equivalent(self) -> None:
        body = b'{"id": 1}\n{"id": 2}\n'
        response = TextResponse(url="http://example.com/", body=body)
        assert (
            list(jsonl_iter(response))
            == list(jsonl_iter(body))
            == [
                {"id": 1},
                {"id": 2},
            ]
        )

    def test_non_text_response_is_serialized_bytes(self) -> None:
        response = Response(url="http://example.com/", body=b'{"id": 1}\n')
        assert list(jsonl_iter(response)) == [{"id": 1}]

    @pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 7])
    def test_arbitrary_chunk_boundaries(self, chunk_size: int) -> None:
        data = (
            '{"id": 1}\n{"text": "déjà vu 名前"}\n{"nested": {"a": [1, 2]}}\n'.encode()
        )
        expected = list(jsonl_iter(data))
        stream = _OneShotStream(data, max_read=chunk_size)
        assert list(jsonl_iter(stream, chunk_size=chunk_size)) == expected
        assert stream.read_sizes
        assert all(1 <= size <= chunk_size for size in stream.read_sizes)
        assert stream.close_calls == 0

    def test_multibyte_character_split_across_chunks(self) -> None:
        # "€" is three bytes and "€" alone is never returned by a chunk_size=1
        # read that ends at a character boundary.
        data = '{"value": "€"}\n'.encode()
        stream = _OneShotStream(data, max_read=1)
        assert list(jsonl_iter(stream, chunk_size=1)) == [{"value": "€"}]

    def test_short_reads_are_not_eof(self) -> None:
        data = b'{"a": 1}\n{"b": 2}\n'
        for max_read in (1, 2, 3, 4):
            stream = _OneShotStream(data, max_read=max_read)
            assert list(jsonl_iter(stream, chunk_size=8)) == [{"a": 1}, {"b": 2}]

    def test_bom_only_on_first_line(self) -> None:
        data = b'\xef\xbb\xbf{"a": 1}\n\xef\xbb\xbf{"b": 2}\n'
        with pytest.raises(JsonlError) as exc_info:
            list(jsonl_iter(data))
        assert exc_info.value.line == 2
        assert exc_info.value.exception is json.JSONDecodeError

    def test_bom_splits_across_chunks(self) -> None:
        data = b'\xef\xbb\xbf{"a": 1}\n'
        stream = _OneShotStream(data, max_read=1)
        assert list(jsonl_iter(stream, chunk_size=1)) == [{"a": 1}]

    @pytest.mark.parametrize(
        ("data", "line", "exception"),
        [
            (b'{"a": 1}\nBAD\n', 2, json.JSONDecodeError),
            (b'{"a": 1}\n[1, 2]\n', 2, JsonlError),
            (b'{"a": 1}\n"text"\n', 2, JsonlError),
            (b'{"a": 1}\nnull\n', 2, JsonlError),
            (b'{"a": 1}\n{"detail": "\xff"}\n', 2, UnicodeDecodeError),
            (b"\xff\n", 1, UnicodeDecodeError),
        ],
    )
    def test_invalid_row_fails_with_line_number(
        self, data: bytes, line: int, exception: type[BaseException]
    ) -> None:
        iterator = jsonl_iter(data)
        with pytest.raises(JsonlError) as exc_info:
            list(iterator)
        assert exc_info.value.line == line
        assert exc_info.value.exception is exception
        assert isinstance(exc_info.value.__cause__, exception)

    def test_invalid_utf8_split_across_chunks(self) -> None:
        # The leading byte of a two-byte sequence arrives at the end of one
        # chunk and the invalid continuation byte starts the next.
        data = b'{"a": 1}\n\xc3\x28\n'
        stream = _OneShotStream(data, max_read=1)
        with pytest.raises(JsonlError) as exc_info:
            list(jsonl_iter(stream, chunk_size=1))
        assert exc_info.value.line == 2
        assert exc_info.value.exception is UnicodeDecodeError

    def test_truncated_multibyte_line_fails(self) -> None:
        data = b'{"a": "\xe2\x82"\n'
        with pytest.raises(JsonlError) as exc_info:
            list(jsonl_iter(data))
        assert exc_info.value.line == 1
        assert exc_info.value.exception is UnicodeDecodeError

    def test_rows_before_a_bad_row_are_returned(self) -> None:
        iterator = jsonl_iter(b'{"a": 1}\n{"b": 2}\nBAD\n{"d": 4}\n')
        assert next(iterator) == {"a": 1}
        assert next(iterator) == {"b": 2}
        with pytest.raises(JsonlError):
            next(iterator)

    def test_skip_invalid_records_only_line_and_exception_class(self) -> None:
        data = b'{"a": 1}\nBAD\n{"detail": "\xff"}\n[1, 2]\n{"d": 4}\n'
        iterator = jsonl_iter(data, skip_invalid=True)
        assert list(iterator) == [{"a": 1}, {"d": 4}]
        assert [(r.line, r.exception) for r in iterator.rejections] == [
            (2, json.JSONDecodeError),
            (3, UnicodeDecodeError),
            (4, JsonlError),
        ]
        for rejection in iterator.rejections:
            assert not hasattr(rejection, "data")

    def test_blank_lines_do_not_shift_line_numbers(self) -> None:
        data = b'{"a": 1}\n\n\nBAD\n'
        with pytest.raises(JsonlError) as exc_info:
            list(jsonl_iter(data))
        assert exc_info.value.line == 4

    def test_skip_invalid_does_not_replay_input(self) -> None:
        data = b'{"a": 1}\nBAD\n{"b": 2}\n'
        stream = _OneShotStream(data, max_read=2)
        iterator = jsonl_iter(stream, skip_invalid=True, chunk_size=4)
        assert list(iterator) == [{"a": 1}, {"b": 2}]
        assert iterator.rejections[0].line == 2
        # Every byte is read once, in short reads, with no seek or replay.
        assert sum(stream.read_sizes) >= len(data)
        assert len(stream.read_sizes) <= (len(data) // 2) + 2

    def test_does_not_close_caller_stream_or_read_at_construction(self) -> None:
        stream = _OneShotStream(b'{"a": 1}\n')
        iterator = jsonl_iter(stream)
        assert stream.read_sizes == []
        assert list(iterator) == [{"a": 1}]
        assert stream.close_calls == 0

    def test_close_releases_buffers_and_stops_iteration(self) -> None:
        stream = _OneShotStream(b'{"a": 1}\n{"b": 2}\n')
        iterator = jsonl_iter(stream)
        assert next(iterator) == {"a": 1}
        iterator.close()
        assert stream.close_calls == 0
        assert list(iterator) == []
        # A closed iterator never reads again.
        assert len(stream.read_sizes) == 1

    def test_context_manager_closes(self) -> None:
        stream = _OneShotStream(b'{"a": 1}\n{"b": 2}\n')
        with jsonl_iter(stream) as iterator:
            assert next(iterator) == {"a": 1}
        assert list(iterator) == []
        assert stream.close_calls == 0

    def test_rows_are_not_repeated(self) -> None:
        iterator = jsonl_iter(b'{"a": 1}\n{"b": 2}\n')
        assert [next(iterator), next(iterator)] == [{"a": 1}, {"b": 2}]
        with pytest.raises(StopIteration):
            next(iterator)

    def test_two_iterators_are_independent(self) -> None:
        first = jsonl_iter(
            _OneShotStream(b'{"a": 1}\n{"a": 2}\n', max_read=2), chunk_size=3
        )
        second = jsonl_iter(
            _OneShotStream(b'{"b": 1}\n{"b": 2}\n', max_read=2), chunk_size=3
        )
        assert next(first) == {"a": 1}
        assert next(second) == {"b": 1}
        assert list(first) == [{"a": 2}]
        assert list(second) == [{"b": 2}]

    def test_line_budget_boundary(self) -> None:
        data = b'{"a": 1}\n'
        assert list(jsonl_iter(data, max_line_bytes=8)) == [{"a": 1}]
        with pytest.raises(JsonlBudgetError) as exc_info:
            list(jsonl_iter(data, max_line_bytes=7))
        assert exc_info.value.line == 1

    def test_line_budget_ignores_terminators(self) -> None:
        data = b'{"a": 1}\r\n'
        assert list(jsonl_iter(data, max_line_bytes=8)) == [{"a": 1}]
        data = b'{"a": 1}'
        assert list(jsonl_iter(data, max_line_bytes=8)) == [{"a": 1}]

    def test_line_budget_checked_before_delivery(self) -> None:
        iterator = jsonl_iter(b'{"a": 1}\n{"bb": 2}\n', max_line_bytes=8)
        assert next(iterator) == {"a": 1}
        with pytest.raises(JsonlBudgetError):
            next(iterator)

    def test_overlong_line_is_skipped_with_bounded_reads(self) -> None:
        stream = _OneShotStream(
            b'{"a": 1}\n' + b"x" * 10_000 + b'\n{"b": 2}\n', max_read=1
        )
        iterator = jsonl_iter(
            stream, skip_invalid=True, max_line_bytes=16, chunk_size=32
        )
        assert list(iterator) == [{"a": 1}, {"b": 2}]
        assert [(r.line, r.exception) for r in iterator.rejections] == [
            (2, JsonlBudgetError)
        ]
        assert all(size <= 32 for size in stream.read_sizes)

    def test_overlong_line_fails_before_reading_the_rest(self) -> None:
        stream = _OneShotStream(b'{"a": 1}\n' + b"x" * 10_000 + b'\n{"b": 2}\n')
        with pytest.raises(JsonlBudgetError) as exc_info:
            list(jsonl_iter(stream, max_line_bytes=16, chunk_size=32))
        assert exc_info.value.line == 2
        assert sum(stream.read_sizes) < 1000

    def test_encoded_budget(self) -> None:
        data = b'{"a": 1}\n{"b": 2}\n'
        assert list(jsonl_iter(data, max_encoded_bytes=len(data))) == [
            {"a": 1},
            {"b": 2},
        ]
        with pytest.raises(JsonlBudgetError):
            list(jsonl_iter(data, max_encoded_bytes=len(data) - 1))

    def test_encoded_budget_cannot_be_skipped(self) -> None:
        iterator = jsonl_iter(
            b'{"a": 1}\n{"b": 2}\n', skip_invalid=True, max_encoded_bytes=8
        )
        with pytest.raises(JsonlBudgetError):
            list(iterator)

    def test_encoded_budget_bounds_reads(self) -> None:
        stream = _OneShotStream(b'{"a": 1}\n{"b": 2}\n')
        with pytest.raises(JsonlBudgetError):
            list(jsonl_iter(stream, chunk_size=65536, max_encoded_bytes=8))
        assert sum(stream.read_sizes) <= 9

    def test_depth_budget_boundary(self) -> None:
        data = b'{"a": {"b": {"c": 1}}}\n'
        assert list(jsonl_iter(data, max_depth=3)) == [{"a": {"b": {"c": 1}}}]
        with pytest.raises(JsonlBudgetError) as exc_info:
            list(jsonl_iter(data, max_depth=2))
        assert exc_info.value.line == 1

    def test_depth_ignores_brackets_inside_strings(self) -> None:
        data = b'{"a": "[[[{{{"}\n'
        assert list(jsonl_iter(data, max_depth=1)) == [{"a": "[[[{{{"}]

    def test_depth_budget_can_be_skipped(self) -> None:
        iterator = jsonl_iter(
            b'{"a": {"b": {"c": 1}}}\n{"ok": 1}\n', skip_invalid=True, max_depth=2
        )
        assert list(iterator) == [{"ok": 1}]
        assert [(r.line, r.exception) for r in iterator.rejections] == [
            (1, JsonlBudgetError)
        ]

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"chunk_size": 0}, "chunk_size"),
            ({"chunk_size": True}, "chunk_size"),
            ({"max_line_bytes": 0}, "max_line_bytes"),
            ({"max_depth": 0}, "max_depth"),
            ({"max_encoded_bytes": -1}, "max_encoded_bytes"),
            ({"skip_invalid": 1}, "skip_invalid"),
        ],
    )
    def test_invalid_arguments(self, kwargs: dict[str, Any], match: str) -> None:
        stream = _OneShotStream(b'{"a": 1}\n')
        with pytest.raises(ValueError, match=match):
            jsonl_iter(stream, **kwargs)
        assert stream.read_sizes == []

    def test_invalid_input_type(self) -> None:
        with pytest.raises(TypeError, match="must be a Response, bytes or"):
            jsonl_iter("text")  # type: ignore[arg-type]

    def test_stream_returning_text_is_rejected(self) -> None:
        class TextStream:
            def read(self, size: int) -> str:
                return '{"a": 1}\n'

        with pytest.raises(TypeError, match="must return bytes"):
            list(jsonl_iter(TextStream()))  # type: ignore[arg-type]

    def test_parser_recursion_is_a_budget_error(self) -> None:
        depth = 20_000
        data = ('{"a":' * depth + "1" + "}" * depth + "\n").encode()
        with pytest.raises(JsonlBudgetError) as exc_info:
            list(jsonl_iter(data, max_depth=depth + 1))
        assert exc_info.value.line == 1
        assert isinstance(exc_info.value.__cause__, RecursionError)

    def test_error_closes_the_iterator(self) -> None:
        iterator = jsonl_iter(b'{"a": 1}\nBAD\n')
        assert next(iterator) == {"a": 1}
        with pytest.raises(JsonlError):
            next(iterator)
        assert list(iterator) == []

    def test_iterator_returns_itself(self) -> None:
        iterator = jsonl_iter(b'{"a": 1}\n')
        assert iter(iterator) is iterator

    def test_crlf_split_across_chunks(self) -> None:
        data = b'{"a": 1}\r\n{"b": 2}\r\n'
        stream = _OneShotStream(data, max_read=1)
        assert list(jsonl_iter(stream, chunk_size=2)) == [{"a": 1}, {"b": 2}]

    def test_no_trailing_newline_with_short_reads(self) -> None:
        stream = _OneShotStream(b'{"a": 1}', max_read=1)
        assert list(jsonl_iter(stream, chunk_size=3)) == [{"a": 1}]

    def test_bom_on_a_later_line_is_rejected(self) -> None:
        data = b'\n\xef\xbb\xbf{"a": 1}\n'
        with pytest.raises(JsonlError) as exc_info:
            list(jsonl_iter(data))
        assert exc_info.value.line == 2
        assert exc_info.value.exception is json.JSONDecodeError

    def test_rejections_are_not_shared_between_iterators(self) -> None:
        first = jsonl_iter(b"BAD\n", skip_invalid=True)
        second = jsonl_iter(b'{"a": 1}\n', skip_invalid=True)
        assert list(first) == []
        assert list(second) == [{"a": 1}]
        assert first.rejections != second.rejections
        assert second.rejections == []


class TestBodyOrStr:
    bbody = b"utf8-body"
    ubody = bbody.decode()

    @pytest.mark.parametrize(
        "obj",
        [
            bbody,
            ubody,
            TextResponse(url="http://example.org/", body=bbody, encoding="utf-8"),
            Response(url="http://example.org/", body=bbody),
        ],
    )
    def test_body_or_str(self, obj: Response | str | bytes) -> None:
        r1 = _body_or_str(obj)
        self._assert_type_and_value(r1, self.ubody, obj)
        r2 = _body_or_str(obj, unicode=True)
        self._assert_type_and_value(r2, self.ubody, obj)
        r3 = _body_or_str(obj, unicode=False)
        self._assert_type_and_value(r3, self.bbody, obj)
        assert type(r1) is type(r2)
        assert type(r1) is not type(r3)  # type: ignore[comparison-overlap]

    def test_body_or_str_unsupported_type(self) -> None:
        with pytest.raises(
            TypeError, match="must be Response or str or bytes, not int"
        ):
            _body_or_str(42)  # type: ignore[call-overload]

    @staticmethod
    def _assert_type_and_value(
        a: str | bytes, b: str | bytes, obj: Response | str | bytes
    ) -> None:
        assert type(a) is type(b), f"Got {type(a)}, expected {type(b)} for {obj!r}"
        assert a == b
