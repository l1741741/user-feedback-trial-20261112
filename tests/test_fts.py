from unittest.mock import ANY

import pytest

from sqlite_utils import Database
from sqlite_utils.utils import sqlite3

search_records = [
    {
        "text": "tanuki are running tricksters",
        "country": "Japan",
        "not_searchable": "foo",
    },
    {
        "text": "racoons are biting trash pandas",
        "country": "USA",
        "not_searchable": "bar",
    },
]


def test_enable_fts(fresh_db):
    table = fresh_db.table("searchable")
    table.insert_all(search_records)
    assert ["searchable"] == fresh_db.table_names()
    table.enable_fts(["text", "country"], fts_version="FTS4")
    assert [
        "searchable",
        "searchable_fts",
        "searchable_fts_segments",
        "searchable_fts_segdir",
        "searchable_fts_docsize",
        "searchable_fts_stat",
    ] == fresh_db.table_names()
    assert [
        {
            "rowid": 1,
            "text": "tanuki are running tricksters",
            "country": "Japan",
            "not_searchable": "foo",
        }
    ] == list(table.search("tanuki"))
    assert [
        {
            "rowid": 2,
            "text": "racoons are biting trash pandas",
            "country": "USA",
            "not_searchable": "bar",
        }
    ] == list(table.search("usa"))
    assert [] == list(table.search("bar"))


def test_enable_fts_escape_table_names(fresh_db):
    # Table names with restricted chars are handled correctly.
    # colons and dots are restricted characters for table names.
    table = fresh_db.table("http://example.com")
    table.insert_all(search_records)
    assert ["http://example.com"] == fresh_db.table_names()
    table.enable_fts(["text", "country"], fts_version="FTS4")
    assert [
        "http://example.com",
        "http://example.com_fts",
        "http://example.com_fts_segments",
        "http://example.com_fts_segdir",
        "http://example.com_fts_docsize",
        "http://example.com_fts_stat",
    ] == fresh_db.table_names()
    assert [
        {
            "rowid": 1,
            "text": "tanuki are running tricksters",
            "country": "Japan",
            "not_searchable": "foo",
        }
    ] == list(table.search("tanuki"))
    assert [
        {
            "rowid": 2,
            "text": "racoons are biting trash pandas",
            "country": "USA",
            "not_searchable": "bar",
        }
    ] == list(table.search("usa"))
    assert [] == list(table.search("bar"))


def test_search_duplicate_columns_are_deduped(fresh_db):
    # https://github.com/simonw/sqlite-utils/issues/624
    table = fresh_db.table("t")
    table.insert_all(search_records)
    table.enable_fts(["text", "country"], fts_version="FTS4")
    rows = list(table.search("tanuki", columns=["text", "text"]))
    assert rows == [
        {
            "text": "tanuki are running tricksters",
            "text_2": "tanuki are running tricksters",
        }
    ]


def test_search_limit_offset(fresh_db):
    table = fresh_db.table("t")
    table.insert_all(search_records)
    table.enable_fts(["text", "country"], fts_version="FTS4")
    assert len(list(table.search("are"))) == 2
    assert len(list(table.search("are", limit=1))) == 1
    assert next(iter(table.search("are", limit=1, order_by="rowid")))["rowid"] == 1
    assert (
        next(iter(table.search("are", limit=1, offset=1, order_by="rowid")))["rowid"]
        == 2
    )


def test_search_offset_without_limit(fresh_db):
    table = fresh_db.table("t")
    table.insert_all(search_records)
    table.enable_fts(["text", "country"], fts_version="FTS4")
    assert [row["rowid"] for row in table.search("are", order_by="rowid")] == [1, 2]
    assert [
        row["rowid"] for row in table.search("are", offset=1, order_by="rowid")
    ] == [2]
    assert table.search_sql(offset=1).strip().endswith("limit -1 offset 1")


@pytest.mark.parametrize("fts_version", ("FTS4", "FTS5"))
def test_search_where(fresh_db, fts_version):
    table = fresh_db.table("t")
    table.insert_all(search_records)
    table.enable_fts(["text", "country"], fts_version=fts_version)
    results = list(
        table.search("are", where="country = :country", where_args={"country": "Japan"})
    )
    assert results == [
        {
            "rowid": 1,
            "text": "tanuki are running tricksters",
            "country": "Japan",
            "not_searchable": "foo",
        }
    ]


def test_search_where_args_disallows_query(fresh_db):
    table = fresh_db.table("t")
    with pytest.raises(ValueError) as ex:
        list(
            table.search(
                "x", where="author = :query", where_args={"query": "not allowed"}
            )
        )
    assert (
        ex.value.args[0]
        == "'query' is a reserved key and cannot be passed to where_args for .search()"
    )


def test_search_include_rank(fresh_db):
    table = fresh_db.table("t")
    table.insert_all(search_records)
    table.enable_fts(["text", "country"], fts_version="FTS5")
    results = list(table.search("are", include_rank=True))
    assert results == [
        {
            "rowid": 1,
            "text": "tanuki are running tricksters",
            "country": "Japan",
            "not_searchable": "foo",
            "rank": ANY,
        },
        {
            "rowid": 2,
            "text": "racoons are biting trash pandas",
            "country": "USA",
            "not_searchable": "bar",
            "rank": ANY,
        },
    ]
    assert isinstance(results[0]["rank"], float)
    assert isinstance(results[1]["rank"], float)
    assert results[0]["rank"] < results[1]["rank"]


def test_enable_fts_table_names_containing_spaces(fresh_db):
    table = fresh_db.table("test")
    table.insert({"column with spaces": "in its name"})
    table.enable_fts(["column with spaces"])
    assert [
        "test",
        "test_fts",
        "test_fts_data",
        "test_fts_idx",
        "test_fts_docsize",
        "test_fts_config",
    ] == fresh_db.table_names()


