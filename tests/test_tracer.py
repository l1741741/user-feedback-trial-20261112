from sqlite_utils import Database


def test_tracer():
    collected = []
    db = Database(
        memory=True, tracer=lambda sql, params: collected.append((sql, params))
    )
    dogs = db.table("dogs")
    dogs.insert({"name": "Cleopaws"})
    dogs.enable_fts(["name"])
    dogs.search("Cleopaws")
    assert collected == [
        ("PRAGMA recursive_triggers=on;", None),
        ("select name from sqlite_master where type = 'view'", None),
        ("select name from sqlite_master where type = 'table'", None),
        ("select name from sqlite_master where type = 'view'", None),
        ("select name from sqlite_master where type = 'view'", None),
        ("select name from sqlite_master where type = 'table'", None),
        ("select name from sqlite_master where type = 'view'", None),
        ('CREATE TABLE "dogs" (\n   "name" TEXT\n);\n        ', None),
        ("select name from sqlite_master where type = 'view'", None),
        ('INSERT INTO "dogs" ("name") VALUES (?)', ["Cleopaws"]),
        ("select name from pragma_table_info(?)", ("dogs",)),
        (
            "select name, sql from sqlite_master where type = 'table' "
            "and sql like '%VIRTUAL TABLE%USING FTS%'",
            None,
        ),
        (
            'CREATE VIRTUAL TABLE "dogs_fts" USING FTS5 (\n    "name",\n    content="dogs"\n)',
            None,
        ),
        (
            'INSERT INTO "dogs_fts"("dogs_fts") VALUES(\'rebuild\');',
            None,
        ),
    ]


def test_with_tracer():
    collected = []

    def tracer(sql, params):
        return collected.append((sql, params))

    db = Database(memory=True)

    dogs = db.table("dogs")

    dogs.insert({"name": "Cleopaws"})
    dogs.enable_fts(["name"])

    assert len(collected) == 0

    with db.tracer(tracer):
        list(dogs.search("Cleopaws"))

    assert len(collected) == 4
    assert collected == [
        (
            (
                "select name, sql from sqlite_master where type = 'table' "
                "and sql like '%VIRTUAL TABLE%USING FTS%'"
            ),
            None,
        ),
        ("select name from sqlite_master where type = 'view'", None),
        ("select sql from sqlite_master where name = ?", ("dogs_fts",)),
        (
            (
                'with "original" as (\n'
                "    select\n"
                "        rowid,\n"
                "        *\n"
                '    from "dogs"\n'
                ")\n"
                "select\n"
                '    "original".*\n'
                "from\n"
                '    "original"\n'
                '    join "dogs_fts" on "original".rowid = "dogs_fts".rowid\n'
                "where\n"
                '    "dogs_fts" match :query\n'
                "order by\n"
                '    "dogs_fts".rank'
            ),
            {"query": "Cleopaws"},
        ),
    ]

    # Outside the with block collected should not be appended to
    dogs.insert({"name": "Cleopaws"})
    assert len(collected) == 4
