"""Regression tests for ``transform(preserve_schema=True)``.

These cover the token-aware rewriting of indexes, triggers and views that
depend on a table while it is rebuilt, plus the failure/rollback guarantees
and the CLI ``--preserve-schema`` flag.
"""

import sqlite3

import pytest

from sqlite_utils.db import TransformError


@pytest.fixture
def dogs_db(fresh_db):
    """A database containing a populated ``dogs`` table."""
    fresh_db.table("dogs").create(
        {"id": int, "name": str, "age": int, "owner_id": int}, pk="id"
    )
    return fresh_db


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------


def test_preserve_schema_plain_index_renamed(fresh_db):
    table = fresh_db.table("dogs")
    table.insert({"id": 1, "name": "Cleo"}, pk="id")
    table.create_index(["name"], index_name="idx_name")

    table.transform(rename={"name": "full_name"}, preserve_schema=True)

    assert [(index.name, index.unique, index.columns) for index in table.indexes] == [
        ("idx_name", 0, ["full_name"])
    ]


def test_preserve_schema_expression_index_renamed(dogs_db):
    dogs_db.execute("CREATE INDEX idx_expr ON dogs(lower(name))")
    dogs_db.table("dogs").insert({"id": 1, "name": "Cleo"})

    dogs_db.table("dogs").transform(
        rename={"name": "full_name"}, preserve_schema=True
    )

    sql = dogs_db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'idx_expr'"
    ).fetchone()[0]
    assert sql == "CREATE INDEX idx_expr ON dogs(lower(full_name))"
    # The index is actually used / valid against the new column.
    dogs_db.execute("ANALYZE")
    assert dogs_db.execute(
        "SELECT full_name FROM dogs WHERE lower(full_name) = 'cleo'"
    ).fetchall() == [("Cleo",)]


def test_preserve_schema_partial_index_renamed(dogs_db):
    dogs_db.execute(
        "CREATE INDEX idx_partial ON dogs(name) WHERE name IS NOT NULL"
    )
    dogs_db.table("dogs").insert({"id": 1, "name": "Cleo"})

    dogs_db.table("dogs").transform(
        rename={"name": "full_name"}, preserve_schema=True
    )

    sql = dogs_db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'idx_partial'"
    ).fetchone()[0]
    assert sql == (
        "CREATE INDEX idx_partial ON dogs(full_name) "
        "WHERE full_name IS NOT NULL"
    )


def test_preserve_schema_index_uniqueness_preserved(dogs_db):
    dogs_db.execute("CREATE UNIQUE INDEX idx_uq ON dogs(name)")
    dogs_db.table("dogs").insert({"id": 1, "name": "Cleo"})

    dogs_db.table("dogs").transform(
        rename={"name": "full_name"}, preserve_schema=True
    )

    index = next(
        index
        for index in dogs_db.table("dogs").indexes
        if index.name == "idx_uq"
    )
    assert index.unique == 1
    # The unique constraint is enforced on the renamed column.
    with pytest.raises(sqlite3.IntegrityError):
        dogs_db.execute(
            'INSERT INTO dogs (id, full_name) VALUES (3, "Cleo")'
        )


def test_preserve_schema_index_drop_column_raises_named_error(dogs_db):
    dogs_db.execute("CREATE INDEX idx_name ON dogs(name)")
    dogs_db.table("dogs").insert({"id": 1, "name": "Cleo"})

    with pytest.raises(TransformError, match="idx_name"):
        dogs_db.table("dogs").transform(drop={"name"}, preserve_schema=True)


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


def test_preserve_schema_trigger_old_new_references(fresh_db):
    fresh_db.table("dogs").create({"id": int, "name": str}, pk="id")
    fresh_db.executescript(
        """
        CREATE TABLE log (id INTEGER PRIMARY KEY, what TEXT);
        CREATE TRIGGER trg_dogs AFTER UPDATE OF name ON dogs
        FOR EACH ROW WHEN OLD.name != NEW.name
        BEGIN
            INSERT INTO log(what) VALUES (OLD.name || '->' || NEW.name);
        END;
        """
    )
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"})

    fresh_db.table("dogs").transform(
        rename={"name": "full_name"}, preserve_schema=True
    )

    sql = fresh_db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'trg_dogs'"
    ).fetchone()[0]
    assert "UPDATE OF full_name" in sql
    assert "OLD.full_name" in sql
    assert "NEW.full_name" in sql
    # Trigger still fires and writes to the side table.
    fresh_db.execute("UPDATE dogs SET full_name = 'Pancakes' WHERE id = 1")
    assert list(fresh_db.table("log").rows) == [
        {"id": 1, "what": "Cleo->Pancakes"}
    ]