def test_populate_fts(fresh_db):
    table = fresh_db.table("populatable")
    table.insert(search_records[0])
    table.enable_fts(["text", "country"], fts_version="FTS4")
    assert [] == list(table.search("trash pandas"))
    table.insert(search_records[1])
    assert [] == list(table.search("trash pandas"))
    # Now run populate_fts to make this record available
    table.populate_fts(["text", "country"])
    rows = list(table.search("usa"))
    assert [
        {
            "rowid": 2,
            "text": "racoons are biting trash pandas",
            "country": "USA",
            "not_searchable": "bar",
        }
    ] == rows


def test_populate_fts_escape_table_names(fresh_db):
    # Restricted characters such as colon and dots should be escaped.
    table = fresh_db.table("http://example.com")
    table.insert(search_records[0])
    table.enable_fts(["text", "country"], fts_version="FTS4")
    assert [] == list(table.search("trash pandas"))
    table.insert(search_records[1])
    assert [] == list(table.search("trash pandas"))
    # Now run populate_fts to make this record available
    table.populate_fts(["text", "country"])
    assert [
        {
            "rowid": 2,
            "text": "racoons are biting trash pandas",
            "country": "USA",
            "not_searchable": "bar",
        }
    ] == list(table.search("usa"))


@pytest.mark.parametrize("fts_version", ("4", "5"))
def test_fts_tokenize(fresh_db, fts_version):
    table_name = f"searchable_{fts_version}"
    table = fresh_db.table(table_name)
    table.insert_all(search_records)
    # Test without porter stemming
    table.enable_fts(
        ["text", "country"],
        fts_version=f"FTS{fts_version}",
    )
    assert [] == list(table.search("bite"))
    # Test WITH stemming
    table.disable_fts()
    table.enable_fts(
        ["text", "country"],
        fts_version=f"FTS{fts_version}",
        tokenize="porter",
    )
    rows = list(table.search("bite", order_by="rowid"))
    assert len(rows) == 1
    assert {
        "rowid": 2,
        "text": "racoons are biting trash pandas",
        "country": "USA",
        "not_searchable": "bar",
    }.items() <= rows[0].items()


def test_fts_tokenize_escaped(fresh_db):
    # A malicious tokenize value must not be able to break out of the
    # string literal in the CREATE VIRTUAL TABLE statement.
    table = fresh_db.table("searchable")
    table.insert_all(search_records)
    malicious = "porter'); CREATE TABLE injected(x); --"
    with pytest.raises(Exception):
        table.enable_fts(["text"], tokenize=malicious)
    # The injected statement must not have executed
    assert "injected" not in fresh_db.table_names()


def test_optimize_fts(fresh_db):
    for fts_version in ("4", "5"):
        table_name = f"searchable_{fts_version}"
        table = fresh_db.table(table_name)
        table.insert_all(search_records)
        table.enable_fts(["text", "country"], fts_version=f"FTS{fts_version}")
    # You can call optimize successfully against the tables OR their _fts equivalents:
    for table_name in (
        "searchable_4",
        "searchable_5",
        "searchable_4_fts",
        "searchable_5_fts",
    ):
        fresh_db.table(table_name).optimize()


def test_enable_fts_with_triggers(fresh_db):
    table = fresh_db.table("searchable")
    table.insert(search_records[0])
    table.enable_fts(["text", "country"], fts_version="FTS4", create_triggers=True)
    rows1 = list(table.search("tanuki"))
    assert len(rows1) == 1
    assert rows1 == [
        {
            "rowid": 1,
            "text": "tanuki are running tricksters",
            "country": "Japan",
            "not_searchable": "foo",
        }
    ]
    table.insert(search_records[1])
    # Triggers will auto-populate FTS virtual table, not need to call populate_fts()
    rows2 = list(table.search("usa"))
    assert rows2 == [
        {
            "rowid": 2,
            "text": "racoons are biting trash pandas",
            "country": "USA",
            "not_searchable": "bar",
        }
    ]
    assert [] == list(table.search("bar"))


@pytest.mark.parametrize("create_triggers", [True, False])
def test_disable_fts(fresh_db, create_triggers):
    table = fresh_db.table("searchable")
    table.insert(search_records[0])
    table.enable_fts(["text", "country"], create_triggers=create_triggers)
    assert {
        "searchable",
        "searchable_fts",
        "searchable_fts_data",
        "searchable_fts_idx",
        "searchable_fts_docsize",
        "searchable_fts_config",
    } == set(fresh_db.table_names())
    if create_triggers:
        expected_triggers = {"searchable_ai", "searchable_ad", "searchable_au"}
    else:
        expected_triggers = set()
    assert expected_triggers == {
        r[0]
        for r in fresh_db.execute(
            "select name from sqlite_master where type = 'trigger'"
        ).fetchall()
    }
    # Now run .disable_fts() and confirm it worked
    table.disable_fts()
    assert (
        0
        == fresh_db.execute(
            "select count(*) from sqlite_master where type = 'trigger'"
        ).fetchone()[0]
    )
    assert ["searchable"] == fresh_db.table_names()


def test_rebuild_fts(fresh_db):
    table = fresh_db.table("searchable")
    table.insert(search_records[0])
    table.enable_fts(["text", "country"])
    # Run a search
    rows = list(table.search("are"))
    assert len(rows) == 1
    assert {
        "rowid": 1,
        "text": "tanuki are running tricksters",
        "country": "Japan",
        "not_searchable": "foo",
    }.items() <= rows[0].items()
    # Insert another record
    table.insert(search_records[1])
    # This should NOT show up in searches
    assert len(list(table.search("are"))) == 1
    # Running rebuild_fts() should fix it
    table.rebuild_fts()
    rows2 = list(table.search("are"))
    assert len(rows2) == 2


@pytest.mark.parametrize("method", ["optimize", "rebuild_fts"])
def test_optimize_and_rebuild_fts_commit(tmpdir, method):
    path = str(tmpdir / "test.db")
    db = Database(path)
    table = db.table("searchable")
    table.insert(search_records[0])
    table.enable_fts(["text", "country"])
    getattr(table, method)()
    # The connection must not be left inside an open transaction,
    # otherwise this and all subsequent writes are lost on close
    assert not db.conn.in_transaction
    table.insert(search_records[1])
    db.close()
    db2 = Database(path)
    assert db2.table("searchable").count == 2
    db2.close()


