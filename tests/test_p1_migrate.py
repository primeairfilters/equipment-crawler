"""P1: `migrate` builds every table, records itself, and a re-run changes nothing.

Migrate only adds. The re-run check is a read-only before/after comparison of
information_schema plus the schema_migrations rows.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

from support import clean_env, connect, db_name

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "migrations"

EXPECTED_TABLES = {
    "schema_migrations",
    "brands", "brand_domains", "start_urls", "hosts", "pages", "page_text",
    "found_models", "found_specs", "energy_star_rows", "ai_calls",
    "manufacturers", "manufacturer_aliases", "equipment_models", "equipment_sources",
    "equipment_model_sources", "equipment_systems", "equipment_system_members",
    "equipment_specs",
}

FORBIDDEN_SQL = [
    r"\bDROP\b", r"\bDELETE\b", r"\bTRUNCATE\b", r"\bREPLACE\b", r"\bCASCADE\b",
    r"\bSET\s+NULL\b", r"\bSET\s+DEFAULT\b",
]


def run_migrate():
    env = clean_env(EQ_DB_NAME=db_name())
    return subprocess.run(
        [sys.executable, "-m", "crawler", "migrate"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
    )


def migrate_ok():
    result = run_migrate()
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def rows(sql):
    conn = connect("migrate", autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return [tuple(str(v) for v in r) for r in cur.fetchall()]
    finally:
        conn.close()


def table_names():
    return {r[0] for r in rows(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'")}


def snapshot():
    return {
        "tables": rows("SELECT table_name, engine, table_collation, create_time FROM information_schema.tables "
                       "WHERE table_schema = DATABASE() ORDER BY 1"),
        "columns": rows("SELECT table_name, column_name, ordinal_position, column_type, is_nullable, "
                        "column_key, column_default, extra FROM information_schema.columns "
                        "WHERE table_schema = DATABASE() ORDER BY 1, 3"),
        "indexes": rows("SELECT table_name, index_name, seq_in_index, column_name, non_unique "
                        "FROM information_schema.statistics WHERE table_schema = DATABASE() ORDER BY 1, 2, 3"),
        "constraints": rows("SELECT table_name, constraint_name, constraint_type FROM "
                            "information_schema.table_constraints WHERE table_schema = DATABASE() ORDER BY 1, 2"),
        "schema_migrations": rows("SELECT * FROM schema_migrations ORDER BY 1"),
    }


def migration_files():
    return sorted(MIGRATIONS.glob("*.sql"))


def test_crawl_script_exists():
    assert (ROOT / "crawl").is_file()


def test_migration_files_are_numbered_and_additive_only():
    files = migration_files()
    assert files, "no migrations/*.sql files"
    for f in files:
        assert re.match(r"^\d{4}_.+\.sql$", f.name), f.name
        sql = re.sub(r"/\*.*?\*/", "", f.read_text(), flags=re.S)
        sql = re.sub(r"(--|#)[^\n]*", "", sql)
        for pattern in FORBIDDEN_SQL:
            assert not re.search(pattern, sql, flags=re.I), f"{f.name} contains {pattern}"


def test_migrate_creates_every_table_and_records_each_migration():
    migrate_ok()
    tables = table_names()
    assert EXPECTED_TABLES <= tables, f"missing: {sorted(EXPECTED_TABLES - tables)}"
    assert int(rows("SELECT COUNT(*) FROM schema_migrations")[0][0]) == len(migration_files())


def test_key_columns_and_indexes_exist():
    migrate_ok()
    cols = {(t, c) for t, c in rows(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = DATABASE()")}
    assert ("equipment_specs", "extracted_by") in cols
    assert ("equipment_models", "model_key") in cols
    assert ("equipment_models", "is_base_model") in cols

    idx = {}
    for name, non_unique, seq, col in rows(
        "SELECT index_name, non_unique, seq_in_index, column_name FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = 'equipment_models' ORDER BY 1, 3"):
        idx.setdefault(name, {"unique": non_unique == "0", "cols": []})["cols"].append(col)
    assert any(i["unique"] and "model_key" in i["cols"] and len(i["cols"]) >= 2 for i in idx.values()), idx
    assert any(i["cols"][0] == "model_key" for i in idx.values()), idx


def test_no_cascade_or_set_null_foreign_keys():
    migrate_ok()
    assert EXPECTED_TABLES <= table_names()  # the schema is there, so an empty result below means something
    bad = rows(
        "SELECT table_name, constraint_name, delete_rule, update_rule FROM information_schema.referential_constraints "
        "WHERE constraint_schema = DATABASE() AND (delete_rule IN ('CASCADE','SET NULL','SET DEFAULT') "
        "OR update_rule IN ('CASCADE','SET NULL','SET DEFAULT'))")
    assert bad == []


def test_rerunning_migrate_changes_nothing():
    migrate_ok()
    before = snapshot()
    assert len(before["tables"]) >= len(EXPECTED_TABLES)
    assert before["schema_migrations"], "schema_migrations is empty, so the first run did not record anything"

    second = run_migrate()
    assert second.returncode == 0, second.stdout + second.stderr

    after = snapshot()
    for part in before:
        assert after[part] == before[part], f"{part} changed on re-run"