def test_preserve_schema_trigger_cross_table_same_named_column(fresh_db):
    # A same-named column on another table referenced by the trigger must
    # not be renamed; only the OLD/NEW references to the transformed table.
    fresh_db.table("dogs").create({"id": int, "name": str}, pk="id")
    fresh_db.executescript(
        """
        CREATE TABLE other (id INTEGER PRIMARY KEY, name TEXT, dog_id INTEGER);
        CREATE TRIGGER trg_sync AFTER UPDATE OF name ON dogs
        FOR EACH ROW
        BEGIN
            UPDATE other SET name = NEW.name WHERE dog_id = NEW.id;
        END;
        """
    )
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"})
    fresh_db.table("other").insert(
        {"id": 9, "name": "old", "dog_id": 1}, pk="id"
    )

    fresh_db.table("dogs").transform(
        rename={"name": "full_name"}, preserve_schema=True
    )

    sql = fresh_db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'trg_sync'"
    ).fetchone()[0]
    assert "SET name = NEW.full_name" in sql
    fresh_db.execute("UPDATE dogs SET full_name = 'Pancakes' WHERE id = 1")
    assert fresh_db.table("other").get(9)["name"] == "Pancakes"


def test_preserve_schema_trigger_drop_column_raises_named_error(fresh_db):
    fresh_db.table("dogs").create({"id": int, "name": str}, pk="id")
    fresh_db.executescript(
        """
        CREATE TRIGGER trg_dogs AFTER UPDATE OF name ON dogs
        FOR EACH ROW WHEN OLD.name IS NOT NULL
        BEGIN SELECT 1; END;
        """
    )
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"})

    with pytest.raises(TransformError, match="trg_dogs"):
        fresh_db.table("dogs").transform(drop={"name"}, preserve_schema=True)

    assert "trg_dogs" in fresh_db.triggers_dict


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def test_preserve_schema_view_renamed_column(fresh_db):
    fresh_db.table("dogs").create({"id": int, "name": str}, pk="id")
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"})
    fresh_db.execute("CREATE VIEW dogs_view AS SELECT id, name FROM dogs")

    fresh_db.table("dogs").transform(
        rename={"name": "title"}, preserve_schema=True
    )

    sql = fresh_db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'dogs_view'"
    ).fetchone()[0]
    assert "title" in sql
    assert list(fresh_db.view("dogs_view").rows) == [{"id": 1, "title": "Cleo"}]


def test_preserve_schema_view_alias_and_join(fresh_db):
    fresh_db.table("owners").insert({"id": 7, "name": "Alice"}, pk="id")
    fresh_db.table("dogs").insert(
        {"id": 1, "name": "Cleo", "owner_id": 7}, pk="id"
    )
    fresh_db.execute(
        """
        CREATE VIEW dog_owner AS
        SELECT d.name AS dog, o.name AS owner
        FROM dogs d JOIN owners o ON d.owner_id = o.id
        WHERE d.name IS NOT NULL
        """
    )

    fresh_db.table("dogs").transform(
        rename={"name": "full_name"}, preserve_schema=True
    )

    sql = fresh_db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'dog_owner'"
    ).fetchone()[0]
    assert "d.full_name AS dog" in sql
    # The owner table's same-named column is untouched.
    assert "o.name AS owner" in sql
    assert list(fresh_db.view("dog_owner").rows) == [
        {"dog": "Cleo", "owner": "Alice"}
    ]


def test_preserve_schema_view_string_literal_unchanged(fresh_db):
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"}, pk="id")
    fresh_db.execute(
        "CREATE VIEW v AS SELECT id, name, 'the name value' AS label "
        "FROM dogs WHERE name != 'name'"
    )

    fresh_db.table("dogs").transform(
        rename={"name": "full_name"}, preserve_schema=True
    )

    sql = fresh_db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'v'"
    ).fetchone()[0]
    assert "'the name value'" in sql
    assert "!= 'name'" in sql
    assert "full_name" in sql


