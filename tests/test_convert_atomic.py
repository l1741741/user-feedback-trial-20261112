"""Regression tests for Table.convert(..., atomic=True) and the CLI --atomic flag."""

import pathlib

import pytest
from click.testing import CliRunner

import sqlite_utils
from sqlite_utils import cli
from sqlite_utils.db import BadMultiValues
from sqlite_utils.utils import sqlite3

ROWS = [{"id": i, "name": f"name-{i}"} for i in range(1, 7)]


def make_table(db):
    db.table("t").insert_all(ROWS, pk="id")
    return db.table("t")


def fail_on(row_value):
    def fn(value):
        if value == row_value:
            raise ValueError(f"boom on {value}")
        return value.upper()

    return fn


def test_atomic_convert_failure_on_late_row_restores_table(fresh_db):
    # fn fails partway through the rows: every already-converted row, plus
    # the temporary output column, must be restored to the pre-call state
    table = make_table(fresh_db)
    schema_before = table.schema
    with pytest.raises(sqlite3.OperationalError):
        table.convert("name", fail_on("name-4"), output="upper_name", atomic=True)
    assert list(table.rows) == ROWS
    assert table.schema == schema_before
    assert "upper_name" not in table.columns_dict
    # The connection is left as it was - no dangling transaction
    assert not fresh_db.conn.in_transaction


def test_convert_default_atomic_false_keeps_partial_schema_change(fresh_db):
    # Documents the pre-existing default behavior: the UPDATE itself is
    # transactional, but the output column added beforehand stays
    table = make_table(fresh_db)
    with pytest.raises(sqlite3.OperationalError):
        table.convert("name", fail_on("name-4"), output="upper_name")
    assert "upper_name" in table.columns_dict
    assert [row["name"] for row in table.rows] == [row["name"] for row in ROWS]


def test_atomic_convert_multi_failure_restores_dynamic_columns(fresh_db):
    # multi=True: columns created for keys of the returned dictionaries and
    # the drop of the original column must all be rolled back on failure
    table = make_table(fresh_db)
    schema_before = table.schema

    def fn(value):
        if value == "name-4":
            raise ValueError("boom")
        return {"upper": value.upper(), "name_length": len(value)}

    with pytest.raises(ValueError):
        table.convert("name", fn, multi=True, drop=True, atomic=True)
    assert list(table.rows) == ROWS
    assert table.schema == schema_before
    assert "name" in table.columns_dict
    assert "upper" not in table.columns_dict
    assert "name_length" not in table.columns_dict


def test_atomic_convert_multi_failure_during_writes_restores_rows(fresh_db):
    # The failing row is only hit in the write phase, after earlier rows
    # have already been updated - those writes must be rolled back too
    table = make_table(fresh_db)

    def fn(value):
        if value == "name-4":
            # A set cannot be bound by sqlite3 - the write for this row fails
            return {"upper": {1, 2}}
        return {"upper": value.upper()}

    with pytest.raises(sqlite3.ProgrammingError):
        table.convert("name", fn, multi=True, atomic=True)
    assert list(table.rows) == ROWS
    assert "upper" not in table.columns_dict


def test_atomic_convert_unique_constraint_failure_restores_table(fresh_db):
    # A SQLite constraint failure triggered by the converted values also
    # restores everything, including index state
    fresh_db.table("t").insert_all(
        [{"id": 1, "code": "a"}, {"id": 2, "code": "b"}], pk="id"
    )
    fresh_db.execute("create unique index idx_code on t(code)")
    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.table("t").convert("code", lambda v: "same", atomic=True)
    assert list(fresh_db.table("t").rows) == [
        {"id": 1, "code": "a"},
        {"id": 2, "code": "b"},
    ]
    assert "idx_code" in [index.name for index in fresh_db.table("t").indexes]


def test_atomic_convert_failure_restores_indexes_and_triggers(fresh_db):
    table = make_table(fresh_db)
    fresh_db.execute("create index idx_name on t(name)")
    fresh_db.execute(
        "create trigger t_touch after update on t begin "
        "update t set id = new.id where id = new.id; end"
    )
    with pytest.raises(sqlite3.OperationalError):
        table.convert(
            "name", fail_on("name-4"), output="upper_name", drop=True, atomic=True
        )
    assert "idx_name" in [index.name for index in table.indexes]
    assert [trigger.name for trigger in table.triggers] == ["t_touch"]
    assert list(table.rows) == ROWS