@pytest.mark.parametrize("invalid_table", ["does_not_exist", "not_searchable"])
def test_rebuild_fts_invalid(fresh_db, invalid_table):
    fresh_db.table("not_searchable").insert({"foo": "bar"})
    # Raise OperationalError on invalid table
    with pytest.raises(sqlite3.OperationalError):
        fresh_db.table(invalid_table).rebuild_fts()


@pytest.mark.parametrize("fts_version", ["FTS4", "FTS5"])
def test_rebuild_removes_junk_docsize_rows(tmpdir, fts_version):
    # Recreating https://github.com/simonw/sqlite-utils/issues/149
    path = tmpdir / "test.db"
    db = Database(str(path), recursive_triggers=False)
    licenses = [{"key": "apache2", "name": "Apache 2"}, {"key": "bsd", "name": "BSD"}]
    db.table("licenses").insert_all(licenses, pk="key", replace=True)
    db.table("licenses").enable_fts(
        ["name"], create_triggers=True, fts_version=fts_version
    )
    assert db.table("licenses_fts_docsize").count == 2
    # Bug: insert with replace increases the number of rows in _docsize:
    db.table("licenses").insert_all(licenses, pk="key", replace=True)
    assert db.table("licenses_fts_docsize").count == 4
    # rebuild should fix this:
    db.table("licenses_fts").rebuild_fts()
    assert db.table("licenses_fts_docsize").count == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"columns": ["title"]},
        {"fts_version": "FTS4"},
        {"create_triggers": True},
        {"tokenize": "porter"},
    ],
)
def test_enable_fts_replace(kwargs):
    db = Database(memory=True)
    db.table("books").insert(
        {
            "id": 1,
            "title": "Habits of Australian Marsupials",
            "author": "Marlee Hawkins",
        },
        pk="id",
    )
    db.table("books").enable_fts(["title", "author"])
    assert not db.table("books").triggers
    assert db.table("books_fts").columns_dict.keys() == {"title", "author"}
    assert "FTS5" in db.table("books_fts").schema
    assert "porter" not in db.table("books_fts").schema
    # Now modify the FTS configuration
    should_have_changed_columns = "columns" in kwargs
    if "columns" not in kwargs:
        kwargs["columns"] = ["title", "author"]
    db.table("books").enable_fts(**kwargs, replace=True)
    # Check that the new configuration is correct
    if should_have_changed_columns:
        assert db.table("books_fts").columns_dict.keys() == {"title"}
    if "create_triggers" in kwargs:
        assert db.table("books").triggers
    if "fts_version" in kwargs:
        assert "FTS4" in db.table("books_fts").schema
    if "tokenize" in kwargs:
        assert "porter" in db.table("books_fts").schema


def test_enable_fts_replace_does_nothing_if_args_the_same():
    queries = []
    db = Database(memory=True, tracer=lambda sql, params: queries.append((sql, params)))
    db.table("books").insert(
        {
            "id": 1,
            "title": "Habits of Australian Marsupials",
            "author": "Marlee Hawkins",
        },
        pk="id",
    )
    db.table("books").enable_fts(["title", "author"], create_triggers=True)
    queries.clear()
    # Running that again shouldn't run much SQL:
    db.table("books").enable_fts(
        ["title", "author"], create_triggers=True, replace=True
    )
    # The only SQL that executed should be select statements
    assert all(q[0].startswith("select ") for q in queries)


def test_enable_fts_replace_handles_legacy_bracket_quoted_content_table():
    db = Database(memory=True)
    db.table("books").insert(
        {
            "id": 1,
            "title": "Habits of Australian Marsupials",
            "author": "Marlee Hawkins",
        },
        pk="id",
    )
    db.executescript("""
        CREATE VIRTUAL TABLE [books_fts] USING FTS5 (
            [title],
            content=[books]
        );
    """)

    db.table("books").enable_fts(["title", "author"], replace=True)

    assert db.table("books_fts").columns_dict.keys() == {"title", "author"}
    assert 'content="books"' in db.table("books_fts").schema


