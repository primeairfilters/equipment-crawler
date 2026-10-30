"""Command line entry: python -m crawler <command>."""
import argparse
import os
import shlex
import sys

from crawler.hosts import RefusedHost


def build_parser():
    parser = argparse.ArgumentParser(prog="./crawl")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="create or update the tables")
    sub.add_parser("prune", help="delete section builds that can never be live again (runs as the migrate role)")
    add = sub.add_parser("add", help="add a brand, with optional start web addresses")
    add.add_argument("brand")
    add.add_argument("urls", nargs="*")
    add.add_argument("--depth", type=int, help="how many links deep to follow from these addresses (default 3)")
    add.add_argument("--feed", action="store_true", help="the addresses are JSON document feeds, not web pages")
    ok = sub.add_parser("ok", help="allow fetching from a host")
    ok.add_argument("host")
    ok.add_argument("--brand")
    robots = ok.add_mutually_exclusive_group()
    robots.add_argument("--ignore-robots", dest="ignore_robots", action="store_true", default=None,
                        help="fetch this host even where its robots.txt says no (the 10 s gap and honest name stay)")
    robots.add_argument("--obey-robots", dest="ignore_robots", action="store_false", help="obey robots.txt again")
    browser = ok.add_mutually_exclusive_group()
    browser.add_argument("--browser", dest="use_browser", action="store_true", default=None,
                         help="render this host's pages in a headless browser (for JavaScript sites)")
    browser.add_argument("--no-browser", dest="use_browser", action="store_false", help="plain HTTP again")
    for effect in ("skip", "prefer"):
        words = sub.add_parser(effect, help=f"{effect} keywords: add, off, list")
        verbs = words.add_subparsers(dest="verb", required=True)
        for verb in ("add", "off"):
            one = verbs.add_parser(verb)
            one.add_argument("keyword")
            one.add_argument("--brand")
            if verb == "add":
                one.add_argument("--note")
        verbs.add_parser("list")
    judge = sub.add_parser("verdict", help="decide one address yourself: crawl or skip")
    judge.add_argument("url")
    judge.add_argument("decision", choices=["crawl", "skip"])
    secs = sub.add_parser("sections", help="cut tagged documents into sections and put them live (free, no AI)")
    secs.add_argument("brand", nargs="?")
    secs.add_argument("--workers", type=int, default=3,
                      help="PDFs read at once (3: 2.4x one worker; the 6 biggest manuals at once peaked at 1.1 GB of 2 GB)")
    vec = sub.add_parser("embed", help="give live sections their vectors (paid: OpenAI, counted in the monthly cap)")
    vec.add_argument("brand", nargs="?")
    question = sub.add_parser("ask", help="print the sections Breeze would get for a question (embeds it: paid, tiny)")
    question.add_argument("question")
    question.add_argument("--model", help="the unit's model number: its manuals and its series manuals first")
    question.add_argument("--manufacturer")
    question.add_argument("--top", type=int, default=8)
    question.add_argument("--language", default="en", help="prefer sections in this language (en, fr, es); 'any' for all")
    score = sub.add_parser("eval", help="score search on a file of questions with their expected manual pages")
    score.add_argument("file")
    score.add_argument("--top", type=int, default=5)
    look = sub.add_parser("preview", help="print the sections a PDF would get, without storing anything")
    look.add_argument("pdf", help="a file in the library (its hash, or <hash>.pdf) or a path")
    for name in ("hide", "unhide"):
        switch = sub.add_parser(name, help=f"{name} a manual or a whole manufacturer from Breeze")
        switch.add_argument("what", choices=["manual", "manufacturer"])
        switch.add_argument("target", help="the manual's id, or the manufacturer's name")
        switch.add_argument("--reason", required=True)
    tag = sub.add_parser("tag", help="set a manual's tags by hand; the AI never overwrites them")
    tag.add_argument("document", type=int, help="the manual's id")
    tag.add_argument("--doc-type")
    tag.add_argument("--equipment-type")
    tag.add_argument("--trade")
    different = sub.add_parser("not-duplicates", help="mark two manuals as different products; check stops listing them")
    different.add_argument("first", type=int)
    different.add_argument("second", type=int)
    different.add_argument("--reason", required=True)
    sub.add_parser("auto", help="the nightly run: crawl, tag, sections, embed, prune and check every active brand, then "
                                "report (one run at a time)")
    sub.add_parser("misses", help="sort what Breeze could not answer, and write the answered ones as eval questions")
    sub.add_parser("issues", help="write the documents the code could not handle as a Markdown fix list for Claude")
    sub.add_parser("uploads", help="take in the documents uploaded on the admin page now (the nightly run does it too)")
    store = sub.add_parser("library", help="the originals library: upload copies the local folder's originals to S3")
    store.add_argument("action", choices=["upload"])
    sub.add_parser("check", help="a read-only health report of what Breeze can see")
    stop = sub.add_parser("stop", help="stop a brand or a host")
    stop.add_argument("target")
    resume = sub.add_parser("resume", help="undo stop for a brand or a host")
    resume.add_argument("target")
    sub.add_parser("status", help="show where each brand is at")
    run = sub.add_parser("run", help="fetch pages")
    run.add_argument("--brand")
    run.add_argument("--max-pages", type=_positive, default=2000,
                     help="stop each start address after this many pages (default 2000; small for a test run)")
    sub.add_parser("energystar", help="pull the ENERGY STAR heat pump and AC list (about once a month)")
    gap = sub.add_parser("gapfill", help="search for documents for ENERGY STAR models that have no document yet")
    gap.add_argument("brand", nargs="?")
    reread = sub.add_parser("reread", help="read stored PDFs again that could not be read or need OCR (fetches nothing)")
    reread.add_argument("brand", nargs="?")
    reread.add_argument("--document", type=int, action="append",
                        help="read this document again by id (repeatable), e.g. one cut at an older page limit")
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


