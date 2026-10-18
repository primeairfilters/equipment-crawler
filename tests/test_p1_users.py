"""P1: both DB users are scoped to tracforce_equipment only, the runtime user cannot DELETE,
and neither can read hvac_app.

Nothing here drops, empties or deletes anything. The DELETE probe uses WHERE 1=0 inside a
transaction that is rolled back, and MariaDB checks the privilege before it looks at rows.
"""
import re

import pymysql
import pytest

from support import TESTING_DB, connect

ALLOWED_SCHEMAS = {"tracforce_equipment", TESTING_DB}
FORBIDDEN_PRIVS = {"ALL", "ALL PRIVILEGES", "DELETE", "DROP", "GRANT OPTION", "SUPER", "FILE"}
DENIED = {1044, 1142, 1143, 1045}  # database / table / column access denied


def grants(conn):
    with conn.cursor() as cur:
        cur.execute("SHOW GRANTS FOR CURRENT_USER()")
        rows = [r[0] for r in cur.fetchall()]
    parsed = []
    for row in rows:
        m = re.match(r"^GRANT (.+?) ON (.+?) TO ", row)
        assert m, f"unparseable grant: {row}"
        privs = {p.strip().upper() for p in m.group(1).split(",")}
        target = m.group(2).replace("`", "").replace("\\", "")
        parsed.append((privs, target, "WITH GRANT OPTION" in row.upper(), row))
    return parsed


def check_scope(parsed):
    on_equipment = [g for g in parsed if g[1].split(".")[0] in ALLOWED_SCHEMAS]
    assert on_equipment, f"no grant on the equipment databases at all: {parsed}"
    for privs, target, with_grant, row in parsed:
        if target == "*.*":
            assert privs == {"USAGE"}, f"global privileges beyond USAGE: {row}"
            continue
        schema = target.split(".")[0]
        assert schema in ALLOWED_SCHEMAS, f"grant outside the equipment databases: {row}"
        assert target.endswith(".*"), f"grant narrower or wider than schema.*: {row}"
        assert not (privs & FORBIDDEN_PRIVS), f"forbidden privilege in: {row}"
        assert not with_grant, f"WITH GRANT OPTION in: {row}"
    return on_equipment


def privs_on_testing_db(parsed):
    privs = set()
    for p, target, _, _ in parsed:
        if target == f"{TESTING_DB}.*":
            privs |= p
    return privs


def test_runtime_user_grants_are_select_insert_update_only(runtime_conn):
    parsed = grants(runtime_conn)
    check_scope(parsed)
    privs = privs_on_testing_db(parsed)
    assert {"SELECT", "INSERT", "UPDATE"} <= privs, privs
    assert not (privs & {"CREATE", "ALTER", "INDEX"}), f"runtime user can change the schema: {privs}"


def test_migrate_user_grants_can_build_but_not_drop(migrate_conn):
    parsed = grants(migrate_conn)
    check_scope(parsed)
    privs = privs_on_testing_db(parsed)
    assert {"CREATE", "ALTER", "INDEX"} <= privs, privs


def test_runtime_user_is_refused_delete_on_every_table(runtime_conn):
    with runtime_conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'"
        )
        tables = sorted(r[0] for r in cur.fetchall())
    assert "schema_migrations" in tables, f"migrate has not built the schema yet: {tables}"

    refused = []
    for table in tables:
        with runtime_conn.cursor() as cur:
            # Control: the user can read this table, so a refusal below is about DELETE only.
            cur.execute(f"SELECT COUNT(*) FROM `{table}`")
            cur.fetchone()
            try:
                cur.execute(f"DELETE FROM `{table}` WHERE 1=0")
            except pymysql.MySQLError as exc:
                assert exc.args[0] == 1142, f"{table}: wrong error for DELETE: {exc!r}"
                refused.append(table)
            else:
                pytest.fail(f"DELETE was NOT refused on {table}")
    assert refused == tables


@pytest.mark.parametrize("kind", ["runtime", "migrate"])
def test_user_cannot_read_hvac_app(kind):
    conn = connect(kind, autocommit=False)
    try:
        with conn.cursor() as cur:
            # Control: the same connection can read its own database.
            cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE()")
            assert cur.fetchone()[0] > 0

            with pytest.raises(pymysql.MySQLError) as table_read:
                cur.execute("SELECT 1 FROM hvac_app.users WHERE 1=0")
            assert table_read.value.args[0] in DENIED, repr(table_read.value)

            with pytest.raises(pymysql.MySQLError) as switch_db:
                cur.execute("USE hvac_app")
            assert switch_db.value.args[0] in DENIED, repr(switch_db.value)

            cur.execute("SHOW DATABASES")
            visible = {r[0] for r in cur.fetchall()}
            assert not (visible & {"hvac_app", "hvac_app_testing"}), visible
            assert TESTING_DB in visible
    finally:
        conn.rollback()
        conn.close()