def test_view_has_no_enable_fts():
    db = Database(memory=True)
    db.create_view("hello", "select 1 + 1")
    # Views deliberately do not have an enable_fts() method
    with pytest.raises(AttributeError):
        db.view("hello").enable_fts()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "kwargs,fts,expected",
    [
        (
            {},
            "FTS5",
            (
                'with "original" as (\n'
                "    select\n"
                "        rowid,\n"
                "        *\n"
                '    from "books"\n'
                ")\n"
                "select\n"
                '    "original".*\n'
                "from\n"
                '    "original"\n'
                '    join "books_fts" on "original".rowid = "books_fts".rowid\n'
                "where\n"
                '    "books_fts" match :query\n'
                "order by\n"
                '    "books_fts".rank'
            ),
        ),
        (
            {"columns": ["title"], "order_by": "rowid", "limit": 10},
            "FTS5",
            (
                'with "original" as (\n'
                "    select\n"
                "        rowid,\n"
                '        "title"\n'
                '    from "books"\n'
                ")\n"
                "select\n"
                '    "original"."title"\n'
                "from\n"
                '    "original"\n'
                '    join "books_fts" on "original".rowid = "books_fts".rowid\n'
                "where\n"
                '    "books_fts" match :query\n'
                "order by\n"
                "    rowid\n"
                "limit 10"
            ),
        ),
        (
            {"where": "author = :author"},
            "FTS5",
            (
                'with "original" as (\n'
                "    select\n"
                "        rowid,\n"
                "        *\n"
                '    from "books"\n'
                "    where author = :author\n"
                ")\n"
                "select\n"
                '    "original".*\n'
                "from\n"
                '    "original"\n'
                '    join "books_fts" on "original".rowid = "books_fts".rowid\n'
                "where\n"
                '    "books_fts" match :query\n'
                "order by\n"
                '    "books_fts".rank'
            ),
        ),
        (
            {"columns": ["title"]},
            "FTS4",
            (
                'with "original" as (\n'
                "    select\n"
                "        rowid,\n"
                '        "title"\n'
                '    from "books"\n'
                ")\n"
                "select\n"
                '    "original"."title"\n'
                "from\n"
                '    "original"\n'
                '    join "books_fts" on "original".rowid = "books_fts".rowid\n'
                "where\n"
                '    "books_fts" match :query\n'
                "order by\n"
                "    rank_bm25(matchinfo(\"books_fts\", 'pcnalx'))"
            ),
        ),
        (
            {"offset": 1, "limit": 1},
            "FTS4",
            (
                'with "original" as (\n'
                "    select\n"
                "        rowid,\n"
                "        *\n"
                '    from "books"\n'
                ")\n"
                "select\n"
                '    "original".*\n'
                "from\n"
                '    "original"\n'
                '    join "books_fts" on "original".rowid = "books_fts".rowid\n'
                "where\n"
                '    "books_fts" match :query\n'
                "order by\n"
                "    rank_bm25(matchinfo(\"books_fts\", 'pcnalx'))\n"
                "limit 1 offset 1"
            ),
        ),
        (
            {"limit": 2},
            "FTS4",
            (
                'with "original" as (\n'
                "    select\n"
                "        rowid,\n"
                "        *\n"
                '    from "books"\n'
                ")\n"
                "select\n"
                '    "original".*\n'
                "from\n"
                '    "original"\n'
                '    join "books_fts" on "original".rowid = "books_fts".rowid\n'
                "where\n"
                '    "books_fts" match :query\n'
                "order by\n"
                "    rank_bm25(matchinfo(\"books_fts\", 'pcnalx'))\n"
                "limit 2"
            ),
        ),
        (
            {"where": "author = :author"},
            "FTS4",
            (
                'with "original" as (\n'
                "    select\n"
                "        rowid,\n"
                "        *\n"
                '    from "books"\n'
                "    where author = :author\n"
                ")\n"
                "select\n"
                '    "original".*\n'
                "from\n"
                '    "original"\n'
                '    join "books_fts" on "original".rowid = "books_fts".rowid\n'
                "where\n"
                '    "books_fts" match :query\n'
                "order by\n"
                "    rank_bm25(matchinfo(\"books_fts\", 'pcnalx'))"
            ),
        ),
        (
            {"include_rank": True},
            "FTS5",
            (
                'with "original" as (\n'
                "    select\n"
                "        rowid,\n"
                "        *\n"
                '    from "books"\n'
                ")\n"
                "select\n"
                '    "original".*,\n'
                '    "books_fts".rank rank\n'
                "from\n"
                '    "original"\n'
                '    join "books_fts" on "original".rowid = "books_fts".rowid\n'
                "where\n"
                '    "books_fts" match :query\n'
                "order by\n"
                '    "books_fts".rank'
            ),
        ),
        (
            {"include_rank": True},
            "FTS4",
            (
                'with "original" as (\n'
                "    select\n"
                "        rowid,\n"
                "        *\n"
                '    from "books"\n'
                ")\n"
                "select\n"
                '    "original".*,\n'
                "    rank_bm25(matchinfo(\"books_fts\", 'pcnalx')) rank\n"
                "from\n"
                '    "original"\n'
                '    join "books_fts" on "original".rowid = "books_fts".rowid\n'
                "where\n"
                '    "books_fts" match :query\n'
                "order by\n"
                "    rank_bm25(matchinfo(\"books_fts\", 'pcnalx'))"
            ),
        ),
    ],
)
def test_search_sql(kwargs, fts, expected):
    db = Database(memory=True)
    db.table("books").insert(
        {
            "title": "Habits of Australian Marsupials",
            "author": "Marlee Hawkins",
        }
    )
    db.table("books").enable_fts(["title", "author"], fts_version=fts)
    sql = db.table("books").search_sql(**kwargs)
    assert sql == expected


@pytest.mark.parametrize(
    "input,expected",
    (
        ("dog", '"dog"'),
        ("cat,", '"cat,"'),
        ("cat's", '"cat\'s"'),
        ("dog.", '"dog."'),
        ("cat dog", '"cat" "dog"'),
        # If a phrase is already double quoted, leave it so
        ('"cat dog"', '"cat dog"'),
        ('"cat dog" fish', '"cat dog" "fish"'),
        # Sensibly handle unbalanced double quotes
        ('cat"', '"cat"'),
        ('"cat dog" "fish', '"cat dog" "fish"'),
    ),
)
def test_quote_fts_query(fresh_db, input, expected):
    table = fresh_db.table("searchable")
    table.insert_all(search_records)
    table.enable_fts(["text", "country"])
    quoted = fresh_db.quote_fts(input)
    assert quoted == expected
    # Executing query does not crash.
    list(table.search(quoted))


def test_search_quote(fresh_db):
    table = fresh_db.table("searchable")
    table.insert_all(search_records)
    table.enable_fts(["text", "country"])
    query = "cat's"
    with pytest.raises(sqlite3.OperationalError):
        list(table.search(query))
    # No exception with quote=True
    list(table.search(query, quote=True))


def test_enable_fts_cli_on_view_errors(tmpdir):
    db_path = str(tmpdir / "test.db")
    db = Database(db_path)
    db.table("t").insert({"text": "hello"})
    db.create_view("v", "select * from t")
    db.close()
    from click.testing import CliRunner

    from sqlite_utils import cli as cli_module

    result = CliRunner().invoke(cli_module.cli, ["enable-fts", db_path, "v", "text"])
    assert result.exit_code == 1
    assert result.output.strip() == "Error: Table v is actually a view"


# ---------------------------------------------------------------------------
# Index lifecycle: atomicity, replacement, triggers and special names
# ---------------------------------------------------------------------------