def _positive(text):
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text} is not a whole number") from None
    if n < 1:
        raise argparse.ArgumentTypeError("the limit must be at least 1")
    return n


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "es" if word.endswith("s") else "s")


def _missed_lines(row):
    """What the crawl did not reach and why, so 'no manual' can be told apart from 'stopped looking' (D59)."""
    lines = []
    if row.get("starts_at_limit"):
        lines.append(f"{_plural(row['starts_at_limit'], 'start address')} hit the page limit")
    if row.get("pdf_links_beyond_depth"):
        lines.append(f"{_plural(row['pdf_links_beyond_depth'], 'PDF link')} beyond depth")
    if row.get("waiting_for_ai"):
        lines.append(f"{_plural(row['waiting_for_ai'], 'link')} waiting for the AI")
    if row.get("unreadable"):
        lines.append(f"{_plural(row['unreadable'], 'document')} could not be read (./crawl reread tries again)")
    if row.get("needs_ocr"):
        lines.append(f"{_plural(row['needs_ocr'], 'manual')} with {_plural(row['pages_without_text'], 'page')} no text "
                     "was read from (OCR reads empty English pages; others are Arabic, blank, Spanish/French or "
                     "diagrams)")
    if row.get("failed_reasons"):  # no reason: failed before migration 0006 and not tried since
        lines.append("failed: " + ", ".join(f"{reason or 'reason not recorded'} {n}"
                                            for reason, n in row["failed_reasons"].items()))
    for reason, n in (row.get("keyword_skips") or {}).items():
        lines.append(f"{reason}: {n}")
    if row.get("suggested_keywords"):
        lines.append("suggested skip keywords: " + ", ".join(row["suggested_keywords"]))
    return lines


def _preview(name):
    """Print the sections a PDF would get. Reads only; nothing is stored."""
    import os
    from crawler.sections import build_sections
    from crawler.textextract import extract_layout
    from crawler import library
    if os.path.exists(name):
        with open(name, "rb") as handle:
            data = handle.read()
    else:
        data = library.read(name[:-4] if name.endswith(".pdf") else name)
        if data is None:
            print(f"No original {name} in the library.")
            return
    sections = build_sections(extract_layout(data))
    if not sections:
        print("No sections: the PDF has no readable text (a scan, or text drawn as shapes). It needs OCR.")
    for n, s in enumerate(sections, start=1):
        pages = f"p.{s.page_from}" + (f"-{s.page_to}" if s.page_to != s.page_from else "")
        flags = f" [{', '.join(s.flags)}]" if s.flags else ""
        print(f"=== {n}. {s.kind} | {pages} | {s.heading_path or '(no heading)'} | {len(s.body.split())} words{flags}")
        print(s.body)
        print()


