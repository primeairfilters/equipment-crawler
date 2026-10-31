"""One-off: copy the Mac's crawler database into the prod server's new, empty one (EQUIPMENT_DATA_CRAWLER.md P22 step 4).

Each table is streamed with COPY from the source connection straight into the target connection; nothing is written
to disk. It only adds rows, and refuses unless every target table is empty (crawl.schema_migrations aside: migrate
filled it on both sides, so it is not copied). The whole copy is one transaction on the target, so a failure leaves
the target as empty as it was.

Without --go it is a rehearsal: everything is copied and counted, then rolled back. With --go the copy is kept.
(A rehearsal does move the target's sequences on, since setval is not rolled back; --go sets them again.)

Run on the Mac, in the crawler container (the source is the Mac's `pg`; the target is reached through an SSH tunnel):
  docker compose run --rm -v ~/.config/tracforce-crawler:/conf:ro crawler \
      python ops/copy_to_prod.py --target-host host.docker.internal --target-port 15432 [--go]
Passwords: /conf/pg-root.env (the Mac's postgres) and /conf/prod-pg-root.env (prod's postgres, copied from the server).
"""
import argparse
import sys

import psycopg
from psycopg import sql

SCHEMAS = ("crawl", "kb", "control", "breeze")
NOT_COPIED = {"crawl.schema_migrations"}
DB = "tracforce_equipment"


def password(path):
    with open(path) as handle:
        for line in handle:
            if line.startswith("POSTGRES_PASSWORD="):
                return line.split("=", 1)[1].strip()
    sys.exit(f"No POSTGRES_PASSWORD in {path}.")


def tables(conn):
    """Top-level tables (a partitioned log is copied through its parent) with their stored columns, in order."""
    rows = conn.execute(
        """SELECT n.nspname, c.relname,
                  array_agg(a.attname ORDER BY a.attnum) FILTER (WHERE a.attnum > 0 AND NOT a.attisdropped
                                                                 AND a.attgenerated = '')
           FROM pg_class c
           JOIN pg_namespace n ON n.oid = c.relnamespace
           JOIN pg_attribute a ON a.attrelid = c.oid
           WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p') AND NOT c.relispartition
           GROUP BY 1, 2 ORDER BY 1, 2""", (list(SCHEMAS),)).fetchall()
    return {f"{s}.{t}": (s, t, cols) for s, t, cols in rows if f"{s}.{t}" not in NOT_COPIED}


def count(conn, schema, table):
    return conn.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table))).fetchone()[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-host", default="pg")
    parser.add_argument("--source-port", type=int, default=5432)
    parser.add_argument("--target-host", required=True)
    parser.add_argument("--target-port", type=int, required=True)
    parser.add_argument("--source-password-file", default="/conf/pg-root.env")
    parser.add_argument("--target-password-file", default="/conf/prod-pg-root.env")
    parser.add_argument("--go", action="store_true", help="keep the copy (without it: copy, count, roll back)")
    args = parser.parse_args()

    src = psycopg.connect(host=args.source_host, port=args.source_port, user="postgres", dbname=DB,
                          password=password(args.source_password_file))
    tgt = psycopg.connect(host=args.target_host, port=args.target_port, user="postgres", dbname=DB,
                          password=password(args.target_password_file))

    # One consistent snapshot of the source for the whole copy (must come before its first query).
    src.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")

    ident = "SELECT system_identifier FROM pg_control_system()"
    if src.execute(ident).fetchone()[0] == tgt.execute(ident).fetchone()[0]:
        sys.exit("Source and target are the same database server. Nothing copied.")

    source_tables, target_tables = tables(src), tables(tgt)
    if source_tables.keys() != target_tables.keys():
        missing = sorted(source_tables.keys() ^ target_tables.keys())
        sys.exit(f"The two databases do not have the same tables (run ./crawl migrate on both): {', '.join(missing)}")
    for name, (_, _, cols) in source_tables.items():
        if cols != target_tables[name][2]:
            sys.exit(f"{name} has different columns on the two sides (run ./crawl migrate on both). Nothing copied.")

    full = [name for name, (s, t, _) in target_tables.items() if count(tgt, s, t)]
    if full:
        sys.exit(f"The target already has rows in {', '.join(full)}. This only fills an empty database. Nothing copied.")

    # The rows arrive complete (vectors, document brands), so the target's triggers and foreign-key checks are not
    # run again; generated columns (tsv, name_key, ...) are left out and the target computes them.
    tgt.execute("SET LOCAL session_replication_role = replica")

    wanted = {}
    for name, (s, t, cols) in source_tables.items():
        wanted[name] = count(src, s, t)
        if not wanted[name]:
            continue
        columns = sql.SQL(", ").join(map(sql.Identifier, cols))
        table = sql.SQL("{}.{}").format(sql.Identifier(s), sql.Identifier(t))
        with src.cursor().copy(sql.SQL("COPY (SELECT {} FROM {}) TO STDOUT").format(columns, table)) as out, \
                tgt.cursor().copy(sql.SQL("COPY {} ({}) FROM STDIN").format(table, columns)) as into:
            for chunk in out:
                into.write(chunk)
        print(f"{name}: {wanted[name]} rows", flush=True)

    wrong = [f"{name} ({count(tgt, s, t)} of {wanted[name]})" for name, (s, t, _) in target_tables.items()
             if count(tgt, s, t) != wanted[name]]
    if wrong:
        tgt.rollback()
        sys.exit(f"Counts differ after the copy, rolled back: {', '.join(wrong)}")

    # Sequences carry on from the source's last values, so new rows on prod never reuse a copied id.
    seqs = src.execute("SELECT schemaname, sequencename, last_value FROM pg_sequences "
                       "WHERE schemaname = ANY(%s) AND last_value IS NOT NULL", (list(SCHEMAS),)).fetchall()
    for schema, name, last in seqs:
        tgt.execute("SELECT setval(%s, %s, true)", (f"{schema}.{name}", last))

    total = sum(wanted.values())
    if args.go:
        tgt.commit()
        print(f"Copied {total} rows in {len(wanted)} tables and set {len(seqs)} sequences. Kept.")
    else:
        tgt.rollback()
        print(f"Rehearsal: {total} rows in {len(wanted)} tables and {len(seqs)} sequences would be copied. "
              "Rolled back; run again with --go to keep it.")
    src.rollback()


if __name__ == "__main__":
    main()