def _snapshot(db):
    "Schema, rows in every (shadow) table and triggers, for exact comparisons."
    schema = db.execute(
        "select type, name, tbl_name, sql from sqlite_master "
        "where name not like 'sqlite_%' order by type, name"
    ).fetchall()
    rows = {}
    for name in db.table_names():
        rows[name] = db.execute(
            "select * from {}".format(quote_sqlite_identifier(name))
        ).fetchall()
    return schema, rows


def quote_sqlite_identifier(name):
    from sqlite_utils.db import quote_identifier

    return quote_identifier(name)


@pytest.mark.parametrize("fts_version", ["FTS4", "FTS5"])
def test_enable_fts_atomic_when_populate_fails(fresh_db, fts_version, monkeypatch):
    import sqlite_utils.db as db_module

    table = fresh_db.table("t")
    table.insert_all([{"id": 1, "a": "hello"}, {"id": 2, "a": "world"}], pk="id")
    before = _snapshot(fresh_db)

    def fail(self, fts_table):
        raise RuntimeError("injected failure while populating")

    monkeypatch.setattr(db_module.Table, "_rebuild_fts_index", fail)
    with pytest.raises(RuntimeError, match="injected failure"):
        table.enable_fts(["a"], fts_version=fts_version, create_triggers=True)
    monkeypatch.undo()

    assert _snapshot(fresh_db) == before
    # No half-built virtual table, shadow tables or triggers remain
    assert fresh_db.table_names() == ["t"]
    assert fresh_db.triggers == []
    # And the caller can simply try again successfully
    table.enable_fts(["a"], fts_version=fts_version, create_triggers=True)
    assert list(table.search("hello"))[0]["id"] == 1


@pytest.mark.parametrize("fts_version", ["FTS4", "FTS5"])
def test_enable_fts_atomic_when_trigger_creation_fails(fresh_db, fts_version):
    # A trigger that occupies one of the names but does not belong to this
    # index: SQLite raises "trigger ... already exists" on the last trigger,
    # after the index and the first two triggers have been created
    table = fresh_db.table("t")
    table.insert({"id": 1, "a": "hello"}, pk="id")
    fresh_db.execute('CREATE TRIGGER "t_au" AFTER INSERT ON "t" BEGIN SELECT 1; END')
    before = _snapshot(fresh_db)

    with pytest.raises(sqlite3.OperationalError, match="trigger|already exists"):
        table.enable_fts(["a"], fts_version=fts_version, create_triggers=True)

    assert _snapshot(fresh_db) == before
    assert fresh_db.table_names() == ["t"]
    assert [t.name for t in fresh_db.triggers] == ["t_au"]


@pytest.mark.parametrize("fts_version", ["FTS4", "FTS5"])
def test_enable_fts_atomic_when_tokenizer_invalid(fresh_db, fts_version):
    table = fresh_db.table("t")
    table.insert({"id": 1, "a": "hello"}, pk="id")
    with pytest.raises(sqlite3.OperationalError):
        table.enable_fts(["a"], fts_version=fts_version, tokenize="no_such_tokenizer")
    assert fresh_db.table_names() == ["t"]
    assert fresh_db.triggers == []


@pytest.mark.parametrize("fts_version", ["FTS4", "FTS5"])
def test_enable_fts_atomic_when_table_has_no_rowid(fresh_db, fts_version):
    # WITHOUT ROWID tables have no rowid to index: CREATE and trigger creation
    # succeed, the first population step fails
    fresh_db.execute("CREATE TABLE t (a TEXT PRIMARY KEY, b TEXT) WITHOUT ROWID")
    fresh_db.execute("INSERT INTO t VALUES ('hello', 'x')")
    fresh_db.execute("CREATE TRIGGER t_business AFTER INSERT ON t BEGIN SELECT 1; END")
    before = _snapshot(fresh_db)

    with pytest.raises(sqlite3.OperationalError):
        fresh_db.table("t").enable_fts(
            ["b"], fts_version=fts_version, create_triggers=True
        )

    assert _snapshot(fresh_db) == before
    assert fresh_db.table_names() == ["t"]
    assert [t.name for t in fresh_db.triggers] == ["t_business"]


@pytest.mark.parametrize("fts_version", ["FTS4", "FTS5"])
def test_enable_fts_replace_fails_restores_old_configuration(fresh_db, fts_version):
    table = fresh_db.table("t")
    table.insert_all(
        [{"id": 1, "a": "hello", "b": "bee"}, {"id": 2, "a": "world", "b": "two"}],
        pk="id",
    )
    table.enable_fts(["a"], fts_version=fts_version)
    old_index_rows = fresh_db.execute(
        "select rowid, a from t_fts order by rowid"
    ).fetchall()
    # Occupy a sync trigger name with an unrelated trigger so replacement fails
    # after the old index has already been dropped and the new one populated
    fresh_db.execute('CREATE TRIGGER "t_au" AFTER INSERT ON "t" BEGIN SELECT 1; END')
    before = _snapshot(fresh_db)

    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        table.enable_fts(
            ["b"], fts_version=fts_version, create_triggers=True, replace=True
        )

    assert _snapshot(fresh_db) == before
    # Old FTS configuration and its index contents are intact
    assert table.detect_fts() == "t_fts"
    assert fresh_db.table("t_fts").columns_dict.keys() == {"a"}
    assert (
        fresh_db.execute("select rowid, a from t_fts order by rowid").fetchall()
        == old_index_rows
    )
    assert [
        r[0]
        for r in fresh_db.execute(
            "select rowid from t_fts where t_fts match 'hello'"
        ).fetchall()
    ] == [1]


def test_enable_fts_replace_is_idempotent(fresh_db):
    table = fresh_db.table("books")
    table.insert({"id": 1, "title": "Habitat", "author": "Author"}, pk="id")
    table.enable_fts(["title", "author"], create_triggers=True, tokenize="porter")
    schema = fresh_db.execute("select name, sql from sqlite_master order by name")
    index_rows = fresh_db.execute("select * from books_fts")
    # Running with the same configuration must not re-create anything or re-index
    table.enable_fts(
        ["title", "author"], create_triggers=True, tokenize="porter", replace=True
    )
    assert (
        fresh_db.execute("select name, sql from sqlite_master order by name").fetchall()
        == schema.fetchall()
    )
    assert (
        fresh_db.execute("select * from books_fts").fetchall() == index_rows.fetchall()
    )