def _hit_line(n, hit):
    pages = f"p.{hit.page_from}" if hit.page_from == hit.page_to else f"p.{hit.page_from}-{hit.page_to}"
    ways = ", ".join(f"{name} #{rank}" for name, rank in (("meaning", hit.vector_rank), ("words", hit.text_rank),
                                                          ("codes", hit.term_rank)) if rank)
    return (f"{n}. {hit.title} ({hit.doc_type}, document {hit.document_id}) {pages} · {hit.heading_path or '(no heading)'}"
            f"\n   found by {ways}" + (f"; distance {hit.distance:.3f}" if hit.distance is not None else ""))


def _search_command(conn, args):
    import json
    from crawler.embed import OpenAIEmbedder
    from crawler.search import AskCapped, ask, evaluate
    try:
        client = OpenAIEmbedder()
    except RuntimeError:
        print("No OpenAI key is set (OPENAI_API_KEY_CRAWLER), so the question could not be embedded.")
        return
    try:
        if args.command == "ask":
            found = ask(conn, client, args.question, model=args.model, manufacturer=args.manufacturer, top=args.top,
                        language=None if args.language == "any" else args.language)
            print(f"Searched: {found['level']}, {found['language'] or 'any language'}" + (f" (nothing relevant at: {', '.join(found['widened'])})"
                                                   if found["widened"] else ""))
            for n, hit in enumerate(found["results"], start=1):
                print(_hit_line(n, hit))
                print("   " + " ".join(hit.body.split())[:240])
            if not found["results"]:
                print("Nothing found.")
        else:
            with open(args.file, encoding="utf-8") as handle:
                cases = json.load(handle)
            report = evaluate(conn, client, cases, top=args.top)
            for case in report["cases"]:
                rank = f"#{case['rank']}" if case["rank"] else "missed"
                print(f"{rank:>7}  [{case['level']}] {case['question']}")
            hits = sum(1 for c in report["cases"] if c["rank"] and c["rank"] <= 5)
            print(f"top 5: {hits} of {len(report['cases'])} ({report['hit_at_5'] * 100:.0f}%); top 1: "
                  f"{report['hit_at_1'] * 100:.0f}%; MRR {report['mrr']:.3f}")
            _eval_moves(args.file, {c["question"]: c["rank"] for c in report["cases"]})
    except AskCapped as exc:
        print(f"Stopped: {exc}.")
    finally:
        conn.commit()


def _eval_moves(path, ranks):
    """Print each question whose rank changed since the last run of this file, then keep this run's ranks in the
    document library (DOCS_DIR/.eval/<file>.last.json; the repo is read-only in the container), so a score that moves
    shows which question moved. A library that cannot be written only loses the comparison."""
    import json
    name = os.path.splitext(os.path.basename(path))[0] + ".last.json"
    last_path = os.path.join(os.environ.get("DOCS_DIR") or os.path.dirname(os.path.abspath(path)), ".eval", name)
    try:
        with open(last_path, encoding="utf-8") as handle:
            last = json.load(handle)
    except (OSError, ValueError):
        last = None
    if last is None:
        print("first run of this file: ranks kept for the next run")
    else:
        def shown(rank):
            return f"#{rank}" if rank else "missed"
        moved = [f"  {shown(last[q])} -> {shown(r)}  {q}" for q, r in ranks.items() if q in last and last[q] != r]
        moved += [f"  new: {shown(r)}  {q}" for q, r in ranks.items() if q not in last]
        print("moved since the last run:\n" + "\n".join(moved) if moved else "no question moved since the last run")
    try:
        os.makedirs(os.path.dirname(last_path), exist_ok=True)
        with open(last_path, "w", encoding="utf-8") as handle:
            json.dump(ranks, handle, indent=1)
    except OSError as exc:
        print(f"could not keep this run's ranks ({exc.strerror}): the next run has nothing to compare with")


def _link_checker():
    """The AI link check, or None without a key (unsure links then wait as ask_ai)."""
    from crawler.ai import AnthropicClient
    from crawler.linkcheck import LinkChecker
    try:
        return LinkChecker(AnthropicClient())
    except RuntimeError:
        print("No AI key is set, so links the rules are unsure about will wait for a later run.")
        return None


