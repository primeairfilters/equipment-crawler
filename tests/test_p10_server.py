"""P10: the PostgreSQL server itself: extensions, a live-only vector index, monthly log partitions, UTC, search path.

The vector check builds a TEMPORARY table as the migrate role inside a transaction that is rolled back,
so it leaves nothing behind.
"""
from datetime import date, datetime

from support import connect


def q(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []


def test_vector_and_trigram_extensions_are_installed(runtime_conn):
    found = dict(q(runtime_conn, "SELECT extname, extversion FROM pg_extension"))
    assert "vector" in found and "pg_trgm" in found, found
    major, minor, *_ = (int(x) for x in found["vector"].split("."))
    assert (major, minor) >= (0, 8), found["vector"]  # iterative index scans arrived in 0.8


def test_server_is_postgres_18(runtime_conn):
    assert int(q(runtime_conn, "SHOW server_version_num")[0][0]) >= 180000


def test_a_partial_hnsw_index_on_halfvec_works():
    conn = connect("migrate")
    try:
        q(conn, "CREATE TEMPORARY TABLE probe_vectors (id int PRIMARY KEY, is_live boolean NOT NULL, "
                "embedding halfvec(3) NOT NULL) ON COMMIT DROP")
        q(conn, "CREATE INDEX ON probe_vectors USING hnsw (embedding halfvec_cosine_ops) WHERE is_live")
        q(conn, "INSERT INTO probe_vectors VALUES (1, true, '[1,0,0]'), (2, true, '[0,1,0]'), (3, false, '[1,0.01,0]')")
        q(conn, "SET LOCAL enable_seqscan = off")
        q(conn, "SET LOCAL hnsw.iterative_scan = relaxed_order")
        plan = "\n".join(r[0] for r in q(conn, "EXPLAIN SELECT id FROM probe_vectors WHERE is_live "
                                               "ORDER BY embedding <=> '[1,0,0]' LIMIT 2"))
        assert "probe_vectors_embedding_idx" in plan, plan
        nearest = [r[0] for r in q(conn, "SELECT id FROM probe_vectors WHERE is_live ORDER BY embedding <=> '[1,0,0]' LIMIT 2")]
        assert nearest == [1, 2]  # row 3 is closer but not live, so the live-only index never returns it
    finally:
        conn.rollback()
        conn.close()


def test_trigram_similarity_finds_a_misread_model_number(runtime_conn):
    score = q(runtime_conn, "SELECT similarity('RA15AZ24AJ3CA', 'RA15A224AJ3CA')")[0][0]
    assert score > 0.4


def test_connections_work_in_utc_and_find_crawl_tables_first(runtime_conn):
    assert q(runtime_conn, "SHOW timezone")[0][0] == "UTC"
    assert q(runtime_conn, "SELECT current_schemas(false)")[0][0] == ["crawl", "kb", "public"]


def test_breeze_sees_only_kb_on_its_path():
    conn = connect("breeze")
    try:
        assert q(conn, "SELECT current_schemas(false)")[0][0] == ["kb", "public"]
    finally:
        conn.close()


def test_logs_have_a_partition_for_this_month_and_next_year(migrate_conn):
    today = date.today()
    for table in ("crawl.ai_calls", "crawl.crawl_runs", "kb.visibility_log"):
        parts = {r[0] for r in q(migrate_conn, "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                                               "WHERE i.inhparent = %s::regclass", (table,))}
        name = table.split(".")[1]
        assert f"{name}_default" in parts, parts
        assert f"{name}_{today:%Y_%m}" in parts, parts
        assert f"{name}_{today.year + 1}_{today.month:02d}" in parts, parts


def test_an_ai_call_lands_in_its_month_partition(runtime_conn):
    when = datetime.now().replace(microsecond=0)
    q(runtime_conn, "INSERT INTO ai_calls (purpose, model, cost_usd, created_at) VALUES ('probe', 'm', 0, %s)", (when,))
    where = q(runtime_conn, "SELECT tableoid::regclass::text FROM ai_calls WHERE purpose = 'probe' AND created_at = %s", (when,))
    assert where[0][0] == f"ai_calls_{when:%Y_%m}"  # crawl is on the search path, so the name prints without it
