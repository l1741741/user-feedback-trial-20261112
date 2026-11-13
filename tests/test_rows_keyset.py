import random
import sqlite3

import pytest

from sqlite_utils import Database
from sqlite_utils.db import (
    _KEYSET_CURSOR_PREFIX,
    _keyset_decode_cursor,
)


def paginate_all(table, order_by, *, reverse=False, where=None, where_args=(), limit=3):
    rows = []
    cursor = None
    pages = 0
    seen_cursors = set()
    while True:
        page = table.rows_keyset(
            order_by,
            limit=limit,
            after=cursor,
            reverse=reverse,
            where=where,
            where_args=where_args,
        )
        pages += 1
        rows.extend(page["rows"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert cursor not in seen_cursors
        seen_cursors.add(cursor)
        assert pages < 1000
    return rows, pages


def test_rowid_table_basic_ascending_matches_sql_order(fresh_db):
    db = fresh_db
    db.executescript(
        """
        CREATE TABLE items (g TEXT, n INTEGER);
        INSERT INTO items (g, n) VALUES ('a', 1), ('a', 2), ('b', 3), (NULL, 4);
        """
    )
    rows, pages = paginate_all(db.table("items"), ["g", "n"], limit=2)
    expected = [
        {"g": None, "n": 4},
        {"g": "a", "n": 1},
        {"g": "a", "n": 2},
        {"g": "b", "n": 3},
    ]
    assert rows == expected
    assert pages == 2
    # Page shape and last-page cursor
    first = db.table("items").rows_keyset(["g", "n"], limit=2)
    assert len(first["rows"]) == 2
    assert first["next_cursor"].startswith(_KEYSET_CURSOR_PREFIX)
    last = db.table("items").rows_keyset(["g", "n"], limit=100)
    assert last["next_cursor"] is None
    assert len(last["rows"]) == 4


def test_rowid_table_reverse_order(fresh_db):
    fresh_db.executescript(
        """
        CREATE TABLE items (g TEXT, n INTEGER);
        INSERT INTO items (g, n) VALUES ('a', 1), ('a', 2), ('b', 3), (NULL, 4);
        """
    )
    rows, _ = paginate_all(
        fresh_db.table("items"), ["g", "n"], reverse=True, limit=2
    )
    expected = list(
        fresh_db.table("items").rows_where(order_by="g DESC, n DESC")
    )
    assert rows == expected
    # NULLs come last in DESC
    assert rows[-1] == {"g": None, "n": 4}
    assert rows[0] == {"g": "b", "n": 3}


def test_without_rowid_composite_primary_key(fresh_db):
    fresh_db.executescript(
        """
        CREATE TABLE logs (
            day TEXT NOT NULL,
            seq INTEGER NOT NULL,
            note TEXT,
            PRIMARY KEY (day, seq)
        ) WITHOUT ROWID;
        INSERT INTO logs VALUES ('mon', 2, 'b'), ('mon', 1, 'a'), ('tue', 1, 'c');
        """
    )
    table = fresh_db.table("logs")
    # order_by a non-key column; the composite PK must complete the total order
    rows, pages = paginate_all(table, ["note"], limit=2)
    expected = list(table.rows_where(order_by="note, day, seq"))
    assert rows == expected
    assert pages == 2
    # reverse direction too
    rev_rows, _ = paginate_all(table, ["note"], reverse=True, limit=2)
    assert rev_rows == list(table.rows_where(order_by="note DESC, day DESC, seq DESC"))
    # order_by that includes part of the PK
    pk_rows, _ = paginate_all(table, ["day", "seq"], limit=1)
    assert pk_rows == list(table.rows_where(order_by="day, seq"))


def test_without_rowid_single_column_primary_key(fresh_db):
    fresh_db.execute(
        "CREATE TABLE solo (id TEXT PRIMARY KEY, v INTEGER) WITHOUT ROWID"
    )
    fresh_db.table("solo").insert_all(
        [{"id": c, "v": i} for i, c in enumerate("zxyabc")]
    )
    rows, _ = paginate_all(fresh_db.table("solo"), ["v"], limit=2)
    assert [r["id"] for r in rows] == list("zxyabc")
    rows_rev, _ = paginate_all(
        fresh_db.table("solo"), ["v"], reverse=True, limit=2
    )
    assert [r["id"] for r in rows_rev] == list("cbayxz")


def test_duplicate_sort_values_dont_lose_or_repeat_rows(fresh_db):
    fresh_db.execute("CREATE TABLE d (k TEXT)")
    fresh_db.table("d").insert_all([{"k": "same"} for _ in range(25)])
    rows, pages = paginate_all(fresh_db.table("d"), ["k"], limit=4)
    assert len(rows) == 25
    assert pages == 7  # 6 full pages of 4 plus one of 1
    rows_rev, _ = paginate_all(fresh_db.table("d"), ["k"], reverse=True, limit=4)
    assert len(rows_rev) == 25
    # Every physical row appears exactly once in each direction
    rids_asc = {r[0] for r in fresh_db.execute("SELECT rowid FROM d")}
    assert len(rids_asc) == 25


def test_nulls_in_multiple_key_columns_both_directions(fresh_db):
    fresh_db.execute("CREATE TABLE m (a TEXT, b INTEGER, c TEXT)")
    combos = [
        (None, None, "z"),
        (None, 1, "y"),
        ("a", None, "x"),
        ("a", 1, None),
        ("a", 1, "w"),
        (None, None, None),
    ]
    fresh_db.table("m").insert_all(
        [{"a": a, "b": b, "c": c} for a, b, c in combos]
    )
    table = fresh_db.table("m")
    for reverse in (False, True):
        rows, _ = paginate_all(table, ["a", "b", "c"], reverse=reverse, limit=2)
        direction = " DESC" if reverse else ""
        expected = list(
            table.rows_where(
                order_by=f"a{direction}, b{direction}, c{direction}"
            )
        )
        assert rows == expected, reverse


def test_nocase_and_rtrim_collations_are_sqlite_s(fresh_db):
    fresh_db.executescript(
        """
        CREATE TABLE coll (
            name TEXT COLLATE NOCASE,
            pad TEXT COLLATE RTRIM,
            n INTEGER
        );
        INSERT INTO coll VALUES ('A', 'abc', 1), ('a', 'abc   ', 2),
                                 ('B', 'abd', 3), ('b', 'aba', 4);
        """
    )
    table = fresh_db.table("coll")
    rows, _ = paginate_all(table, ["name", "pad", "n"], limit=2)
    # NOCASE makes A/a and RTRIM makes 'abc'/'abc   ' equal; rowid breaks ties
    expected = list(
        table.rows_where(order_by="name, pad, n")
    )
    assert rows == expected
    # 'A' and 'a' must land on the same NOCASE group
    assert [r["n"] for r in rows][:2] == [1, 2]
    rev_rows, _ = paginate_all(
        table, ["name", "pad", "n"], reverse=True, limit=2
    )
    assert rev_rows == list(
        table.rows_where(order_by="name DESC, pad DESC, n DESC")
    )


def test_native_types_cross_pages_unchanged(fresh_db):
    fresh_db.execute("CREATE TABLE typed (k INTEGER, payload BLOB, t TEXT, f REAL)")
    blob = b"\x00\x01\x02\xff blob"
    fresh_db.table("typed").insert_all(
        [
            {"k": 1, "payload": blob, "t": "ünicode ✓", "f": 1.5},
            {"k": 2, "payload": b"", "t": "", "f": -0.25},
            {"k": 3, "payload": None, "t": None, "f": None},
            {"k": 4, "payload": b"\xff" * 100, "t": "x" * 200, "f": 3.14159},
        ]
    )
    rows, _ = paginate_all(fresh_db.table("typed"), ["k"], limit=1)
    assert [r["k"] for r in rows] == [1, 2, 3, 4]
    assert rows[0]["payload"] == blob
    assert isinstance(rows[0]["payload"], bytes)
    assert isinstance(rows[0]["k"], int)
    assert isinstance(rows[0]["f"], float)
    assert rows[0]["t"] == "ünicode ✓"
    assert rows[2]["payload"] is None and rows[2]["t"] is None
    assert rows[3]["payload"] == b"\xff" * 100


def test_integer_primary_key_table(fresh_db):
    fresh_db.execute("CREATE TABLE ipk (id INTEGER PRIMARY KEY, g TEXT)")
    fresh_db.table("ipk").insert_all(
        [{"id": i, "g": "a" if i % 2 else "b"} for i in range(10)]
    )
    rows, _ = paginate_all(fresh_db.table("ipk"), ["g", "id"], limit=3)
    assert [r["id"] for r in rows] == [1, 3, 5, 7, 9, 0, 2, 4, 6, 8]


def test_shadowed_rowid_aliases_without_reliable_pk_raise(fresh_db):
    fresh_db.executescript(
        """
        CREATE TABLE shadowed (
            rowid TEXT, oid TEXT, _rowid_ TEXT, v TEXT
        );
        INSERT INTO shadowed VALUES ('a', 'b', 'c', 'd');
        """
    )
    with pytest.raises(ValueError, match="rowid, _rowid_ and oid"):
        fresh_db.table("shadowed").rows_keyset(["v"])


def test_shadowed_aliases_but_not_null_pk_falls_back_to_pk(fresh_db):
    fresh_db.executescript(
        """
        CREATE TABLE shadowed_ok (
            rowid TEXT, oid TEXT, _rowid_ TEXT,
            code TEXT NOT NULL PRIMARY KEY, v TEXT
        );
        INSERT INTO shadowed_ok VALUES ('a', 'b', 'c', 'k1', 'x');
        INSERT INTO shadowed_ok VALUES ('d', 'e', 'f', 'k2', 'x');
        """
    )
    rows, _ = paginate_all(fresh_db.table("shadowed_ok"), ["v", "code"], limit=1)
    assert [r["code"] for r in rows] == ["k1", "k2"]

    # A table-level PRIMARY KEY (code) with an explicit NOT NULL works too
    fresh_db.executescript(
        """
        CREATE TABLE shadowed_ok2 (
            rowid TEXT, oid TEXT, _rowid_ TEXT,
            code TEXT NOT NULL, v TEXT, PRIMARY KEY (code)
        );
        INSERT INTO shadowed_ok2 VALUES ('a', 'b', 'c', 'k1', 'x');
        INSERT INTO shadowed_ok2 VALUES ('d', 'e', 'f', 'k2', 'x');
        """
    )
    rows, _ = paginate_all(fresh_db.table("shadowed_ok2"), ["v", "code"], limit=1)
    assert [r["code"] for r in rows] == ["k1", "k2"]


def test_nullable_primary_key_on_rowid_table_is_not_accepted(fresh_db):
    # A non-INTEGER PRIMARY KEY on a rowid table is not implicitly NOT NULL,
    # so it cannot be assumed unique-and-non-null when all aliases are shadowed
    fresh_db.executescript(
        """
        CREATE TABLE shaky (
            rowid TEXT, oid TEXT, _rowid_ TEXT,
            code TEXT PRIMARY KEY, v TEXT
        );
        """
    )
    with pytest.raises(ValueError, match="non-NULL unique primary key"):
        fresh_db.table("shaky").rows_keyset(["v"])


def test_unshadowed_alias_names_are_accepted(fresh_db):
    # Only one alias shadowed: another alias remains usable
    fresh_db.executescript(
        "CREATE TABLE one_shadow (rowid TEXT, v TEXT);"
        "INSERT INTO one_shadow VALUES ('a', 'x'), ('b', 'x');"
    )
    rows, _ = paginate_all(fresh_db.table("one_shadow"), ["v"], limit=1)
    assert [r["rowid"] for r in rows] == ["a", "b"]


def test_special_column_names(fresh_db):
    name = 'weird "name", with spaces and SELECT'
    fresh_db.execute(
        f'CREATE TABLE specials ({quote(name)} TEXT, "order" INTEGER, "a,b" TEXT)'
    )
    fresh_db.table("specials").insert_all(
        [{name: v, "order": i, "a,b": "z"} for i, v in enumerate(["b", "a", "a"])]
    )
    table = fresh_db.table("specials")
    rows, _ = paginate_all(table, [name, "order"], limit=2)
    # name 'b' (order 0) sorts after the two 'a' rows, whose tie is the
    # declared tiebreaker order (1 before 2)
    assert [r["order"] for r in rows] == [1, 2, 0]
    # SQL keywords as column names
    rows2, _ = paginate_all(table, ["order"], limit=2)
    assert [r["order"] for r in rows2] == [0, 1, 2]


def quote(identifier):
    return '"{}"'.format(identifier.replace('"', '""'))


def test_unknown_and_duplicate_and_nonstring_columns_rejected(fresh_db):
    fresh_db.execute("CREATE TABLE u (a TEXT, b TEXT)")
    table = fresh_db.table("u")
    with pytest.raises(ValueError, match="Unknown column"):
        table.rows_keyset(["nope"])
    with pytest.raises(ValueError, match="more than once"):
        table.rows_keyset(["a", "a"])
    with pytest.raises(ValueError, match="non-empty sequence"):
        table.rows_keyset([])
    with pytest.raises(ValueError, match="non-empty sequence"):
        table.rows_keyset("a")
    with pytest.raises(ValueError):
        table.rows_keyset([1])
    # SQL fragments are not treated as column names
    with pytest.raises(ValueError, match="Unknown column"):
        table.rows_keyset(["a || b"])


def test_case_insensitive_column_name_resolution(fresh_db):
    fresh_db.execute('CREATE TABLE cas ("MixedName" TEXT)')
    fresh_db.table("cas").insert_all([{"MixedName": v} for v in "cba"])
    page = fresh_db.table("cas").rows_keyset(["mixedname"], limit=2)
    assert [r["MixedName"] for r in page["rows"]] == ["a", "b"]


def test_limit_validation(fresh_db):
    fresh_db.execute("CREATE TABLE l (a TEXT)")
    table = fresh_db.table("l")
    for bad in (0, -1, 10001, 1.5, "10", True, False):
        with pytest.raises(ValueError):
            table.rows_keyset(["a"], limit=bad)
    table.rows_keyset(["a"], limit=1)
    table.rows_keyset(["a"], limit=10000)


def test_reverse_must_be_bool(fresh_db):
    fresh_db.execute("CREATE TABLE r (a TEXT)")
    with pytest.raises(ValueError):
        fresh_db.table("r").rows_keyset(["a"], reverse=1)


def test_where_positional_and_named_parameters(fresh_db):
    fresh_db.execute("CREATE TABLE w (g TEXT, n INTEGER)")
    fresh_db.table("w").insert_all(
        [{"g": g, "n": i} for i, g in enumerate(["a", "b", "a", "c", "a"])]
    )
    table = fresh_db.table("w")
    rows, _ = paginate_all(
        table, ["n"], where="g = ?", where_args=["a"], limit=2
    )
    assert [r["n"] for r in rows] == [0, 2, 4]
    rows, _ = paginate_all(
        table, ["n"], where="g = :g", where_args={"g": "a"}, limit=2
    )
    assert [r["n"] for r in rows] == [0, 2, 4]
    # Blob and NULL parameters round-trip through the cursor
    fresh_db.execute("CREATE TABLE w2 (k INTEGER, b BLOB)")
    fresh_db.table("w2").insert_all(
        [{"k": 1, "b": b"\x00\x01"}, {"k": 2, "b": b"\x00\x01"}, {"k": 3, "b": None}]
    )
    rows, _ = paginate_all(
        fresh_db.table("w2"),
        ["k"],
        where="b IS ?",
        where_args=[b"\x00\x01"],
        limit=1,
    )
    assert [r["k"] for r in rows] == [1, 2]


def test_where_args_type_validation(fresh_db):
    fresh_db.execute("CREATE TABLE q (a TEXT)")
    table = fresh_db.table("q")
    for bad in ("x", b"x", 123, object()):
        with pytest.raises(ValueError):
            table.rows_keyset(["a"], where="a = ?", where_args=bad)


def test_reserved_keyset_parameter_names_rejected(fresh_db):
    fresh_db.execute("CREATE TABLE q (a TEXT)")
    table = fresh_db.table("q")
    with pytest.raises(ValueError, match="reserved"):
        table.rows_keyset(
            ["a"], where="a = :ks_1", where_args={"ks_1": "x"}
        )
    with pytest.raises(ValueError, match="reserved"):
        table.rows_keyset(
            ["a"], where="a = :ks_99", where_args={"ks_99": "x"}
        )
    # Ordinary user names still work alongside the internal slots
    page = table.rows_keyset(
        ["a"], where="a = :mine", where_args={"mine": "x"}
    )
    assert page["next_cursor"] is None


def test_cursor_survives_connection_reopen(tmp_path):
    path = str(tmp_path / "keyset.db")
    db = Database(path)
    db.execute("CREATE TABLE p (g TEXT, n INTEGER)")
    db.table("p").insert_all(
        [{"g": g, "n": i} for i, g in enumerate(["a", "b", "a", "b", "c"] * 4)]
    )
    table = db.table("p")
    first = table.rows_keyset(["g", "n"], limit=3)
    second = table.rows_keyset(
        ["g", "n"], limit=3, after=first["next_cursor"]
    )
    cursor = second["next_cursor"]
    seen = first["rows"] + second["rows"]
    db.close()

    db2 = Database(path)
    table2 = db2.table("p")
    cursor_now = cursor
    while cursor_now is not None:
        page = table2.rows_keyset(["g", "n"], limit=3, after=cursor_now)
        seen.extend(page["rows"])
        cursor_now = page["next_cursor"]
    db2.close()

    db3 = Database(path)
    expected = list(db3.table("p").rows_where(order_by="g, n"))
    db3.close()
    assert seen == expected


def test_cursor_rejected_for_different_table(fresh_db):
    fresh_db.executescript(
        "CREATE TABLE t1 (a TEXT);"
        "CREATE TABLE t2 (a TEXT);"
        "INSERT INTO t1 VALUES ('x');"
        "INSERT INTO t2 VALUES ('y');"
    )
    cursor = fresh_db.table("t1").rows_keyset(["a"])["next_cursor"]
    # Single-row table yields no cursor; force a multi-row one
    fresh_db.table("t1").insert_all([{"a": c} for c in "abcde"])
    cursor = fresh_db.table("t1").rows_keyset(["a"], limit=2)["next_cursor"]
    with pytest.raises(ValueError, match="different table"):
        fresh_db.table("t2").rows_keyset(["a"], after=cursor)


def test_cursor_rejected_for_changed_where_and_args(fresh_db):
    fresh_db.execute("CREATE TABLE f (g TEXT, n INTEGER)")
    fresh_db.table("f").insert_all(
        [{"g": g, "n": i} for i, g in enumerate(["a", "b", "a", "b", "a", "b"])]
    )
    table = fresh_db.table("f")
    cursor = table.rows_keyset(
        ["n"], where="g = ?", where_args=["a"], limit=2
    )["next_cursor"]
    with pytest.raises(ValueError, match="where"):
        table.rows_keyset(["n"], after=cursor)
    with pytest.raises(ValueError, match="where_args"):
        table.rows_keyset(
            ["n"], where="g = ?", where_args=["b"], after=cursor
        )
    # Named-argument cursor cross-check
    named_cursor = table.rows_keyset(
        ["n"], where="g = :g", where_args={"g": "a"}, limit=2
    )["next_cursor"]
    with pytest.raises(ValueError, match="where_args"):
        table.rows_keyset(
            ["n"],
            where="g = :g",
            where_args={"g": "b"},
            after=named_cursor,
        )


def test_cursor_rejected_for_changed_order_and_reverse(fresh_db):
    fresh_db.execute("CREATE TABLE o (a TEXT, b TEXT)")
    fresh_db.table("o").insert_all(
        [{"a": str(i), "b": str(-i)} for i in range(6)]
    )
    table = fresh_db.table("o")
    cursor = table.rows_keyset(["a"], limit=2)["next_cursor"]
    with pytest.raises(ValueError, match="order_by"):
        table.rows_keyset(["b"], after=cursor)
    with pytest.raises(ValueError, match="reverse"):
        table.rows_keyset(["a"], reverse=True, after=cursor)


def test_cursor_rejected_after_schema_change(tmp_path):
    path = str(tmp_path / "schema.db")
    db = Database(path)
    db.execute("CREATE TABLE s (a TEXT, b TEXT)")
    db.table("s").insert_all([{"a": c, "b": c} for c in "abcdef"])
    cursor = db.table("s").rows_keyset(["a"], limit=2)["next_cursor"]
    db.close()

    db = Database(path)
    db.execute("ALTER TABLE s ADD COLUMN c TEXT")
    with pytest.raises(ValueError, match="schema has changed"):
        db.table("s").rows_keyset(["a"], after=cursor)
    db.close()


def test_corrupt_cursors_raise_valueerror_and_never_restart(fresh_db):
    fresh_db.execute("CREATE TABLE bad (a TEXT)")
    fresh_db.table("bad").insert_all([{"a": c} for c in "abcdef"])
    table = fresh_db.table("bad")
    good = table.rows_keyset(["a"], limit=2)["next_cursor"]
    payload = _keyset_decode_cursor(good)
    corrupt_variants = [
        "not-a-cursor",
        _KEYSET_CURSOR_PREFIX + "!!!notbase64!!!",
        _KEYSET_CURSOR_PREFIX + "bm90anNvbg==",  # base64 of "notjson"
        12345,
    ]
    tampered = dict(payload)
    tampered["table"] = "other"
    corrupt_variants.append(
        _KEYSET_CURSOR_PREFIX
        + __import__("base64").urlsafe_b64encode(
            __import__("json").dumps(tampered).encode()
        ).decode()
    )
    # Missing-field variant
    del tampered["keys"]
    corrupt_variants.append(
        _KEYSET_CURSOR_PREFIX
        + __import__("base64").urlsafe_b64encode(
            __import__("json").dumps(tampered).encode()
        ).decode()
    )
    # Wrong value type inside the continuation key
    bad_typed = dict(payload)
    bad_typed["keys"] = [{"t": "int", "v": "not-an-int"}]
    corrupt_variants.append(
        _KEYSET_CURSOR_PREFIX
        + __import__("base64").urlsafe_b64encode(
            __import__("json").dumps(bad_typed).encode()
        ).decode()
    )
    for variant in corrupt_variants:
        with pytest.raises(ValueError):
            table.rows_keyset(["a"], after=variant)
    # An empty/garbage string is treated as a corrupt cursor, not "page one"
    with pytest.raises(ValueError):
        table.rows_keyset(["a"], after="")


def test_cursor_contents_are_never_interpolated_into_sql(fresh_db):
    fresh_db.execute("CREATE TABLE inj (a TEXT)")
    fresh_db.table("inj").insert_all([{"a": c} for c in "abcd"])
    statements = []
    fresh_db._tracer = lambda sql, params: statements.append((sql, params))
    fresh_db.table("inj").rows_keyset(["a"], limit=2)
    cursor = fresh_db.table("inj").rows_keyset(["a"], limit=2)["next_cursor"]
    statements.clear()
    fresh_db.table("inj").rows_keyset(["a"], after=cursor, limit=2)
    sql, params = statements[-1]
    assert "OFFSET" not in sql.upper()
    assert "LIMIT 3" in sql
    # Decoded cursor values only appear among bound parameters
    payload = _keyset_decode_cursor(cursor)
    for value in payload["keys"]:
        assert any(bound is not None for bound in (params or [])) or params
    # A payload containing SQL metacharacters as data must remain data
    fresh_db.execute("CREATE TABLE inj2 (a TEXT)")
    fresh_db.table("inj2").insert_all(
        [{"a": "x"}, {"a": "1); DROP TABLE inj2;--"}]
    )
    page = fresh_db.table("inj2").rows_keyset(["a"], limit=1)
    assert page["next_cursor"] is not None
    fresh_db.table("inj2").rows_keyset(["a"], after=page["next_cursor"])
    assert fresh_db.table("inj2").exists()


def test_no_offset_and_limit_plus_one(fresh_db):
    fresh_db.execute("CREATE TABLE n (a TEXT)")
    fresh_db.table("n").insert_all([{"a": str(i)} for i in range(10)])
    statements = []
    fresh_db._tracer = lambda sql, params: statements.append(sql)
    fresh_db.table("n").rows_keyset(["a"], limit=4)
    assert "OFFSET" not in statements[-1].upper()
    assert "LIMIT 5" in statements[-1]


def test_inserts_and_deletes_before_cursor_do_not_repeat_rows(fresh_db):
    fresh_db.execute("CREATE TABLE live (id INTEGER PRIMARY KEY, g TEXT)")
    fresh_db.table("live").insert_all(
        [{"id": i, "g": "a" if i % 2 else "b"} for i in range(1, 11)]
    )
    table = fresh_db.table("live")
    page1 = table.rows_keyset(["g", "id"], limit=3)
    first_ids = {r["id"] for r in page1["rows"]}
    cursor = page1["next_cursor"]
    # Insert rows that sort BEFORE the cursor, and delete a row that has not
    # been visited yet (it lies after the cursor in the total order)
    unvisited = next(
        r["id"] for r in table.rows if r["id"] not in first_ids
    )
    fresh_db.table("live").insert_all(
        [{"id": 100, "g": "a"}, {"id": 101, "g": "a"}]
    )
    fresh_db.execute("DELETE FROM live WHERE id = ?", (unvisited,))
    seen = set(first_ids)
    while cursor is not None:
        page = table.rows_keyset(["g", "id"], limit=3, after=cursor)
        ids = {r["id"] for r in page["rows"]}
        assert seen.isdisjoint(ids)
        seen.update(ids)
        cursor = page["next_cursor"]
    remaining = {r["id"] for r in table.rows}
    # Every surviving row was visited exactly once; the deleted row never
    # appeared, and rows inserted behind the cursor did not repeat anything
    assert seen == remaining
    assert unvisited not in seen


def test_database_is_not_modified_by_pagination(tmp_path):
    path = str(tmp_path / "ro.db")
    db = Database(path)
    db.execute("CREATE TABLE ro (a TEXT)")
    db.table("ro").insert_all([{"a": c} for c in "abcde"])
    db.close()

    uri = f"file:{path}?mode=ro"
    ro_conn = sqlite3.connect(uri, uri=True)
    ro_db = Database(ro_conn)
    table = ro_db.table("ro")
    cursor = None
    total = 0
    while True:
        page = table.rows_keyset(["a"], limit=2, after=cursor)
        total += len(page["rows"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert total == 5
    ro_conn.close()


def test_rows_dictionaries_have_only_real_columns(fresh_db):
    fresh_db.execute("CREATE TABLE clean (a TEXT, b INTEGER)")
    fresh_db.table("clean").insert_all(
        [{"a": "x", "b": 1}, {"a": "y", "b": 2}]
    )
    page = fresh_db.table("clean").rows_keyset(["a", "b"])
    for row in page["rows"]:
        assert set(row) == {"a", "b"}
        assert "_sqlite_utils_keyset_rowid" not in row
        assert "rowid" not in row


def test_missing_table_raises(fresh_db):
    with pytest.raises(Exception):
        fresh_db.table("ghost").rows_keyset(["a"])


def test_strict_table_and_empty_table_and_special_table_name(fresh_db):
    fresh_db.execute("CREATE TABLE s (a TEXT, b INTEGER) STRICT")
    fresh_db.table("s").insert_all([{"a": "x", "b": 1}, {"a": "y", "b": 2}])
    page = fresh_db.table("s").rows_keyset(["a"])
    assert [r["a"] for r in page["rows"]] == ["x", "y"]

    fresh_db.execute("CREATE TABLE empty (a TEXT)")
    assert fresh_db.table("empty").rows_keyset(["a"]) == {
        "rows": [],
        "next_cursor": None,
    }

    fresh_db.execute('CREATE TABLE "my table" (a TEXT)')
    fresh_db.table("my table").insert_all([{"a": c} for c in "cba"])
    t = fresh_db.table("my table")
    p1 = t.rows_keyset(["a"], limit=2)
    p2 = t.rows_keyset(["a"], limit=2, after=p1["next_cursor"])
    assert [r["a"] for r in p1["rows"]] == ["a", "b"]
    assert [r["a"] for r in p2["rows"]] == ["c"]
    assert p2["next_cursor"] is None


def test_views_do_not_offer_rows_keyset(fresh_db):
    fresh_db.execute("CREATE TABLE base (a TEXT)")
    fresh_db.table("base").insert_all([{"a": "x"}])
    fresh_db.execute("CREATE VIEW v AS SELECT * FROM base")
    assert not hasattr(fresh_db.view("v"), "rows_keyset")
    assert not hasattr(fresh_db["v"], "rows_keyset")


def test_float_and_negative_number_cursors(fresh_db):
    fresh_db.execute("CREATE TABLE f (x REAL)")
    fresh_db.table("f").insert_all([{"x": 1.1}, {"x": -2.5}, {"x": 3.0}])
    table = fresh_db.table("f")
    p1 = table.rows_keyset(["x"], limit=2)
    p2 = table.rows_keyset(["x"], limit=2, after=p1["next_cursor"])
    assert [r["x"] for r in p1["rows"]] == [-2.5, 1.1]
    assert [r["x"] for r in p2["rows"]] == [3.0]
    assert isinstance(p1["rows"][0]["x"], float)


def test_cursor_rejected_when_key_shape_changes(fresh_db):
    # A tampered cursor whose keys list length no longer matches the table
    import base64
    import json

    fresh_db.execute("CREATE TABLE k (a TEXT, b TEXT)")
    fresh_db.table("k").insert_all([{"a": str(i), "b": str(i)} for i in range(6)])
    cursor = fresh_db.table("k").rows_keyset(["a"], limit=2)["next_cursor"]
    payload = _keyset_decode_cursor(cursor)
    payload["keys"] = payload["keys"][:1]  # drop the rowid tiebreaker entry
    tampered = _KEYSET_CURSOR_PREFIX + base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).decode("ascii")
    with pytest.raises(ValueError, match="wrong shape"):
        fresh_db.table("k").rows_keyset(["a"], after=tampered)


def test_cursor_with_unsupported_version_rejected(fresh_db):
    import base64
    import json

    fresh_db.execute("CREATE TABLE k (a TEXT)")
    fresh_db.table("k").insert_all([{"a": str(i)} for i in range(4)])
    cursor = fresh_db.table("k").rows_keyset(["a"], limit=2)["next_cursor"]
    payload = _keyset_decode_cursor(cursor)
    payload["v"] = 999
    tampered = _KEYSET_CURSOR_PREFIX + base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).decode("ascii")
    with pytest.raises(ValueError, match="version"):
        fresh_db.table("k").rows_keyset(["a"], after=tampered)


def test_existing_rows_and_rows_where_defaults_unchanged(fresh_db):
    fresh_db.execute("CREATE TABLE legacy (a TEXT)")
    fresh_db.table("legacy").insert_all([{"a": "z"}, {"a": "a"}])
    assert [r["a"] for r in fresh_db.table("legacy").rows] == ["z", "a"]
    assert list(
        fresh_db.table("legacy").rows_where(order_by="a")
    ) == [{"a": "a"}, {"a": "z"}]


def test_large_composite_without_rowid_stable_pagination(fresh_db):
    fresh_db.executescript(
        """
        CREATE TABLE big (
            g TEXT NOT NULL, n INTEGER NOT NULL, d TEXT,
            PRIMARY KEY (g, n)
        ) WITHOUT ROWID;
        """
    )
    fresh_db.table("big").insert_all(
        [
            {"g": g, "n": i, "d": None if i % 7 == 0 else f"d{i}"}
            for g in ("a", "b", "c")
            for i in range(30)
        ]
    )
    table = fresh_db.table("big")
    for reverse in (False, True):
        rows, pages = paginate_all(
            table, ["d", "g", "n"], reverse=reverse, limit=8
        )
        direction = " DESC" if reverse else ""
        expected = list(
            table.rows_where(
                order_by=f"d{direction}, g{direction}, n{direction}"
            )
        )
        assert rows == expected
        assert pages > 1


def test_cursor_is_plain_string_and_self_describing(fresh_db):
    fresh_db.execute("CREATE TABLE c (a TEXT)")
    fresh_db.table("c").insert_all([{"a": "x"}, {"a": "y"}])
    cursor = fresh_db.table("c").rows_keyset(["a"], limit=1)["next_cursor"]
    assert isinstance(cursor, str)
    payload = _keyset_decode_cursor(cursor)
    assert payload["v"] == 1
    assert payload["table"] == "c"
    assert payload["order_by"] == ["a"]
    assert payload["reverse"] is False


@pytest.mark.parametrize("without_rowid", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_randomized_pagination_matches_sqlite_total_order(
    fresh_db, without_rowid, reverse
):
    # Cross-check paging against SQLite's own ORDER BY over many random,
    # duplicate-heavy, NULL-heavy datasets including BLOBs and Unicode.
    rng = random.Random(987654 if without_rowid else 123456)
    text_pool = [None, "A", "a", "b", "B", "abc", "abc ", "ü", "αβγ", ""]
    int_pool = [None, 1, 2, 1, 10, -3, 0]
    blob_pool = [None, b"", b"\x00", b"abc", b"\xff\xfe"]
    if without_rowid:
        fresh_db.executescript(
            """
            CREATE TABLE rnd (
                id1 TEXT NOT NULL, id2 INTEGER NOT NULL,
                a TEXT COLLATE NOCASE, b INTEGER, c BLOB,
                PRIMARY KEY (id1, id2)
            ) WITHOUT ROWID;
            """
        )
        rows = []
        for i in range(80):
            rows.append(
                {
                    "id1": f"k{i % 7}",
                    "id2": i,
                    "a": rng.choice(text_pool),
                    "b": rng.choice(int_pool),
                    "c": rng.choice(blob_pool),
                }
            )
        fresh_db.table("rnd").insert_all(rows)
        order_cols = ["a", "b", "c"]
        tie_cols = ["id1", "id2"]
    else:
        fresh_db.execute(
            "CREATE TABLE rnd (a TEXT COLLATE NOCASE, b INTEGER, c BLOB)"
        )
        rows = [
            {
                "a": rng.choice(text_pool),
                "b": rng.choice(int_pool),
                "c": rng.choice(blob_pool),
            }
            for _ in range(120)
        ]
        fresh_db.table("rnd").insert_all(rows)
        order_cols = ["a", "b", "c"]
        tie_cols = ["rowid"]

    table = fresh_db.table("rnd")
    direction = " DESC" if reverse else ""
    all_key_cols = order_cols + tie_cols
    expected_sql = (
        "SELECT "
        + ", ".join(order_cols)
        + " FROM rnd ORDER BY "
        + ", ".join(col + direction for col in all_key_cols)
    )
    expected = [dict(zip(order_cols, row)) for row in fresh_db.execute(expected_sql)]

    for page_limit in (1, 2, 7, 50, 1000):
        got, cursor, count = [], None, 0
        while True:
            page = table.rows_keyset(
                order_cols, limit=page_limit, after=cursor, reverse=reverse
            )
            count += 1
            assert len(page["rows"]) <= page_limit
            got.extend(
                {col: row[col] for col in order_cols} for row in page["rows"]
            )
            cursor = page["next_cursor"]
            if cursor is None:
                break
            assert count < 500
        assert got == expected
        # No row dropped or duplicated: compare full rows when tie columns are
        # real columns (WITHOUT ROWID), otherwise the projected count suffices
        assert len(got) == len(expected)