def test_preserve_schema_view_comment_unchanged(fresh_db):
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"}, pk="id")
    fresh_db.execute(
        "CREATE VIEW v AS SELECT id, name /* the name column */ FROM dogs"
    )

    fresh_db.table("dogs").transform(
        rename={"name": "full_name"}, preserve_schema=True
    )

    sql = fresh_db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'v'"
    ).fetchone()[0]
    assert "/* the name column */" in sql
    assert "full_name" in sql


def test_preserve_schema_view_star_expands_keeping_output_names(fresh_db):
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"}, pk="id")
    fresh_db.execute("CREATE VIEW star_view AS SELECT * FROM dogs")

    fresh_db.table("dogs").transform(
        rename={"name": "full_name"}, preserve_schema=True
    )

    # The view must keep exposing a column called "name".
    assert [column.name for column in fresh_db.view("star_view").columns] == [
        "id",
        "name",
    ]
    assert list(fresh_db.view("star_view").rows) == [
        {"id": 1, "name": "Cleo"}
    ]


def test_preserve_schema_view_drop_column_raises_named_error(fresh_db):
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"}, pk="id")
    fresh_db.execute("CREATE VIEW dogs_view AS SELECT id, name FROM dogs")

    with pytest.raises(TransformError, match="dogs_view"):
        fresh_db.table("dogs").transform(drop={"name"}, preserve_schema=True)

    assert "dogs_view" in fresh_db.view_names()
    assert list(fresh_db.view("dogs_view").rows) == [{"id": 1, "name": "Cleo"}]


def test_preserve_schema_unrelated_view_left_untouched(fresh_db):
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"}, pk="id")
    fresh_db.table("cats").insert({"id": 1, "name": "Tom"}, pk="id")
    fresh_db.execute("CREATE VIEW cats_view AS SELECT id, name FROM cats")
    before = fresh_db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'cats_view'"
    ).fetchone()[0]

    fresh_db.table("dogs").transform(
        rename={"name": "full_name"}, preserve_schema=True
    )

    after = fresh_db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'cats_view'"
    ).fetchone()[0]
    assert before == after


# ---------------------------------------------------------------------------
# Quoted identifiers
# ---------------------------------------------------------------------------


def test_preserve_schema_quoted_identifiers(fresh_db):
    fresh_db.executescript(
        """
        CREATE TABLE t (id INTEGER PRIMARY KEY, "Weird Name" TEXT);
        CREATE INDEX idx_q ON t("Weird Name");
        CREATE TRIGGER trg_q AFTER UPDATE OF "Weird Name" ON t
        FOR EACH ROW WHEN OLD."Weird Name" IS NOT NULL
        BEGIN SELECT NEW."Weird Name"; END;
        CREATE VIEW v_q AS SELECT id, "Weird Name" FROM t;
        """
    )
    fresh_db.table("t").insert({"id": 1, "Weird Name": "a"})

    fresh_db.table("t").transform(
        rename={"Weird Name": "normal_name"}, preserve_schema=True
    )

    assert fresh_db.table("t").columns_dict == {"id": int, "normal_name": str}
    for name in ("idx_q", "trg_q", "v_q"):
        sql = fresh_db.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
        ).fetchone()[0]
        assert "normal_name" in sql
        assert "Weird Name" not in sql
    assert list(fresh_db.view("v_q").rows) == [{"id": 1, "normal_name": "a"}]


# ---------------------------------------------------------------------------
# Data, names, foreign keys and pragmas
# ---------------------------------------------------------------------------


def test_preserve_schema_keeps_rows_and_object_names(fresh_db):
    fresh_db.table("dogs").insert_all(
        [{"id": 1, "name": "Cleo"}, {"id": 2, "name": "Pancakes"}], pk="id"
    )
    fresh_db.execute("CREATE INDEX idx_name ON dogs(name)")
    fresh_db.execute("CREATE VIEW v AS SELECT id, name FROM dogs")

    fresh_db.table("dogs").transform(
        rename={"name": "full_name"}, preserve_schema=True
    )

    assert list(fresh_db.table("dogs").rows) == [
        {"id": 1, "full_name": "Cleo"},
        {"id": 2, "full_name": "Pancakes"},
    ]
    assert "idx_name" in {index.name for index in fresh_db.table("dogs").indexes}
    assert "v" in fresh_db.view_names()