@pytest.mark.parametrize("fts_version", ["FTS4", "FTS5"])
def test_enable_fts_replace_changes_columns_and_tokenizer(fresh_db, fts_version):
    table = fresh_db.table("books")
    table.insert(
        {"id": 1, "title": "Habits of Australian Marsupials", "author": "Marlee"},
        pk="id",
    )
    table.enable_fts(["title", "author"], fts_version=fts_version)
    table.enable_fts(
        ["title"], fts_version=fts_version, tokenize="porter", replace=True
    )
    assert fresh_db.table("books_fts").columns_dict.keys() == {"title"}
    assert "porter" in fresh_db.table("books_fts").schema
    # Search works against the replaced index, including stemming
    assert [r["rowid"] for r in table.search("marsupial")] == [1]
    # And rows not in the new column set were not indexed
    table.insert({"id": 2, "title": "Second", "author": "Nobody"}, pk="id")
    table.populate_fts(["title"])
    assert list(table.search("nobody")) == []


@pytest.mark.parametrize("fts_version", ["FTS4", "FTS5"])
def test_enable_fts_replace_toggles_triggers(fresh_db, fts_version):
    table = fresh_db.table("books")
    table.insert({"id": 1, "title": "One"}, pk="id")
    table.enable_fts(["title"], fts_version=fts_version)
    assert table.triggers == []
    table.enable_fts(
        ["title"], fts_version=fts_version, create_triggers=True, replace=True
    )
    assert {t.name for t in table.triggers} == {"books_ai", "books_ad", "books_au"}
    table.enable_fts(["title"], fts_version=fts_version, replace=True)
    assert table.triggers == []
    # Index still intact after trashing the triggers
    assert [r["rowid"] for r in table.search("one")] == [1]


@pytest.mark.parametrize("fts_version", ["FTS4", "FTS5"])
def test_enable_fts_triggers_track_update_and_delete(fresh_db, fts_version):
    table = fresh_db.table("t")
    table.insert_all(
        [{"id": 1, "text": "tanuki"}, {"id": 2, "text": "racoons"}], pk="id"
    )
    table.enable_fts(["text"], fts_version=fts_version, create_triggers=True)

    def matches(query):
        return [
            r[0]
            for r in fresh_db.execute(
                "select rowid from t_fts where t_fts match ?", (query,)
            ).fetchall()
        ]

    assert matches("tanuki") == [1]
    table.update(1, {"text": "badger"})
    assert matches("tanuki") == []
    assert matches("badger") == [1]
    table.delete(2)
    assert matches("racoons") == []
    table.insert({"id": 3, "text": "wolverine"}, pk="id")
    assert matches("wolverine") == [3]
    # The index itself is still structurally sound
    fresh_db.execute("insert into t_fts(t_fts) values('integrity-check')")


@pytest.mark.parametrize("fts_version", ["FTS4", "FTS5"])
def test_populate_fts_is_consistent_after_update_and_delete(fresh_db, fts_version):
    table = fresh_db.table("t")
    table.insert_all(
        [{"id": 1, "text": "tanuki"}, {"id": 2, "text": "racoons"}], pk="id"
    )
    table.enable_fts(["text"], fts_version=fts_version)
    table.update(1, {"text": "badger"})
    table.delete(2)
    table.insert({"id": 3, "text": "wolverine"}, pk="id")
    table.populate_fts(["text"])
    assert [r["rowid"] for r in table.search("tanuki")] == []
    assert [r["rowid"] for r in table.search("racoons")] == []
    assert [r["rowid"] for r in table.search("badger")] == [1]
    assert [r["rowid"] for r in table.search("wolverine")] == [3]


def test_enable_fts_works_inside_caller_transaction(fresh_db):
    table = fresh_db.table("t")
    table.insert({"id": 1, "a": "hello"}, pk="id")
    fresh_db.begin()
    table.insert({"id": 2, "a": "world"}, pk="id")
    table.enable_fts(["a"], create_triggers=True)
    table.populate_fts(["a"])
    assert fresh_db.conn.in_transaction
    assert "t_fts" in fresh_db.table_names()
    # Caller rolls back: business change and index change are both undone
    fresh_db.rollback()
    assert not fresh_db.conn.in_transaction
    assert fresh_db.table_names() == ["t"]
    assert fresh_db.triggers == []
    assert fresh_db.execute("select id from t").fetchall() == [(1,)]


def test_enable_fts_works_inside_outer_atomic(fresh_db):
    table = fresh_db.table("t")
    with pytest.raises(RuntimeError), fresh_db.atomic():
        table.insert({"id": 1, "a": "hello"}, pk="id")
        table.enable_fts(["a"], create_triggers=True)
        raise RuntimeError("boom")
    assert not fresh_db.conn.in_transaction
    assert fresh_db.table_names() == []
    assert fresh_db.triggers == []


def test_enable_fts_works_inside_nested_atomic(fresh_db):
    table = fresh_db.table("t")
    table.insert({"id": 1, "a": "hello"}, pk="id")
    with fresh_db.atomic():
        table.insert({"id": 2, "a": "world"}, pk="id")
        with fresh_db.atomic():
            table.enable_fts(["a"], create_triggers=True)
    assert fresh_db.conn.in_transaction is False
    assert [r["rowid"] for r in table.search("world")] == [2]


def test_enable_fts_failure_leaves_caller_transaction_usable(fresh_db):
    table = fresh_db.table("t")
    table.insert({"id": 1, "a": "hello"}, pk="id")
    fresh_db.begin()
    table.insert({"id": 2, "a": "world"}, pk="id")
    with pytest.raises(sqlite3.OperationalError):
        table.enable_fts(["a"], tokenize="no_such_tokenizer")
    assert fresh_db.conn.in_transaction
    assert fresh_db.table_names() == ["t"]
    # Caller can carry on writing and commit their own changes
    table.insert({"id": 3, "a": "third"}, pk="id")
    fresh_db.commit()
    assert fresh_db.execute("select id from t order by id").fetchall() == [
        (1,),
        (2,),
        (3,),
    ]


