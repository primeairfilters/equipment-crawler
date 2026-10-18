"""Applies migrations/NNNN_name.sql in order as the migrate user. Only adds; re-runs are no-ops.

Every statement in a migration must be safe to run twice (IF NOT EXISTS), because a file that
stops part way is run again from its first statement.
"""
import os
from pathlib import Path

import pymysql

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def connect():
    return pymysql.connect(
        host=os.environ.get("EQ_DB_HOST", "db"),
        port=int(os.environ.get("EQ_DB_PORT", "3306")),
        user=os.environ["EQ_MIGRATE_USER"],
        password=os.environ["EQ_MIGRATE_PASSWORD"],
        database=os.environ.get("EQ_DB_NAME", "tracforce_equipment"),
        autocommit=True,
        connect_timeout=10,
    )


def migrate():
    conn = connect()
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name VARCHAR(190) NOT NULL PRIMARY KEY, "
            "applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
        cur.execute("SELECT name FROM schema_migrations")
        done = {row[0] for row in cur.fetchall()}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in done:
                print(f"already applied: {path.name}")
                continue
            text = "\n".join(l for l in path.read_text().splitlines() if not l.lstrip().startswith("--"))
            for statement in text.split(";\n"):
                if statement.strip():
                    cur.execute(statement)
            cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            print(f"applied: {path.name}")
    conn.close()