def test_preserve_schema_keeps_foreign_keys(fresh_db):
    fresh_db.table("authors").insert({"id": 1, "name": "Jane"}, pk="id")
    fresh_db.table("books").insert(
        {"id": 1, "title": "Book", "author_id": 1},
        pk="id",
        foreign_keys=("author_id",),
    )
    fresh_db.execute("CREATE VIEW v AS SELECT title, author_id FROM books")

    fresh_db.table("books").transform(
        rename={"title": "book_title"}, preserve_schema=True
    )

    fk = fresh_db.table("books").foreign_keys[0]
    assert fk.other_table == "authors"
    assert fk.columns == ("author_id",)
    assert list(fresh_db.view("v").rows) == [
        {"book_title": "Book", "author_id": 1}
    ]


def test_preserve_schema_restores_foreign_key_pragmas(fresh_db):
    fresh_db.conn.execute("PRAGMA foreign_keys=ON")
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"}, pk="id")
    fresh_db.execute("CREATE VIEW v AS SELECT id, name FROM dogs")

    fresh_db.table("dogs").transform(
        rename={"name": "full_name"}, preserve_schema=True
    )

    assert fresh_db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_preserve_schema_inside_transaction_does_not_commit(fresh_db):
    fresh_db.table("dogs").create({"id": int, "name": str}, pk="id")
    fresh_db.conn.execute("PRAGMA foreign_keys=ON")
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"})
    fresh_db.execute("CREATE VIEW v AS SELECT id, name FROM dogs")

    fresh_db.conn.execute("BEGIN")
    fresh_db.execute("INSERT INTO dogs (id, name) VALUES (2, 'Temp')")
    fresh_db.table("dogs").transform(
        rename={"name": "full_name"}, preserve_schema=True
    )
    # Still inside the caller's transaction - transform() used a savepoint
    # rather than committing the surrounding work.
    assert fresh_db.conn.in_transaction
    # Committing the caller's transaction publishes both its own insert and
    # the rebuilt table plus the rewritten view together.
    fresh_db.conn.commit()

    assert fresh_db.table("dogs").columns_dict == {"id": int, "full_name": str}
    assert list(fresh_db.table("dogs").rows) == [
        {"id": 1, "full_name": "Cleo"},
        {"id": 2, "full_name": "Temp"},
    ]
    assert list(fresh_db.view("v").rows) == [
        {"id": 1, "full_name": "Cleo"},
        {"id": 2, "full_name": "Temp"},
    ]
    assert fresh_db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_preserve_schema_rolls_back_with_caller_savepoint(fresh_db):
    fresh_db.table("dogs").create({"id": int, "name": str}, pk="id")
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"})
    fresh_db.execute("CREATE VIEW v AS SELECT id, name FROM dogs")

    # A failure inside the caller's savepoint rolls the transform back
    # without touching the surrounding data or the view definition.
    view_sql_before = fresh_db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'v'"
    ).fetchone()[0]
    with pytest.raises(TransformError):
        with fresh_db.atomic():
            fresh_db.execute("INSERT INTO dogs (id, name) VALUES (2, 'Temp')")
            fresh_db.table("dogs").transform(
                drop={"name"}, preserve_schema=True
            )

    assert fresh_db.table("dogs").columns_dict == {"id": int, "name": str}
    assert (
        fresh_db.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'v'"
        ).fetchone()[0]
        == view_sql_before
    )
    assert not [name for name in fresh_db.table_names() if "new_" in name]


def test_preserve_schema_type_change_keeps_dependencies(fresh_db):
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo", "age": "5"}, pk="id")
    fresh_db.execute("CREATE INDEX idx_age ON dogs(age)")
    fresh_db.execute("CREATE VIEW v AS SELECT id, age FROM dogs")

    fresh_db.table("dogs").transform(
        types={"age": int}, preserve_schema=True
    )

    assert fresh_db.table("dogs").columns_dict["age"] is int
    assert "idx_age" in {index.name for index in fresh_db.table("dogs").indexes}
    assert list(fresh_db.view("v").rows) == [{"id": 1, "age": 5}]


