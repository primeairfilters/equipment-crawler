"""P1 (P10 on PostgreSQL): `migrate` builds every table in crawl and kb, records itself, and a re-run changes nothing.

Migrate only adds. The re-run check is a read-only before/after comparison of the catalog
plus the schema_migrations rows.
"""
import re
import subprocess
import sys
from pathlib import Path

from support import clean_env, connect, db_name

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "migrations"

EXPECTED_TABLES = {
    ("crawl", "schema_migrations"),
    ("crawl", "brands"), ("crawl", "brand_domains"), ("crawl", "start_urls"), ("crawl", "hosts"),
    ("crawl", "pages"), ("crawl", "page_text"), ("crawl", "page_links"), ("crawl", "crawl_runs"),
    ("crawl", "crawl_keywords"), ("crawl", "ai_calls"), ("crawl", "gapfill_searches"), ("crawl", "es_rows"),
    ("crawl", "es_state"), ("crawl", "files"), ("crawl", "document_urls"), ("crawl", "keyword_suggestions"),
    ("kb", "manufacturers"), ("kb", "manufacturer_aliases"), ("kb", "models"), ("kb", "documents"),
    ("kb", "document_models"), ("kb", "sections"), ("kb", "section_terms"), ("kb", "visibility_log"),
    ("kb", "es_systems"), ("kb", "es_system_members"),
}

# DROP is forbidden except `DROP CONSTRAINT IF EXISTS`, which removes no data: the user approved it on 2026-10-27
# for swapping a CHECK constraint (0002). DROP TABLE / COLUMN / INDEX / SCHEMA stay forbidden.
FORBIDDEN_SQL = [
    r"\bDROP\b(?!\s+CONSTRAINT\s+IF\s+EXISTS\b)", r"\bDELETE\b", r"\bTRUNCATE\b", r"\bREPLACE\b", r"\bCASCADE\b",
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
    """Ordinary and partitioned tables in crawl and kb; partitions themselves are left out."""
    return {(s, t) for s, t in rows(
        "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname IN ('crawl', 'kb') AND c.relkind IN ('r', 'p') AND NOT c.relispartition")}


def snapshot():
    return {
        "tables": rows("SELECT n.nspname, c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                       "WHERE n.nspname IN ('crawl', 'kb') ORDER BY 1, 2"),
        "columns": rows("SELECT table_schema, table_name, column_name, ordinal_position, data_type, is_nullable, "
                        "column_default, collation_name FROM information_schema.columns "
                        "WHERE table_schema IN ('crawl', 'kb') ORDER BY 1, 2, 4"),
        "indexes": rows("SELECT schemaname, tablename, indexname, indexdef FROM pg_indexes "
                        "WHERE schemaname IN ('crawl', 'kb') ORDER BY 1, 2, 3"),
        "constraints": rows("SELECT conrelid::regclass::text, conname, contype, pg_get_constraintdef(oid) FROM pg_constraint "
                            "WHERE connamespace IN ('crawl'::regnamespace, 'kb'::regnamespace) ORDER BY 1, 2"),
        "grants": rows("SELECT table_schema, table_name, grantee, privilege_type FROM information_schema.role_table_grants "
                       "WHERE table_schema IN ('crawl', 'kb') ORDER BY 1, 2, 3, 4"),
        "schema_migrations": rows("SELECT * FROM crawl.schema_migrations ORDER BY 1"),
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
        sql = re.sub(r"--[^\n]*", "", sql)
        for pattern in FORBIDDEN_SQL:
            assert not re.search(pattern, sql, flags=re.I), f"{f.name} contains {pattern}"


def test_drop_is_only_allowed_for_a_constraint():
    allowed = "ALTER TABLE crawl.pages DROP CONSTRAINT IF EXISTS pages_fetch_state_check"
    assert not any(re.search(p, allowed, flags=re.I) for p in FORBIDDEN_SQL)
    for bad in ("DROP TABLE crawl.pages", "ALTER TABLE crawl.pages DROP COLUMN url", "DROP INDEX pages_host",
                "ALTER TABLE crawl.pages DROP CONSTRAINT pages_fetch_state_check", "DROP SCHEMA kb"):
        assert any(re.search(p, bad, flags=re.I) for p in FORBIDDEN_SQL), bad


def test_migrate_creates_every_table_and_records_each_migration():
    migrate_ok()
    tables = table_names()
    assert EXPECTED_TABLES <= tables, f"missing: {sorted(EXPECTED_TABLES - tables)}"
    assert int(rows("SELECT COUNT(*) FROM crawl.schema_migrations")[0][0]) == len(migration_files())


def test_key_columns_and_indexes_exist():
    migrate_ok()
    cols = {(t, c) for t, c in rows(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema IN ('crawl', 'kb')")}
    for needed in [("models", "model_key"), ("models", "is_base_model"), ("document_models", "model_key"),
                   ("sections", "is_live"), ("documents", "content_sha"), ("pages", "fetch_state"),
                   ("hosts", "ignore_robots"), ("start_urls", "depth")]:
        assert needed in cols, needed
    defs = [r[0] for r in rows("SELECT indexdef FROM pg_indexes WHERE schemaname = 'kb'")]
    assert any("UNIQUE" in d and "(manufacturer_id, model_key)" in d for d in defs), defs
    assert any("gin_trgm_ops" in d for d in defs), defs
    assert any("document_models" in d and "(model_key, document_id)" in d for d in defs), defs
    assert any("sections" in d and "WHERE is_live" in d for d in defs), defs


def test_no_cascade_or_set_null_foreign_keys():
    migrate_ok()
    assert EXPECTED_TABLES <= table_names()  # the schema is there, so an empty result below means something
    bad = rows("SELECT conrelid::regclass::text, conname FROM pg_constraint WHERE contype = 'f' "
               "AND connamespace IN ('crawl'::regnamespace, 'kb'::regnamespace) "
               "AND (confdeltype IN ('c', 'n', 'd') OR confupdtype IN ('c', 'n', 'd'))")
    assert bad == []


def test_kb_has_no_foreign_keys_into_crawl():
    migrate_ok()
    into_crawl = rows("SELECT conrelid::regclass::text, conname FROM pg_constraint WHERE contype = 'f' "
                      "AND connamespace = 'kb'::regnamespace "
                      "AND confrelid IN (SELECT oid FROM pg_class WHERE relnamespace = 'crawl'::regnamespace)")
    assert into_crawl == []


def test_rerunning_migrate_changes_nothing():
    migrate_ok()
    before = snapshot()
    assert len(before["tables"]) >= len(EXPECTED_TABLES)
    assert before["schema_migrations"], "schema_migrations is empty, so the first run did not record anything"

    second = run_migrate()
    assert second.returncode == 0, second.stdout + second.stderr
    assert "already applied" in second.stdout

    after = snapshot()
    for part in before:
        assert after[part] == before[part], f"{part} changed on re-run"