def main(argv):
    args = build_parser().parse_args(argv)
    if args.command == "prune":
        from crawler.db import connect
        from crawler.kb import prune
        owner = connect("migrate")
        try:
            gone = prune(owner)
            owner.commit()
        finally:
            owner.close()
        print(f"Removed {_plural(gone['sections'], 'old section')}, {_plural(gone['terms'], 'term')} and "
              f"{_plural(gone['vectors'], 'vector')}.")
        return 0
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
                    brands.add_start_url(conn, args.brand, url, depth=args.depth, kind="feed" if args.feed else "page")
                print(f"Added {args.brand}.")
                _discover(conn, args.brand)
            elif args.command == "ok":
                host = brands.approve_host(conn, args.host, brand=args.brand, ignore_robots=args.ignore_robots,
                                           use_browser=args.use_browser)
                print(f"OK to fetch from {host}.")
                if args.ignore_robots is not None:
                    print(f"{host}: " + ("ignoring robots.txt (your call, D67); the 10 second gap and honest name stay."
                                         if args.ignore_robots else "obeying robots.txt."))
                if args.use_browser is not None:
                    print(f"{host}: " + ("pages are rendered in a headless browser." if args.use_browser
                                         else "plain HTTP, no browser."))
            elif args.command in ("skip", "prefer"):
                if args.verb == "list":
                    rows = brands.list_keywords(conn, args.command)
                    if not rows:
                        print(f"No {args.command} keywords.")
                    for keyword, active, source, brand, host, note in rows:
                        where = f"{host}/{keyword}/" if host else keyword
                        print(f"{where} ({source}, {brand}{'' if active else ', off'})" + (f": {note}" if note else ""))
                else:
                    word = brands.set_keyword(conn, args.keyword, args.command, brand=args.brand,
                                              active=args.verb == "add", note=getattr(args, "note", None))
                    print(f"{args.command.capitalize()} keyword {word} " + ("on." if args.verb == "add" else "off."))
            elif args.command == "sections":
                from crawler.kb import build_and_publish
                brand_id = brands.find_brand(conn, args.brand) if args.brand else None
                if args.brand and brand_id is None:
                    raise brands.UnknownTarget(f"No brand called {args.brand}.")
                try:
                    summary = build_and_publish(conn, brand_id=brand_id, after_document=conn.commit,
                                                workers=args.workers)
                finally:
                    conn.commit()
                print(f"Built sections for {summary['built']} documents: {summary['live']} live, "
                      f"{summary['held']} held back (hidden, switched off or not equipment), {summary['empty']} with no text; "
                      f"{summary['unchanged']} unchanged"
                      + (f", {len(summary['kept'])} kept as they are" if summary["kept"] else "") + ".")
                for document_id, why in summary["kept"]:
                    print(f"Document {document_id}: the original could not be read ({why}); its live sections stay "
                          "as they are and the next run tries again.")
                for document_id, why in summary["fallback"]:
                    print(f"Document {document_id}: the original could not be read ({why}), so its sections come from "
                          "the stored text, without headings or tables.")
            elif args.command == "embed":
                from crawler.embed import OpenAIEmbedder, embed_pass
                brand_id = brands.find_brand(conn, args.brand) if args.brand else None
                if args.brand and brand_id is None:
                    raise brands.UnknownTarget(f"No brand called {args.brand}.")
                try:
                    client = OpenAIEmbedder()
                except RuntimeError:
                    print("No OpenAI key is set (OPENAI_API_KEY_CRAWLER), so nothing was embedded.")
                    client = None
                if client is not None:
                    try:
                        summary = embed_pass(conn, client, brand_id=brand_id, after_request=conn.commit)
                    finally:
                        conn.commit()
                    print(f"Embedded {summary['embedded']} sections in {summary['requests']} requests "
                          f"({summary['tokens']} tokens, ${summary['cost']:.6f}); {summary['reused']} reused a vector "
                          "for the same text.")
                    if summary["capped"]:
                        print("Stopped at the monthly AI cap (AI_MONTHLY_CAP_USD); the rest wait for next month or a "
                              "higher cap.")
                    if summary["error"]:
                        print(f"Stopped: {summary['error']}. What was done is kept; run it again to go on.")
            elif args.command in ("ask", "eval"):
                _search_command(conn, args)
            elif args.command == "preview":
                _preview(args.pdf)
            elif args.command in ("hide", "unhide"):
                import getpass
                from crawler import kb
                target_type = "document" if args.what == "manual" else "manufacturer"
                switch = kb.hide if args.command == "hide" else kb.unhide
                switch(conn, target_type, args.target, reason=args.reason, by_whom=getpass.getuser())
                print(f"{args.command.capitalize()}: {args.what} {args.target}.")
            elif args.command == "tag":
                from crawler.kb import set_tags
                done = set_tags(conn, args.document, doc_type=args.doc_type, equipment_type=args.equipment_type,
                                trade=args.trade)
                print(f"Document {args.document}: " + ", ".join(f"{k} {v}" for k, v in done.items())
                      + " (set by hand; the AI will not change these).")
            elif args.command == "not-duplicates":
                import getpass
                from crawler.kb import not_duplicates
                first, second = not_duplicates(conn, args.first, args.second, reason=args.reason,
                                               by_whom=getpass.getuser())
                print(f"Marked {first} and {second} as different manuals; check will not list them as duplicates.")
            elif args.command == "auto":
                from crawler.auto import run_auto
                from crawler.db import connect as connect_as
                from crawler.embed import OpenAIEmbedder
                from crawler.fetch import Fetcher
                try:
                    from crawler.ai import AnthropicClient
                    ai_client = AnthropicClient()
                except RuntimeError:
                    print("No Anthropic key is set, so tonight's documents wait untagged.")
                    ai_client = None
                try:
                    embedder = OpenAIEmbedder()
                except RuntimeError:
                    print("No OpenAI key is set, so new sections wait without vectors.")
                    embedder = None
                summary = run_auto(conn, fetcher=Fetcher(conn), link_checker=_link_checker(), ai_client=ai_client,
                                   embedder=embedder, owner_connect=lambda: connect_as("migrate"), commit=conn.commit)
                if summary["state"] == "skipped":
                    print("Another run is still going; this one was skipped.")
                else:
                    with conn.cursor() as cur:
                        cur.execute("SELECT report FROM runs WHERE id = %s", (summary["run_id"],))
                        print(cur.fetchone()[0], end="")
            elif args.command == "misses":
                import json
                from crawler.misses import eval_cases, sort_misses, uncovered_brands
                try:
                    from crawler.ai import AnthropicClient
                    client = AnthropicClient()
                except RuntimeError:
                    client = None
                try:
                    from crawler.embed import OpenAIEmbedder
                    embedder = OpenAIEmbedder()
                except RuntimeError:
                    embedder = None
                done = sort_misses(conn, client=client, embedder=embedder, after_miss=conn.commit)
                conn.commit()
                print(", ".join(f"{k.replace('_', ' ')} {v}" for k, v in done.items()))
                for brand, count in uncovered_brands(conn):
                    print(f"  not covered: {brand} ({count})")
                cases = eval_cases(conn)
                folder = os.environ.get("DOCS_DIR")
                if folder and cases:
                    with open(os.path.join(folder, "eval-misses.json"), "w", encoding="utf-8") as handle:
                        json.dump(cases, handle, indent=2)
                    print(f"{len(cases)} eval question(s) from Breeze misses in eval-misses.json in the documents "
                          "folder; score them with ./crawl eval /docs/eval-misses.json")
            elif args.command == "issues":
                from crawler.issues import report, scan
                scan(conn)
                text = report(conn)
                conn.commit()
                folder = os.environ.get("DOCS_DIR")
                if folder:
                    with open(os.path.join(folder, "parse-issues.md"), "w", encoding="utf-8") as handle:
                        handle.write(text)
                    print("Wrote parse-issues.md in the documents folder (~/Documents/equipment-crawler-docs on the Mac).")
                print(text, end="")
            elif args.command == "uploads":
                from crawler.control import sync
                from crawler.uploads import process_uploads
                sync(conn)
                done = process_uploads(conn, after_upload=conn.commit)
                conn.commit()
                print(f"Uploads: added {done['added']}, already had {done['already_had']}, rejected {done['rejected']}. "
                      "New ones are tagged and searchable after the next nightly run (or ./crawl auto).")
            elif args.command == "library":
                from crawler import library
                if not os.environ.get("DOCS_BUCKET"):
                    raise ValueError("DOCS_BUCKET is not set (~/.config/tracforce-crawler/aws.env), so there is no S3 "
                                     "library to upload to")
                counts = library.upload_local()
                print(f"Originals: {counts['uploaded']} uploaded, {counts['already there']} already in S3, "
                      f"{counts['failed']} failed (each checked by S3 against its SHA-256).")
            elif args.command == "check":
                from crawler.kb import check
                report = check(conn)
                problems = 0
                for key, words in (("live_without_sections", "live manuals with no live sections"),
                                   ("hidden_but_live", "hidden or switched-off manuals still live"),
                                   ("live_but_not_current", "manuals with a non-current build live"),
                                   ("likely_duplicates", "likely duplicate pairs (same maker, title and models)")):
                    if report[key]:
                        problems += 1
                        print(f"{len(report[key])} {words}: {report[key][:20]}")
                print(f"{report['needs_ocr']} manuals have pages with no text even after OCR (blank, drawings, "
                      f"Spanish/French). {report['live_without_vectors']} live sections have no vector yet "
                      "(./crawl embed).")
                if report["missing_originals"]:
                    problems += 1
                    print(f"{len(report['missing_originals'])} live manuals whose original PDF is not in the library: "
                          f"{report['missing_originals'][:20]}")
                if report["marked_different"]:
                    print(f"{_plural(report['marked_different'], 'pair')} of manuals marked as different products "
                          "(not listed as duplicates).")
                if report["user_tags"]:
                    print("tags set by hand: " + "; ".join(
                        f"{doc} ({', '.join(f'{k} {v}' for k, v in fields.items())})" for doc, fields in report["user_tags"]))
                for maker, trade, documents, sections in report["counts"]:
                    print(f"  {maker} / {trade}: {documents} live manuals, {sections} live sections")
                print("No problems found." if not problems else f"{problems} kinds of problem found.")
            elif args.command == "verdict":
                changed = brands.verdict(conn, args.url, args.decision)
                print(f"{args.decision.capitalize()}: {args.url} ({changed} page row{'s' if changed != 1 else ''}).")
            elif args.command == "reread":
                from crawler.documents import reread
                brand_id = brands.find_brand(conn, args.brand) if args.brand else None
                if args.brand and brand_id is None:
                    raise brands.UnknownTarget(f"No brand called {args.brand}.")
                result = reread(conn, brand_id, after_document=conn.commit, documents=args.document)
                conn.commit()
                print(f"Read {_plural(result['read'], 'document')} again; {result['unreadable']} still could not be read.")
            elif args.command == "stop":
                brands.stop(conn, args.target)
                print(f"Stopped {args.target}.")
            elif args.command == "resume":
                brands.resume(conn, args.target)
                print(f"Resumed {args.target}.")
            elif args.command == "status":
                for row in brands.status(conn):
                    print(f"{row['brand']} ({row['state']}): {row['hosts_waiting']} hosts waiting, "
                          f"pages {row['pages_done']} done / {row['pages_waiting']} waiting / "
                          f"{row['pages_failed']} failed, {row['models_found']} models found"
                          + (f", {row['pages_submitting']} stuck while sending to AI" if row["pages_submitting"] else "")
                          + f", {row['documents']} documents"
                          + (f" ({row['untagged']} not tagged yet)" if row["untagged"] else "")
                          + (f", {row['es_models_without_documents']} ENERGY STAR models with no documents"
                             if row["es_models_without_documents"] else ""))
                    for line in _missed_lines(row):
                        print("  " + line)
            elif args.command == "run":
                from crawler.crawl import run
                from crawler.fetch import Fetcher
                from crawler.sitemaps import read_sitemaps
                fetcher = Fetcher(conn)
                checker = _link_checker()
                try:
                    with conn.cursor() as cur:  # sitemaps are free: read them first, now that hosts are approved
                        cur.execute("SELECT name FROM brands WHERE status = 'active'"
                                    + (" AND name_key = %s" if args.brand else ""),
                                    (" ".join(args.brand.split()).lower(),) if args.brand else ())
                        names = [r[0] for r in cur.fetchall()]
                    for name in names:
                        read_sitemaps(conn, fetcher, name)
                        conn.commit()
                    summary = run(conn, fetcher, brand=args.brand, max_pages=args.max_pages, after_page=conn.commit,
                                  link_checker=checker)
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