# ---------------------------------------------------------------------------
# Failure / rollback guarantees
# ---------------------------------------------------------------------------


def test_preserve_schema_failure_leaves_table_and_objects_intact(fresh_db):
    fresh_db.table("dogs").create({"id": int, "name": str}, pk="id")
    fresh_db.executescript(
        """
        CREATE TABLE log (id INTEGER PRIMARY KEY, what TEXT);
        CREATE TRIGGER trg_dogs AFTER UPDATE OF name ON dogs
        FOR EACH ROW
        BEGIN INSERT INTO log(what) VALUES (NEW.name); END;
        CREATE VIEW dogs_view AS SELECT id, name FROM dogs;
        CREATE INDEX idx_name ON dogs(name);
        """
    )
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"})

    table_sql_before = fresh_db.table("dogs").schema
    view_sql_before = fresh_db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'dogs_view'"
    ).fetchone()[0]

    with pytest.raises(TransformError, match="trg_dogs|idx_name|dogs_view"):
        fresh_db.table("dogs").transform(drop={"name"}, preserve_schema=True)

    assert fresh_db.table("dogs").schema == table_sql_before
    assert (
        fresh_db.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'dogs_view'"
        ).fetchone()[0]
        == view_sql_before
    )
    assert "trg_dogs" in fresh_db.triggers_dict
    assert "idx_name" in {index.name for index in fresh_db.table("dogs").indexes}
    assert list(fresh_db.table("dogs").rows) == [{"id": 1, "name": "Cleo"}]
    # No leftover temporary table.
    assert not [name for name in fresh_db.table_names() if "new_" in name]


def test_preserve_schema_failure_with_foreign_keys_on(fresh_db):
    # The whole rebuild runs in a transaction, so a late failure rolls the
    # data back even when foreign keys are enabled.
    fresh_db.conn.execute("PRAGMA foreign_keys=ON")
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"}, pk="id")
    fresh_db.execute("CREATE VIEW dogs_view AS SELECT id, name FROM dogs")

    with pytest.raises(TransformError):
        fresh_db.table("dogs").transform(drop={"name"}, preserve_schema=True)

    assert fresh_db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert list(fresh_db.table("dogs").rows) == [{"id": 1, "name": "Cleo"}]
    assert not [name for name in fresh_db.table_names() if "new_" in name]


# ---------------------------------------------------------------------------
# Default behaviour is unchanged
# ---------------------------------------------------------------------------


def test_default_transform_does_not_rewrite_view(fresh_db):
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"}, pk="id")
    fresh_db.execute("CREATE VIEW dogs_view AS SELECT id, name FROM dogs")

    fresh_db.table("dogs").transform(rename={"name": "title"})

    # Default path leaves the stored view text byte-for-byte untouched.
    assert fresh_db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'dogs_view'"
    ).fetchone()[0] == "CREATE VIEW dogs_view AS SELECT id, name FROM dogs"


def test_default_transform_expression_index_still_raises(fresh_db):
    fresh_db.table("dogs").create({"id": int, "name": str}, pk="id")
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"})
    fresh_db.execute("CREATE INDEX idx_expr ON dogs(lower(name))")

    with pytest.raises(TransformError, match="partial or expression index"):
        fresh_db.table("dogs").transform(rename={"name": "full_name"})


# ---------------------------------------------------------------------------
# transform_sql() planning
# ---------------------------------------------------------------------------


def test_transform_sql_preserve_schema_emits_recreation(fresh_db):
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"}, pk="id")
    fresh_db.execute("CREATE VIEW dogs_view AS SELECT id, name FROM dogs")

    sqls = fresh_db.table("dogs").transform_sql(
        rename={"name": "title"}, preserve_schema=True, tmp_suffix="x"
    )

    joined = "\n".join(sqls)
    assert "DROP VIEW IF EXISTS" in joined
    assert any("CREATE VIEW dogs_view" in sql for sql in sqls)
    # Planning alone must not change the database.
    assert "dogs_view" in fresh_db.view_names()
    assert fresh_db.table("dogs").columns_dict == {"id": int, "name": str}


