"""Command line entry: python -m crawler <command>."""
import argparse
import shlex
import sys

from crawler.hosts import RefusedHost


def build_parser():
    parser = argparse.ArgumentParser(prog="./crawl")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="create or update the tables")
    add = sub.add_parser("add", help="add a brand, with optional start web addresses")
    add.add_argument("brand")
    add.add_argument("urls", nargs="*")
    ok = sub.add_parser("ok", help="allow fetching from a host")
    ok.add_argument("host")
    ok.add_argument("--brand")
    stop = sub.add_parser("stop", help="stop a brand or a host")
    stop.add_argument("target")
    sub.add_parser("status", help="show where each brand is at")
    run = sub.add_parser("run", help="fetch pages")
    run.add_argument("--brand")
    sub.add_parser("energystar", help="pull the ENERGY STAR heat pump and AC list (about once a month)")
    gap = sub.add_parser("gapfill", help="search for documents for ENERGY STAR models that have no specs yet")
    gap.add_argument("brand", nargs="?")
    extract = sub.add_parser("extract", help="read fetched documents with AI (collects finished batches, sends new ones)")
    extract.add_argument("brand", nargs="?")
    return parser


def _discover(conn, brand):
    """Ask AI where the brand's data lives (once every 180 days), then read approved hosts' sitemaps."""
    from crawler.ai import AnthropicClient
    from crawler.discover import discover
    from crawler.fetch import Fetcher
    from crawler.sitemaps import read_sitemaps

    try:
        client = AnthropicClient()
    except RuntimeError:
        print("No AI key is set (ANTHROPIC_API_KEY_CRAWLER), so I skipped finding the brand's websites.")
        client = None
    if client is not None:
        try:
            found = discover(conn, client, brand)
        finally:
            conn.commit()
        if found == "recent":
            print("Already looked for this brand's websites in the last 6 months.")
        elif found == "budget":
            print("Monthly AI budget reached, so I skipped finding websites until the 1st.")
        elif found == "unusable":
            print("Skipped finding websites: the AI answer could not be used. Run ./crawl add again to retry.")
        if isinstance(found, dict):
            with conn.cursor() as cur:
                cur.execute("SELECT d.host FROM brand_domains d JOIN brands b ON b.id = d.brand_id "
                            "WHERE b.name_key = %s AND d.status = 'waiting' ORDER BY d.host", (" ".join(brand.split()).lower(),))
                hosts = [r[0] for r in cur.fetchall()]
            if hosts:
                print("Found these websites. Nothing is fetched until you say OK:")
                for host in hosts:
                    print(f"  ./crawl ok {shlex.quote(host)} --brand {shlex.quote(brand)}")
    try:
        added = read_sitemaps(conn, Fetcher(conn), brand)
    finally:
        conn.commit()
    if added:
        print(f"Added {added} pages from sitemaps.")


def main(argv):
    args = build_parser().parse_args(argv)
    if args.command == "migrate":
        from crawler.migrate import migrate
        migrate()
        return 0

    from crawler import brands
    from crawler.db import connect

    conn = connect()
    try:
        try:
            if args.command == "add":
                brands.add_brand(conn, args.brand)
                for url in args.urls:
                    brands.add_start_url(conn, args.brand, url)
                print(f"Added {args.brand}.")
                _discover(conn, args.brand)
            elif args.command == "ok":
                host = brands.approve_host(conn, args.host, brand=args.brand)
                print(f"OK to fetch from {host}.")
            elif args.command == "stop":
                brands.stop(conn, args.target)
                print(f"Stopped {args.target}.")
            elif args.command == "status":
                for row in brands.status(conn):
                    print(f"{row['brand']} ({row['state']}): {row['hosts_waiting']} hosts waiting, "
                          f"pages {row['pages_done']} done / {row['pages_waiting']} waiting / "
                          f"{row['pages_failed']} failed, {row['models_found']} models found"
                          + (f", {row['pages_submitting']} stuck while sending to AI" if row["pages_submitting"] else "")
                          + (f", {row['es_models_without_specs']} ENERGY STAR models with no specs"
                             if row["es_models_without_specs"] else ""))
            elif args.command == "run":
                from crawler.crawl import run
                from crawler.fetch import Fetcher
                from crawler.sitemaps import read_sitemaps
                fetcher = Fetcher(conn)
                try:
                    with conn.cursor() as cur:  # sitemaps are free: read them first, now that hosts are approved
                        cur.execute("SELECT name FROM brands WHERE status = 'active'"
                                    + (" AND name_key = %s" if args.brand else ""),
                                    (" ".join(args.brand.split()).lower(),) if args.brand else ())
                        names = [r[0] for r in cur.fetchall()]
                    for name in names:
                        read_sitemaps(conn, fetcher, name)
                        conn.commit()
                    summary = run(conn, fetcher, brand=args.brand, after_page=conn.commit)
                finally:
                    conn.commit()
                print("Done: " + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in summary.items()))
            elif args.command == "energystar":
                from crawler.energystar import pull
                from crawler.fetch import Fetcher
                try:
                    summary = pull(conn, Fetcher(conn), after_page=conn.commit)
                finally:
                    conn.commit()
                if summary["status"] == "unchanged":
                    print("ENERGY STAR has not changed since the last pull.")
                elif summary["status"] == "pulled":
                    print(f"Pulled {summary['rows']} rows: {summary['systems_added']} new systems, "
                          f"{summary['systems_changed']} changed, {summary['skipped_no_ahri']} skipped (no AHRI number).")
                else:
                    print(f"The ENERGY STAR pull did not finish ({summary['status']}). Try again later.", file=sys.stderr)
                    return 1
            elif args.command == "gapfill":
                from crawler.ai import AnthropicClient
                from crawler.gapfill import gap_fill
                try:
                    summary = gap_fill(conn, AnthropicClient(), brand=args.brand, after_search=conn.commit)
                finally:
                    conn.commit()
                print(f"Searched {summary['searched']} model families, {summary['found']} found documents.")
            elif args.command == "extract":
                from crawler.ai import AnthropicClient
                from crawler.extract import extract_pass
                try:
                    summary = extract_pass(conn, AnthropicClient(), brand=args.brand, after_document=conn.commit)
                finally:
                    conn.commit()
                print("Done: " + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in summary.items()))
                if summary["submitted"]:
                    print("Run ./crawl extract again later to collect the answers.")
            conn.commit()
        except RefusedHost as exc:
            print(f"Refused: {exc}", file=sys.stderr)
            return 1
        except (brands.UnknownTarget, brands.HostUnknown, ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
