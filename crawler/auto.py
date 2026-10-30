"""P17 (D73; Q19, Q20): `./crawl auto`, the nightly run (2 AM, launchd on the Mac for now).

One pass for every active brand, highest admin-page priority first, with nobody at the keyboard:
0. The admin page's brands (control.brands) are read: new ones linked, pauses, budgets and switches copied (P19). A
   brand never discovered and with no start address (one typed on the page) is asked about once and its own site
   approved (crawler/control.py). Documents uploaded on the page are taken in (crawler/uploads.py, D84).
1. Crawl: sitemaps, then pages that are new or due again (rechecks, failed pages whose wait is over), up to
   AUTO_MAX_PAGES per start address.
2. AI tagging: submit, then poll every poll_seconds until the brand's answers are collected or wait_seconds pass
   (what is still out is collected by the next run).
2b. Coverage (P20, crawler/coverage.py): the public model list once a month, gap-fill web searches and the site's
   own search for families with no manual (found manuals are crawled the next night), coverage and phase.
3. Sections, embeddings, prune (as the migrate role), Breeze misses sorted (P21, crawler/misses.py), parse issues (crawler/issues.py: retried after a code change
   before the crawl, scanned and written as a Markdown fix list after the build), check.
4. A report: a crawl.runs row (the admin page reads it) and DOCS_DIR/.runs/run-<id>.txt.

One run at a time: a PostgreSQL advisory lock held for the run (it goes with the session, so a crashed run never
blocks the next); a row a crashed run left 'running' is marked 'interrupted'. Every step can be stopped and started
again (pages due, batches out, sections and vectors missing are all read back from the database), so the next run
finishes what an interrupted one left.

Paid steps (the AI link check and tagging) stop for a brand that has spent its monthly budget (brands.monthly_budget_usd,
$5 by default, Q25); the monthly cap (AI_MONTHLY_CAP_USD) still holds for everything. Free steps always run. A
stopped brand (`./crawl stop`, the admin page's pause) is skipped. One step failing is reported and the run goes on.
"""
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal

LOCK_KEY = 7_316_002_017  # pg advisory lock: one `auto` run at a time
AUTO_MAX_PAGES = 200  # per start address, per night
WAIT_SECONDS = 2 * 3600  # how long a run waits for the AI's tagging answers
POLL_SECONDS = 300


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def run_auto(conn, *, fetcher, link_checker=None, ai_client=None, embedder=None, owner_connect=None, commit=None,
             sleep=time.sleep, now=None, max_pages=AUTO_MAX_PAGES, wait_seconds=WAIT_SECONDS,
             poll_seconds=POLL_SECONDS, public_lists=True):
    """Returns the run's summary: state ('done' or 'skipped'), run_id, brands {name: {...}}, documents_added,
    documents_retired, embedded, ai_spent, needs_you [lines], interrupted_runs."""
    commit = commit or (lambda: None)
    if not _rows(conn, "SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,))[0][0]:
        run_id = _rows(conn, "INSERT INTO runs (state, finished_at, summary) VALUES ('skipped', now() AT TIME ZONE "
                             "'UTC', %s) RETURNING id", (json.dumps({"why": "another run is still going"}),))[0][0]
        commit()
        return {"state": "skipped", "run_id": run_id, "brands": {}, "needs_you": []}
    try:
        return _run(conn, fetcher, link_checker, ai_client, embedder, owner_connect, commit, sleep,
                    now or _utcnow(), max_pages, wait_seconds, poll_seconds, public_lists)
    finally:
        _rows(conn, "SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


def _run(conn, fetcher, link_checker, ai_client, embedder, owner_connect, commit, sleep, now, max_pages, wait_seconds,
         poll_seconds, public_lists):
    interrupted = len(_rows(conn, "UPDATE runs SET state = 'interrupted', finished_at = now() AT TIME ZONE 'UTC' "
                                  "WHERE state = 'running' RETURNING id"))
    run_id = _rows(conn, "INSERT INTO runs (state) VALUES ('running') RETURNING id")[0][0]
    first_document = _rows(conn, "SELECT COALESCE(MAX(id), 0) FROM documents")[0][0]
    retired_before = _rows(conn, "SELECT COUNT(*) FROM documents WHERE state = 'retired'")[0][0]
    first_call = _rows(conn, "SELECT COALESCE(MAX(id), 0) FROM ai_calls")[0][0]
    commit()
    summary = {"state": "done", "run_id": run_id, "brands": {}, "needs_you": [], "interrupted_runs": interrupted,
               "embedded": 0, "pruned": None, "hosts": []}

    def step(name, work):
        try:
            return work()
        except Exception as exc:  # one step failing is reported; the rest of the run goes on
            conn.rollback()
            summary["needs_you"].append(f"{name} failed: {exc.__class__.__name__}: {str(exc)[:200]}")
            return None
        finally:
            commit()

    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    step("reading the admin page's brands", lambda: _sync(conn))
    summary["uploads"] = step("uploads", lambda: _uploads(conn, commit)) or {"added": 0, "already_had": 0, "rejected": 0}
    retried = step("retrying parse issues after code changes", lambda: _retry_issues(conn, commit)) or {}
    for brand_id, name, budget, discovered in _rows(
            conn, "SELECT b.id, b.name, b.monthly_budget_usd, b.discovered_at IS NOT NULL OR EXISTS (SELECT 1 FROM "
                  "start_urls s WHERE s.brand_id = b.id) FROM brands b "
                  "LEFT JOIN control.brands c ON c.crawl_brand_id = b.id WHERE b.status = 'active' "
                  "ORDER BY COALESCE(c.priority, 0) DESC, b.id"):
        spent = _rows(conn, "SELECT COALESCE(SUM(cost_usd), 0) FROM ai_calls WHERE brand_id = %s AND created_at >= %s",
                      (brand_id, month))[0][0]
        over = Decimal(spent) >= Decimal(budget)
        row = summary["brands"][name] = {"fetched": 0, "not_modified": 0, "failed": 0, "refused": 0, "tagged": 0,
                                         "waiting_for_ai": 0, "over_budget": over, "spent_this_month": float(spent)}
        if over:
            summary["needs_you"].append(f"{name} has spent its ${budget} AI budget this month (${spent:.2f}): crawling "
                                        "goes on, tagging waits for next month or a higher budget")
        if not discovered and not over and ai_client is not None:
            step(f"finding where {name} publishes", lambda: _discover(conn, ai_client, brand_id, name, summary))
        step(f"judging hosts for {name}", lambda: _hosts(conn, ai_client, brand_id, name, over, summary))
        crawled = step(f"crawling {name}", lambda: _crawl(conn, fetcher, name, max_pages,
                                                          None if over else link_checker, commit))
        if crawled:
            row.update({k: crawled.get(k, 0) for k in ("fetched", "not_modified", "failed", "refused")})
        if not over and ai_client is not None:
            row["tagged"] = step(f"tagging {name}", lambda: _tag(conn, ai_client, brand_id, name, commit, sleep,
                                                                  wait_seconds, poll_seconds)) or 0
        step(f"host trials for {name}", lambda: _trials(conn, brand_id, name, summary))
        row["coverage"] = step(f"coverage for {name}", lambda: _coverage(
            conn, fetcher if public_lists else None, None if over else ai_client, brand_id, name, first_document,
            run_id, now, commit))
        row["waiting_for_ai"] = _rows(conn, "SELECT COUNT(*) FROM pages WHERE brand_id = %s AND ai_batch_id IS NOT NULL",
                                      (brand_id,))[0][0]

    if ai_client is not None:  # uploads with no brand, or under a paused brand, sit where the loop above never reaches
        for brand_id, name in _rows(conn, "SELECT DISTINCT b.id, b.name FROM control.uploads u JOIN pages p ON p.id = "
                                          "u.page_id JOIN brands b ON b.id = p.brand_id WHERE b.status <> 'active' AND "
                                          "(p.extraction_status = 'waiting' OR p.ai_batch_id IS NOT NULL) ORDER BY b.id"):
            step(f"tagging uploads of {name}", lambda: _tag(conn, ai_client, brand_id, name, commit, sleep, wait_seconds,
                                                            poll_seconds))

    from crawler.kb import build_and_publish, check
    built = step("sections", lambda: build_and_publish(conn, after_document=commit))
    if embedder is not None:
        from crawler.embed import embed_pass
        embedded = step("embedding", lambda: embed_pass(conn, embedder, after_request=commit))
        summary["embedded"] = (embedded or {}).get("embedded", 0)
        if embedded and embedded.get("capped"):
            summary["needs_you"].append("embedding stopped at the monthly AI cap")
    if owner_connect is not None:
        summary["pruned"] = step("prune", lambda: _prune(owner_connect))
    sorted_misses = step("sorting Breeze misses", lambda: _misses(conn, ai_client, embedder, now, commit)) or {}
    summary["misses"] = sorted_misses
    for brand, count in sorted_misses.get("uncovered", []):
        summary["needs_you"].append(f"Breeze was asked about {brand} {count} time(s), a brand the crawler does not "
                                    "have: add it on the admin page if you want it")
    found = step("parse issues", lambda: _scan_issues(conn, now)) or {}
    summary["issues"] = {"open": found.get("open", 0), "new": found.get("new", 0),
                         "retried": retried.get("retried", 0), "fixed": retried.get("fixed", 0) + found.get("fixed", 0)}
    report = step("check", lambda: check(conn)) or {}
    for key, words in (("live_without_sections", "live manuals with no live sections"),
                       ("hidden_but_live", "hidden manuals still live"),
                       ("live_but_not_current", "manuals with an old build live"),
                       ("likely_duplicates", "likely duplicate pairs"),
                       ("missing_originals", "live manuals whose original is missing")):
        if report.get(key):
            summary["needs_you"].append(f"check: {len(report[key])} {words}: {report[key][:10]}")
    summary["sections_built"] = (built or {}).get("built", 0)
    summary["documents_added"] = _rows(conn, "SELECT COUNT(*) FROM documents WHERE id > %s", (first_document,))[0][0]
    summary["documents_retired"] = max(0, _rows(conn, "SELECT COUNT(*) FROM documents WHERE state = 'retired'")[0][0]
                                       - retired_before)
    summary["ai_spent"] = float(_rows(conn, "SELECT COALESCE(SUM(cost_usd), 0) FROM ai_calls WHERE id > %s",
                                      (first_call,))[0][0])
    summary["ai_spent_month"] = float(_rows(conn, "SELECT COALESCE(SUM(cost_usd), 0) FROM ai_calls WHERE created_at >= %s",
                                            (month,))[0][0])
    text = _report(summary, now)
    _rows(conn, "UPDATE runs SET state = 'done', finished_at = now() AT TIME ZONE 'UTC', summary = %s, report = %s "
                "WHERE id = %s", (json.dumps(summary, default=str), text, run_id))
    commit()
    _write_report(run_id, text)
    return summary


GAP_SEARCHES_PER_RUN = 20  # per brand per night; gapfill's monthly cap (200) and the AI cap hold too


def _coverage(conn, fetcher, client, brand_id, name, first_document, run_id, now, commit):
    """P20: the brand's public model list (monthly), web searches and site searches for families with no manual,
    then its coverage and phase. Paid searches only with a client (not over budget)."""
    from crawler.coverage import measure, pull_models, site_search
    if fetcher is not None:
        pull_models(conn, fetcher, brand_id, name, now=now)
        commit()
    searched = 0
    if client is not None and hasattr(client, "find_documents"):
        from crawler.gapfill import gap_fill
        searched = gap_fill(conn, client, brand=name, now=now, limit=GAP_SEARCHES_PER_RUN, after_search=commit)["searched"]
    sites = site_search(conn, brand_id, now=now)
    new = _rows(conn, "SELECT COUNT(DISTINCT u.document_id) FROM document_urls u JOIN pages p ON p.id = u.page_id "
                      "WHERE p.brand_id = %s AND u.document_id > %s", (brand_id, first_document))[0][0]
    return measure(conn, brand_id, run_id=run_id, new_documents=new, gap_searches=searched, site_searches=sites)


def _sync(conn):
    from crawler.control import sync
    sync(conn)


def _discover(conn, client, brand_id, name, summary):
    """A brand never discovered (one the admin page just added): find where it publishes, approve its own site (P19)."""
    from crawler.control import discover_brand
    decided, waiting = discover_brand(conn, client, brand_id, name)
    for host, state, reason in decided:
        _host_line(summary, name, host, state, reason)
    for host in waiting:
        summary["needs_you"].append(f"{name}: {host} (found by discovery) waits for your approval (automatic "
                                    "approval is off)")


def _misses(conn, client, embedder, now, commit):
    from crawler.misses import sort_misses, uncovered_brands
    done = sort_misses(conn, client=client, embedder=embedder, now=now, after_miss=commit)
    newly = done["uncovered_brand"]
    return {**done, "uncovered": uncovered_brands(conn) if newly else []}


def _retry_issues(conn, commit):
    from crawler.issues import retry
    return retry(conn, after_document=commit)


def _scan_issues(conn, now):
    """Scan, then write the fix list. Counts: open, new (first seen this run), fixed (by this scan)."""
    from crawler.issues import report, scan
    start = _rows(conn, "SELECT now() AT TIME ZONE 'UTC'")[0][0]
    fixed_before = _rows(conn, "SELECT COUNT(*) FROM parse_issues WHERE state = 'fixed'")[0][0]
    scan(conn)
    report(conn, now)
    return {"open": _rows(conn, "SELECT COUNT(*) FROM parse_issues WHERE state = 'open'")[0][0],
            "new": _rows(conn, "SELECT COUNT(*) FROM parse_issues WHERE first_seen_at >= %s", (now,))[0][0],
            "fixed": _rows(conn, "SELECT COUNT(*) FROM parse_issues WHERE state = 'fixed'")[0][0] - fixed_before,
            "scanned_at": start}


def _uploads(conn, commit):
    from crawler.uploads import process_uploads
    return process_uploads(conn, after_upload=commit)


def _hosts(conn, client, brand_id, name, over_budget, summary):
    """Settle last night's trials, then decide the hosts this brand's pages point at (P18). With judging off, the
    hosts waiting are listed for the user instead."""
    from crawler import hostjudge
    if not hostjudge.enabled(conn, brand_id):
        for host, evidence in sorted(hostjudge.candidates(conn, brand_id).items()):
            summary["needs_you"].append(f"{name}: {host} ({evidence['pdf_links']} PDF links) waits for your approval "
                                        "(automatic approval is off)")
        return
    _trials(conn, brand_id, name, summary)
    for host, state, reason in hostjudge.judge_hosts(conn, client, brand_id, use_ai=not over_budget):
        _host_line(summary, name, host, state, reason)


def _trials(conn, brand_id, name, summary):
    from crawler.hostjudge import evaluate_trials
    for host, state, reason in evaluate_trials(conn, brand_id):
        _host_line(summary, name, host, state, reason)


def _host_line(summary, name, host, state, reason):
    words = {"own": "approved as its own site", "trial": "approved for a trial (PDFs only)", "confirmed": "confirmed (PDFs only)",
             "stopped": "stopped after its trial", "rejected": "not approved"}[state]
    summary["hosts"].append(f"{name}: {host} {words}: {reason}")
    if state == "stopped":
        summary["needs_you"].append(f"{name}: {host} was stopped after its trial ({reason}); `./crawl ok {host} "
                                    f"--brand \"{name}\"` approves it anyway")


def _crawl(conn, fetcher, name, max_pages, link_checker, commit):
    from crawler.crawl import run
    from crawler.sitemaps import read_sitemaps
    read_sitemaps(conn, fetcher, name)
    commit()
    return run(conn, fetcher, brand=name, max_pages=max_pages, after_page=commit, link_checker=link_checker)


def _tag(conn, client, brand_id, name, commit, sleep, wait_seconds, poll_seconds):
    """Submit this brand's documents for tagging, then poll until the answers are in or the wait is over."""
    from crawler.extract import extract_pass
    tagged, waited = 0, 0
    while True:
        done = extract_pass(conn, client, brand=name, after_document=commit)
        tagged += done.get("collected", 0)
        commit()
        out = _rows(conn, "SELECT COUNT(*) FROM pages WHERE brand_id = %s AND ai_batch_id IS NOT NULL", (brand_id,))[0][0]
        if not out or waited >= wait_seconds:
            return tagged
        sleep(poll_seconds)
        waited += poll_seconds


def _prune(owner_connect):
    from crawler.kb import prune
    owner = owner_connect()
    try:
        gone = prune(owner)
        owner.commit()
        return gone
    finally:
        owner.close()


def _report(summary, now):
    lines = [f"Crawler run {summary['run_id']}, started {now:%Y-%m-%d %H:%M} UTC"]
    if summary.get("interrupted_runs"):
        lines.append(f"(finishing what {summary['interrupted_runs']} interrupted run(s) left)")
    for name, b in summary["brands"].items():
        lines.append(f"{name}: fetched {b['fetched']}, unchanged {b['not_modified']}, failed {b['failed']}, refused "
                     f"{b['refused']}; tagged {b['tagged']}, still with the AI {b['waiting_for_ai']}; AI this month "
                     f"${b['spent_this_month']:.2f}" + (" (over budget)" if b["over_budget"] else ""))
        cov = b.get("coverage")
        if cov:
            share = f"{cov['covered']} of {cov['families']} listed model families have a manual" if cov["families"] \
                else "no public model list names its models"
            lines.append(f"  coverage: {share}; searches {cov['gap_searches']} web, {cov['site_searches']} site; "
                         f"phase {cov['phase'].replace('_', ' ')}")
    up = summary.get("uploads") or {}
    if any(up.values()):
        lines.append(f"Uploads: added {up['added']}, already had {up['already_had']}, rejected {up['rejected']}")
    lines.append(f"Library: manuals added {summary['documents_added']}, retired by newer versions "
                 f"{summary['documents_retired']}, sections built {summary['sections_built']}, embedded "
                 f"{summary['embedded']}" + (f", pruned {summary['pruned']['sections']} old sections"
                                              if summary.get("pruned") else ""))
    misses = summary.get("misses") or {}
    if any(misses.get(k) for k in ("uncovered_brand", "no_manual", "search_missed", "not_in_manuals", "repeat")):
        lines.append(f"Breeze misses sorted: {misses['no_manual']} model(s) with no manual (searched), "
                     f"{misses['search_missed']} answered by a manual search missed (new eval questions), "
                     f"{misses['not_in_manuals']} not in the manuals, {misses['uncovered_brand']} on brands not "
                     f"covered, {misses['repeat']} repeats; {misses['waiting']} wait for tomorrow")
    issues = summary.get("issues") or {}
    if issues.get("open") or issues.get("fixed"):
        lines.append(f"Parse issues: {issues['open']} open ({issues['new']} new this run), {issues['fixed']} fixed by "
                     "code changes; `./crawl issues` writes the fix list for Claude")
    if summary.get("hosts"):
        lines.append("Hosts decided by the crawler:")
        lines += [f"- {line}" for line in summary["hosts"]]
    lines.append(f"AI spent: ${summary['ai_spent']:.4f} this run, ${summary['ai_spent_month']:.2f} this month")
    lines.append("Needs you: " + ("nothing" if not summary["needs_you"] else ""))
    lines += [f"- {line}" for line in summary["needs_you"]]
    return "\n".join(lines) + "\n"


def _write_report(run_id, text):
    folder = os.environ.get("DOCS_DIR")
    if not folder:
        return
    try:
        os.makedirs(os.path.join(folder, ".runs"), exist_ok=True)
        with open(os.path.join(folder, ".runs", f"run-{run_id}.txt"), "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError:
        pass  # the crawl.runs row has the same report