def test_transform_sql_preserve_schema_failure_before_destructive_sql(fresh_db):
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"}, pk="id")
    fresh_db.execute("CREATE VIEW dogs_view AS SELECT id, name FROM dogs")

    with pytest.raises(TransformError, match="dogs_view"):
        fresh_db.table("dogs").transform_sql(
            drop={"name"}, preserve_schema=True
        )

    # Nothing was executed.
    assert fresh_db.table("dogs").columns_dict == {"id": int, "name": str}


# ---------------------------------------------------------------------------
# CTEs, subqueries, quoting and case sensitivity
# ---------------------------------------------------------------------------


def test_preserve_schema_view_with_cte(fresh_db):
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"}, pk="id")
    fresh_db.execute(
        """
        CREATE VIEW cte_view AS
        WITH cte AS (SELECT id, name FROM dogs)
        SELECT id, name FROM cte WHERE name IS NOT NULL
        """
    )

    fresh_db.table("dogs").transform(
        rename={"name": "full_name"}, preserve_schema=True
    )

    sql = fresh_db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'cte_view'"
    ).fetchone()[0]
    assert "full_name" in sql
    assert list(fresh_db.view("cte_view").rows) == [
        {"id": 1, "full_name": "Cleo"}
    ]


def test_preserve_schema_view_with_derived_table(fresh_db):
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"}, pk="id")
    fresh_db.execute(
        "CREATE VIEW sub_view AS SELECT name FROM (SELECT id, name FROM dogs)"
    )

    fresh_db.table("dogs").transform(
        rename={"name": "full_name"}, preserve_schema=True
    )

    assert list(fresh_db.view("sub_view").rows) == [
        {"full_name": "Cleo"}
    ]


def test_preserve_schema_case_insensitive_rename(fresh_db):
    fresh_db.execute('CREATE TABLE T1 (ID INTEGER PRIMARY KEY, "Display Name" TEXT)')
    fresh_db.execute('CREATE VIEW V1 AS SELECT ID, "Display Name" FROM T1')

    fresh_db.table("T1").transform(
        rename={"display name": "name"}, preserve_schema=True
    )

    sql = fresh_db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'V1'"
    ).fetchone()[0]
    assert '"name"' in sql
    assert "Display Name" not in sql


def test_preserve_schema_backtick_and_bracket_identifiers(fresh_db):
    fresh_db.execute(
        "CREATE TABLE t3 (id INTEGER PRIMARY KEY, [col name] TEXT)"
    )
    fresh_db.execute("CREATE VIEW v3 AS SELECT `col name` FROM t3")

    fresh_db.table("t3").transform(
        rename={"col name": "renamed"}, preserve_schema=True
    )

    sql = fresh_db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'v3'"
    ).fetchone()[0]
    assert "`renamed`" in sql


def test_preserve_schema_rename_to_keyword_is_quoted_everywhere(fresh_db):
    fresh_db.table("d").insert({"id": 1, "name": "x"}, pk="id")
    fresh_db.execute("CREATE INDEX ix ON d(name)")
    fresh_db.execute("CREATE VIEW v AS SELECT name FROM d")
    fresh_db.executescript(
        "CREATE TRIGGER tr AFTER UPDATE OF name ON d "
        "FOR EACH ROW BEGIN SELECT NEW.name; END"
    )

    fresh_db.table("d").transform(
        rename={"name": "order"}, preserve_schema=True
    )

    for object_name in ("ix", "v", "tr"):
        sql = fresh_db.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (object_name,)
        ).fetchone()[0]
        assert '"order"' in sql
    assert list(fresh_db.view("v").rows) == [{"order": "x"}]


def test_preserve_schema_trigger_insert_select_and_delete(fresh_db):
    fresh_db.table("dogs").create({"id": int, "name": str}, pk="id")
    fresh_db.table("archive").create({"id": int, "name": str})
    fresh_db.table("tombstone").create({"id": int})
    fresh_db.executescript(
        """
        CREATE TRIGGER tr_ins AFTER INSERT ON dogs
        FOR EACH ROW
        BEGIN
            INSERT INTO archive(id, name)
            SELECT NEW.id, NEW.name WHERE NEW.name IS NOT NULL;
        END;
        CREATE TRIGGER tr_del BEFORE DELETE ON dogs
        FOR EACH ROW
        BEGIN
            INSERT INTO tombstone(id) SELECT OLD.id WHERE OLD.name IS NOT NULL;
        END;
        """
    )
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"})

    fresh_db.table("dogs").transform(
        rename={"name": "full_name"}, preserve_schema=True
    )

    fresh_db.execute("INSERT INTO dogs VALUES (2, 'Pancakes')")
    assert fresh_db.table("archive").get(2)["name"] == "Pancakes"
    fresh_db.execute("DELETE FROM dogs WHERE id = 2")
    assert list(fresh_db.table("tombstone").rows) == [{"id": 2}]