def test_fts_operations_persist_independently(tmpdir):
    path = str(tmpdir / "test.db")
    db = Database(path)
    table = db.table("t")
    table.insert({"id": 1, "a": "hello"}, pk="id")
    table.enable_fts(["a"], create_triggers=True)
    table.populate_fts(["a"])
    assert not db.conn.in_transaction
    db.close()
    db = Database(path)
    assert "t_fts" in db.table_names()
    assert [r["rowid"] for r in db.table("t").search("hello")] == [1]
    db.table("t").disable_fts()
    assert not db.conn.in_transaction
    db.close()
    db = Database(path)
    assert db.table_names() == ["t"]
    db.close()


@pytest.mark.parametrize("fts_version", ["FTS4", "FTS5"])
@pytest.mark.parametrize(
    "table_name",
    [
        "weird table",
        'we"ird',
        "a,b",
        "select",
        "100%",
        "a_b",
        "o'brien",
        "[brackets]",
        "main.point",
    ],
)
def test_enable_fts_special_table_names(fresh_db, fts_version, table_name):
    table = fresh_db.table(table_name)
    table.insert({"id": 1, "select": "hello world", 'a"b': "x"}, pk="id")
    table.enable_fts(["select"], fts_version=fts_version, create_triggers=True)
    assert table.detect_fts() == table_name + "_fts"
    assert [r["rowid"] for r in table.search("hello")] == [1]
    table.update(1, {"select": "goodbye"})
    assert [r["rowid"] for r in table.search("goodbye")] == [1]
    assert [r["rowid"] for r in table.search("hello")] == []
    table.delete(1)
    assert [r["rowid"] for r in table.search("goodbye")] == []
    table.insert({"id": 2, "select": "third thing", 'a"b': "y"}, pk="id")
    assert [r["rowid"] for r in table.search("third")] == [2]
    table.disable_fts()
    assert fresh_db.table_names() == [table_name]
    assert fresh_db.triggers == []


@pytest.mark.parametrize("fts_version", ["FTS4", "FTS5"])
@pytest.mark.parametrize(
    "column_name", ["column with spaces", 'col"umn', "col,umn", "order"]
)
def test_enable_fts_special_column_names(fresh_db, fts_version, column_name):
    table = fresh_db.table("t")
    table.insert({"id": 1, column_name: "hello world"}, pk="id")
    table.enable_fts([column_name], fts_version=fts_version, create_triggers=True)
    assert [r["rowid"] for r in table.search("hello")] == [1]
    table.update(1, {column_name: "goodbye"})
    assert [r["rowid"] for r in table.search("goodbye")] == [1]


def test_disable_fts_does_not_touch_similar_tables(fresh_db):
    for name in ("demo", "demo2", "demo_fts_extra"):
        fresh_db.table(name).insert({"id": 1, "title": "Hello world"}, pk="id")
        fresh_db.table(name).enable_fts(["title"], create_triggers=True)
    fresh_db.table("demo").disable_fts()
    assert fresh_db.table("demo").detect_fts() is None
    for name in ("demo2", "demo_fts_extra"):
        table = fresh_db.table(name)
        assert table.detect_fts() == name + "_fts"
        assert [r["rowid"] for r in table.search("hello")] == [1]
        assert {t.name for t in table.triggers} == {
            name + "_ai",
            name + "_ad",
            name + "_au",
        }


def test_disable_fts_keeps_unrelated_triggers(fresh_db):
    table = fresh_db.table("t")
    table.insert({"id": 1, "a": "hello"}, pk="id")
    table.enable_fts(["a"], create_triggers=True)
    fresh_db.execute("CREATE TRIGGER t_business AFTER INSERT ON t BEGIN SELECT 1; END")
    # A trigger on another table writing into the FTS shadow table must survive
    fresh_db.table("other").insert({"id": 1, "v": "x"}, pk="id")
    fresh_db.execute(
        'CREATE TRIGGER other_sync AFTER INSERT ON "other" '
        'BEGIN INSERT INTO "t_fts" (rowid, "a") VALUES (new.rowid, new.v); END'
    )
    table.disable_fts()
    assert sorted(t.name for t in fresh_db.triggers) == ["other_sync", "t_business"]
    assert set(fresh_db.table_names()) == {"other", "t"}


def test_disable_fts_is_idempotent_when_no_index(fresh_db):
    fresh_db.table("t").insert({"id": 1, "a": "hello"}, pk="id")
    fresh_db.execute("CREATE TRIGGER t_ai AFTER INSERT ON t BEGIN SELECT 1; END")
    before = _snapshot(fresh_db)
    fresh_db.table("t").disable_fts()
    fresh_db.table("t").disable_fts()
    assert _snapshot(fresh_db) == before


def test_disable_fts_leaves_trigger_named_for_other_index(fresh_db):
    # Table t has an FTS index and table demo2 has its own; the trigger names
    # are per table so disabling t must not touch demo2's triggers
    fresh_db.table("t").insert({"id": 1, "a": "hello"}, pk="id")
    fresh_db.table("demo2").insert({"id": 1, "a": "hello"}, pk="id")
    fresh_db.table("t").enable_fts(["a"], create_triggers=True)
    fresh_db.table("demo2").enable_fts(["a"], create_triggers=True)
    fresh_db.table("t").disable_fts()
    assert {t.name for t in fresh_db.table("demo2").triggers} == {
        "demo2_ai",
        "demo2_ad",
        "demo2_au",
    }
    assert fresh_db.table("demo2").detect_fts() == "demo2_fts"