def test_atomic_convert_success_inside_caller_transaction_is_not_committed(
    fresh_db,
):
    # A successful atomic convert must not commit the caller's transaction
    table = make_table(fresh_db)
    fresh_db.begin()
    fresh_db.execute("insert into t (id, name) values (100, 'pending')")
    table.convert("name", lambda v: v.upper(), atomic=True)
    assert fresh_db.conn.in_transaction
    fresh_db.rollback()
    # Both the caller's pending insert and the conversion are gone
    assert list(table.rows) == ROWS
    # And again, this time committing - both changes persist
    fresh_db.begin()
    fresh_db.execute("insert into t (id, name) values (100, 'pending')")
    table.convert("name", lambda v: v.upper(), atomic=True)
    assert fresh_db.conn.in_transaction
    fresh_db.commit()
    assert list(table.rows) == [
        *[{"id": i, "name": f"NAME-{i}"} for i in range(1, 7)],
        {"id": 100, "name": "PENDING"},
    ]


def test_atomic_convert_failure_inside_caller_transaction_rolls_back_only_convert(
    fresh_db,
):
    # The caller's earlier pending operations survive a failed atomic convert
    table = make_table(fresh_db)
    fresh_db.begin()
    fresh_db.execute("insert into t (id, name) values (100, 'pending')")
    with pytest.raises(sqlite3.OperationalError):
        table.convert("name", fail_on("name-4"), output="upper_name", atomic=True)
    # The caller's transaction is still open and its work is intact
    assert fresh_db.conn.in_transaction
    fresh_db.commit()
    assert list(table.rows) == [*ROWS, {"id": 100, "name": "pending"}]
    assert "upper_name" not in table.columns_dict


def test_atomic_convert_inside_atomic_block(fresh_db):
    table = make_table(fresh_db)
    with fresh_db.atomic():
        fresh_db.table("t").insert({"id": 100, "name": "pending"})
        table.convert("name", lambda v: v.upper(), atomic=True)
        assert fresh_db.conn.in_transaction
    assert table.get(100)["name"] == "PENDING"
    assert table.get(1)["name"] == "NAME-1"


@pytest.mark.parametrize(
    "where,where_args",
    (("id > ?", [3]), ("id > :min_id", {"min_id": 3})),
)
def test_atomic_convert_where_success(fresh_db, where, where_args):
    table = make_table(fresh_db)
    table.convert(
        "name", lambda v: v.upper(), where=where, where_args=where_args, atomic=True
    )
    assert list(table.rows) == [
        *ROWS[:3],
        *[{"id": i, "name": f"NAME-{i}"} for i in range(4, 7)],
    ]


def test_atomic_convert_where_failure_restores_filtered_rows(fresh_db):
    table = make_table(fresh_db)
    with pytest.raises(sqlite3.OperationalError):
        table.convert(
            "name",
            fail_on("name-5"),
            where="id > ?",
            where_args=[3],
            atomic=True,
        )
    assert list(table.rows) == ROWS


def test_atomic_convert_where_args_are_bound_not_interpolated(fresh_db):
    # A where_args value that looks like SQL must be treated as a value
    table = make_table(fresh_db)
    table.convert(
        "name",
        lambda v: v.upper(),
        where="name = ?",
        where_args=["name-1' or 1=1 --"],
        atomic=True,
    )
    # No row matched the literal value, so nothing was converted
    assert list(table.rows) == ROWS


def test_atomic_convert_quoted_column_names(fresh_db):
    fresh_db.table("t").insert({"id": 1, "my column": "mixed case"}, pk="id")
    fresh_db.table("t").convert(
        "my column", lambda v: v.upper(), output="output column", atomic=True
    )
    assert list(fresh_db.table("t").rows) == [
        {"id": 1, "my column": "mixed case", "output column": "MIXED CASE"}
    ]
    # Failure restores the added quoted output column too
    with pytest.raises(sqlite3.OperationalError):
        fresh_db.table("t").convert(
            "my column", fail_on("mixed case"), output="second column", atomic=True
        )
    assert "second column" not in fresh_db.table("t").columns_dict
    assert list(fresh_db.table("t").rows) == [
        {"id": 1, "my column": "mixed case", "output column": "MIXED CASE"}
    ]


def test_atomic_convert_none_result_is_not_a_failure(fresh_db):
    table = fresh_db.table("t")
    table.insert_all([{"id": 1, "name": "keep"}, {"id": 2, "name": "drop"}], pk="id")
    table.convert("name", lambda v: None if v == "drop" else v.upper(), atomic=True)
    assert list(table.rows) == [
        {"id": 1, "name": "KEEP"},
        {"id": 2, "name": None},
    ]


def test_atomic_convert_empty_table_succeeds(fresh_db):
    fresh_db.table("t").create({"id": int, "name": str}, pk="id")
    fresh_db.table("t").convert("name", lambda v: v.upper(), atomic=True)
    fresh_db.table("t").convert(
        "name", lambda v: {"upper": v.upper()}, multi=True, atomic=True
    )
    assert list(fresh_db.table("t").rows) == []


def test_atomic_convert_multi_bad_return_type_restores_table(fresh_db):
    table = make_table(fresh_db)
    with pytest.raises(BadMultiValues):
        table.convert("name", lambda v: v.upper(), multi=True, atomic=True)
    assert list(table.rows) == ROWS