def test_preserve_schema_cross_table_trigger_raises_named_error(fresh_db):
    # A trigger attached to another table cannot be rebuilt here; refuse
    # rather than leave a trigger that references a renamed column.
    fresh_db.table("dogs").create({"id": int, "name": str}, pk="id")
    fresh_db.table("events").create({"id": int, "note": str}, pk="id")
    fresh_db.executescript(
        """
        CREATE TRIGGER trg_events AFTER INSERT ON events
        FOR EACH ROW
        BEGIN
            UPDATE dogs SET name = NEW.note WHERE id = NEW.id;
        END;
        """
    )

    with pytest.raises(TransformError, match="trg_events"):
        fresh_db.table("dogs").transform(
            rename={"name": "full_name"}, preserve_schema=True
        )

    assert fresh_db.table("dogs").columns_dict == {"id": int, "name": str}
    assert "trg_events" in fresh_db.triggers_dict


def test_preserve_schema_unrelated_cross_table_trigger_left_intact(fresh_db):
    fresh_db.table("dogs").create({"id": int, "name": str}, pk="id")
    fresh_db.table("events").create({"id": int, "note": str}, pk="id")
    fresh_db.executescript(
        """
        CREATE TRIGGER trg_events AFTER INSERT ON events
        FOR EACH ROW
        BEGIN
            UPDATE events SET note = NEW.note WHERE id = NEW.id;
        END;
        """
    )

    fresh_db.table("dogs").transform(
        rename={"name": "full_name"}, preserve_schema=True
    )

    assert "trg_events" in fresh_db.triggers_dict
    assert fresh_db.table("dogs").columns_dict == {"id": int, "full_name": str}


def test_preserve_schema_trigger_with_numeric_literal_body(fresh_db):    # A "SELECT 1" style trigger body must not be mistaken for a column ref.
    fresh_db.table("dogs").create({"id": int, "name": str}, pk="id")
    fresh_db.executescript(
        """
        CREATE TRIGGER trg_dogs AFTER UPDATE OF name ON dogs
        FOR EACH ROW
        BEGIN SELECT 1; END;
        """
    )

    fresh_db.table("dogs").transform(
        rename={"name": "full_name"}, preserve_schema=True
    )

    sql = fresh_db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'trg_dogs'"
    ).fetchone()[0]
    assert "UPDATE OF full_name" in sql
    assert "SELECT 1" in sql


def test_preserve_schema_block_comment_unchanged(fresh_db):
    fresh_db.table("dogs").insert({"id": 1, "name": "Cleo"}, pk="id")
    fresh_db.execute(
        "CREATE VIEW v AS SELECT id, name /* block name comment */ FROM dogs"
    )

    fresh_db.table("dogs").transform(
        rename={"name": "full_name"}, preserve_schema=True
    )

    sql = fresh_db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'v'"
    ).fetchone()[0]
    assert "/* block name comment */" in sql
    assert "full_name" in sql


def test_preserve_schema_restores_defer_foreign_keys(fresh_db):
    fresh_db.conn.execute("PRAGMA foreign_keys=ON")
    fresh_db.table("dogs").create({"id": int, "name": str}, pk="id")
    fresh_db.execute("CREATE VIEW v AS SELECT id, name FROM dogs")
    # Open a transaction so transform() takes the defer_foreign_keys path.
    fresh_db.conn.execute("BEGIN")
    assert (
        fresh_db.conn.execute("PRAGMA defer_foreign_keys").fetchone()[0] == 0
    )

    fresh_db.table("dogs").transform(
        rename={"name": "full_name"}, preserve_schema=True
    )

    # defer_foreign_keys is switched back off after the transform.
    assert fresh_db.conn.execute("PRAGMA defer_foreign_keys").fetchone()[0] == 0
    assert fresh_db.conn.in_transaction
    fresh_db.conn.rollback()