@pytest.mark.parametrize("fts_version", ["FTS4", "FTS5"])
def test_enable_fts_trigger_name_conflict_keeps_existing_trigger(fresh_db, fts_version):
    table = fresh_db.table("t")
    table.insert({"id": 1, "a": "hello"}, pk="id")
    fresh_db.execute('CREATE TRIGGER "t_ai" AFTER INSERT ON "t" BEGIN SELECT 1; END')
    before = _snapshot(fresh_db)
    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        table.enable_fts(["a"], fts_version=fts_version, create_triggers=True)
    assert _snapshot(fresh_db) == before
    # The occupying trigger was not overwritten
    assert (
        fresh_db.execute(
            "select sql from sqlite_master where name = 't_ai'"
        ).fetchone()[0]
        == 'CREATE TRIGGER "t_ai" AFTER INSERT ON "t" BEGIN SELECT 1; END'
    )


def test_enable_fts_does_not_commit_caller_transaction(fresh_db):
    # A Failed enable_fts must not silently commit earlier writes of the caller
    table = fresh_db.table("t")
    fresh_db.begin()
    table.insert({"id": 1, "a": "hello"}, pk="id")
    with pytest.raises(sqlite3.OperationalError):
        table.enable_fts(["a"], tokenize="no_such_tokenizer")
    fresh_db.rollback()
    assert fresh_db.table_names() == []


@pytest.mark.parametrize("fts_version", ["FTS4", "FTS5"])
def test_populate_fts_atomic_when_rebuild_fails(fresh_db, fts_version, monkeypatch):
    import sqlite_utils.db as db_module

    table = fresh_db.table("t")
    table.insert_all([{"id": 1, "a": "hello"}, {"id": 2, "a": "world"}], pk="id")
    table.enable_fts(["a"], fts_version=fts_version)
    before = _snapshot(fresh_db)

    def fail(self, fts_table):
        raise RuntimeError("injected failure while rebuilding")

    monkeypatch.setattr(db_module.Table, "_rebuild_fts_index", fail)
    with pytest.raises(RuntimeError, match="injected failure"):
        table.populate_fts(["a"])
    monkeypatch.undo()
    assert _snapshot(fresh_db) == before


def test_disable_fts_atomic_when_drop_fails(fresh_db):
    table = fresh_db.table("t")
    table.insert({"id": 1, "a": "hello"}, pk="id")
    table.enable_fts(["a"], create_triggers=True)
    # An unrelated object so the test would notice if too much was removed
    fresh_db.execute("CREATE VIEW demo_v AS SELECT 1")
    before = _snapshot(fresh_db)
    original_triggers = list(fresh_db.triggers)

    import sqlite_utils.db as db_module

    real_execute = db_module.Database.execute

    def execute(self, sql, parameters=None):
        if sql.startswith("DROP TABLE") and "_fts" in sql:
            raise sqlite3.OperationalError("injected drop failure")
        return real_execute(self, sql, parameters)

    db_module.Database.execute = execute  # type: ignore[method-assign]
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected drop failure"):
            table.disable_fts()
    finally:
        db_module.Database.execute = real_execute  # type: ignore[method-assign]
    after = _snapshot(fresh_db)
    assert after == before
    assert fresh_db.triggers == original_triggers


@pytest.mark.parametrize(
    "columns", [[], (), ["a", "a"], ["a", "A"], ["missing"], ["a", "missing"]]
)
def test_enable_fts_column_validation(fresh_db, columns):
    from sqlite_utils.db import InvalidColumns

    table = fresh_db.table("t")
    table.insert({"a": "hello", "b": "world"})
    with pytest.raises(InvalidColumns):
        table.enable_fts(columns)
    assert fresh_db.table_names() == ["t"]


@pytest.mark.parametrize(
    "fts_version", ["FTS3", "fts", "FTS5; DROP TABLE t", "", None, 5]
)
def test_enable_fts_version_validation(fresh_db, fts_version):
    table = fresh_db.table("t")
    table.insert({"a": "hello"})
    with pytest.raises(ValueError, match="fts_version"):
        table.enable_fts(["a"], fts_version=fts_version)
    assert fresh_db.table_names() == ["t"]


@pytest.mark.parametrize("fts_version", ["fts4", "FTS5"])
def test_enable_fts_accepts_case_insensitive_version(fresh_db, fts_version):
    table = fresh_db.table("t")
    table.insert({"a": "hello"})
    table.enable_fts(["a"], fts_version=fts_version)
    assert fts_version.upper() in fresh_db.table("t_fts").schema


@pytest.mark.parametrize("tokenize", ["porter", "unicode61 remove_diacritics 0"])
def test_enable_fts_tokenize_formats(fresh_db, tokenize):
    table = fresh_db.table("t")
    table.insert({"a": "hello world"})
    table.enable_fts(["a"], tokenize=tokenize)
    assert tokenize in fresh_db.table("t_fts").schema


def test_enable_fts_tokenize_error_is_atomic(fresh_db):
    table = fresh_db.table("t")
    table.insert({"a": "hello"})
    with pytest.raises(sqlite3.OperationalError):
        table.enable_fts(["a"], tokenize="porter bogus_tokenizer")
    assert fresh_db.table_names() == ["t"]
    assert fresh_db.triggers == []


def test_enable_fts_only_deletes_triggers_of_its_own_index(fresh_db):
    # Existing legacy-style triggers on this table that write into a different
    # FTS table must not be treated as belonging to t_fts
    fresh_db.table("t").insert({"id": 1, "a": "hello"}, pk="id")
    fresh_db.table("other").insert({"id": 1, "a": "hello"}, pk="id")
    fresh_db.table("other").enable_fts(["a"], create_triggers=True)
    fresh_db.execute(
        'CREATE TRIGGER "t_ai" AFTER INSERT ON "t" '
        'BEGIN INSERT INTO "other_fts" (rowid, "a") VALUES (new.rowid, new."a"); END'
    )
    # Add the real index for t, without triggers
    fresh_db.table("t").enable_fts(["a"])
    # disable_fts for t must leave the other-index trigger alone
    fresh_db.table("t").disable_fts()
    assert sorted(t.name for t in fresh_db.triggers) == [
        "other_ad",
        "other_ai",
        "other_au",
        "t_ai",
    ]