def test_atomic_convert_restores_foreign_keys_pragma(fresh_db):
    fresh_db.conn.execute("PRAGMA foreign_keys=ON")
    table = make_table(fresh_db)
    table.convert("name", lambda v: v.upper(), output="upper", drop=True, atomic=True)
    assert list(table.rows) == [{"id": i, "upper": f"NAME-{i}"} for i in range(1, 7)]
    assert fresh_db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert fresh_db.conn.execute("PRAGMA defer_foreign_keys").fetchone()[0] == 0
    assert not fresh_db.conn.in_transaction


def test_atomic_convert_failure_restores_foreign_keys_pragma(fresh_db):
    fresh_db.conn.execute("PRAGMA foreign_keys=ON")
    table = make_table(fresh_db)
    with pytest.raises(sqlite3.OperationalError):
        table.convert("name", fail_on("name-4"), output="upper", drop=True, atomic=True)
    assert list(table.rows) == ROWS
    assert fresh_db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert fresh_db.conn.execute("PRAGMA defer_foreign_keys").fetchone()[0] == 0
    assert not fresh_db.conn.in_transaction


def test_atomic_convert_fn_is_evaluated_by_sql_once_per_row(fresh_db):
    # Public contract: fn is registered as a SQL function and SQLite
    # evaluates it during the UPDATE. convert() must not pre-apply fn to
    # the whole table in Python and then write a second time, which would
    # double any side effects of fn.
    table = make_table(fresh_db)
    calls = []

    def fn(value):
        calls.append(value)
        return value.upper()

    table.convert("name", fn, atomic=True)
    assert sorted(calls) == sorted(row["name"] for row in ROWS)
    assert table.get(1)["name"] == "NAME-1"


def test_atomic_convert_multi_evaluates_fn_once_per_row(fresh_db):
    table = make_table(fresh_db)
    calls = []

    def fn(value):
        calls.append(value)
        return {"upper": value.upper()}

    table.convert("name", fn, multi=True, atomic=True)
    assert sorted(calls) == sorted(row["name"] for row in ROWS)
    assert table.get(1)["upper"] == "NAME-1"


@pytest.fixture
def cli_db_path(tmpdir):
    db_path = str(pathlib.Path(tmpdir) / "data.db")
    db = sqlite_utils.Database(db_path)
    db.table("t").insert_all(ROWS, pk="id")
    db.close()
    return db_path


def test_cli_convert_atomic_success(cli_db_path):
    result = CliRunner().invoke(
        cli.cli,
        [
            "convert",
            cli_db_path,
            "t",
            "name",
            "return value.upper()",
            "--output",
            "upper_name",
            "--atomic",
        ],
    )
    assert result.exit_code == 0, result.output
    db = sqlite_utils.Database(cli_db_path)
    assert list(db.table("t").rows) == [
        {"id": i, "name": f"name-{i}", "upper_name": f"NAME-{i}"} for i in range(1, 7)
    ]


FAILING_CODE = (
    "def convert(value):\n"
    "    if value == 'name-4':\n"
    "        raise ValueError('boom')\n"
    "    return value.upper()"
)


def test_cli_convert_atomic_failure_restores_table(cli_db_path):
    result = CliRunner().invoke(
        cli.cli,
        [
            "convert",
            cli_db_path,
            "t",
            "name",
            FAILING_CODE,
            "--output",
            "upper_name",
            "--atomic",
        ],
    )
    assert result.exit_code == 1
    assert "Error: user-defined function raised exception" in result.output
    db = sqlite_utils.Database(cli_db_path)
    assert list(db.table("t").rows) == ROWS
    assert "upper_name" not in db.table("t").columns_dict


def test_cli_convert_without_atomic_keeps_partial_schema_change(cli_db_path):
    # Same failure without --atomic: the added output column is left behind,
    # demonstrating what --atomic protects against
    result = CliRunner().invoke(
        cli.cli,
        ["convert", cli_db_path, "t", "name", FAILING_CODE, "--output", "upper_name"],
    )
    assert result.exit_code == 1
    db = sqlite_utils.Database(cli_db_path)
    assert "upper_name" in db.table("t").columns_dict
    assert [row["name"] for row in db.table("t").rows] == [row["name"] for row in ROWS]


def test_cli_convert_atomic_multi_failure_restores_table(cli_db_path):
    result = CliRunner().invoke(
        cli.cli,
        [
            "convert",
            cli_db_path,
            "t",
            "name",
            "def convert(value):\n"
            "    if value == 'name-4':\n"
            "        raise ValueError('boom')\n"
            "    return {'upper': value.upper()}",
            "--multi",
            "--drop",
            "--atomic",
        ],
    )
    assert result.exit_code == 1
    assert "Error: ValueError: boom" in result.output
    db = sqlite_utils.Database(cli_db_path)
    assert list(db.table("t").rows) == ROWS
    assert "upper" not in db.table("t").columns_dict
    assert "name" in db.table("t").columns_dict
