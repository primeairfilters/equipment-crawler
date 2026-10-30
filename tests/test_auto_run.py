"""P17 (D73; Q19, Q20): `./crawl auto`, the nightly run. One pass for every active brand: crawl, AI tagging until
collected, sections, embeddings, prune, check, and a report (a crawl.runs row for the admin page and a text file in
DOCS_DIR/.runs/). One run at a time; a run stopped part way is finished by the next; the monthly cap and each brand's
budget stop the paid steps while the free ones still run; a stopped (paused) brand is skipped.

Fake web server, fake AI, fake embedder, `_testing`, one rolled-back transaction per test. No network and no key."""
import os
from decimal import Decimal
from pathlib import Path

from fakeai import FakeAI
from p11support import Checker, serve, serve_pdf, start, world  # noqa: F401  (world is a fixture)
from p3support import q
from test_p11b_tagging import tags
from test_p14_search import BagEmbedder


def auto(world, **kwargs):
    from crawler.auto import run_auto
    kwargs.setdefault("ai_client", FakeAI(lambda r: tags()))
    kwargs.setdefault("embedder", BagEmbedder())
    kwargs.setdefault("link_checker", Checker())
    kwargs.setdefault("public_lists", False)  # no ENERGY STAR pull (tests/test_coverage.py has its own)
    return run_auto(world.conn, fetcher=world.fetcher(), sleep=lambda s: None, **kwargs)


def a_brand_with_a_manual(world):
    serve(world, "/start", "/manual.pdf")
    serve_pdf(world, "/manual.pdf")
    start(world)


def test_one_run_takes_a_brand_from_its_start_page_to_searchable_sections(world):
    a_brand_with_a_manual(world)
    summary = auto(world)
    assert summary["state"] == "done"
    brand = summary["brands"][world.brand]
    assert brand["fetched"] >= 2 and brand["tagged"] == 1
    assert summary["documents_added"] == 1 and summary["embedded"] > 0
    assert q(world.conn, "SELECT COUNT(*) FROM sections s JOIN section_vectors_te3s_1536 v ON v.section_id = s.id "
                         "WHERE s.is_live")[0][0] > 0


def test_the_run_is_recorded_for_the_admin_page_and_written_as_a_text_report(world):
    a_brand_with_a_manual(world)
    summary = auto(world)
    row = q(world.conn, "SELECT state, finished_at IS NOT NULL, summary->>'documents_added', report FROM runs WHERE id = %s",
            (summary["run_id"],))[0]
    assert row[:3] == ("done", True, "1")
    report = Path(os.environ["DOCS_DIR"], ".runs", f"run-{summary['run_id']}.txt").read_text()
    assert report == row[3]
    for words in (world.brand, "fetched", "manuals added 1", "AI spent", "Needs you: nothing"):
        assert words in report


def test_a_second_run_while_one_is_running_is_skipped(world):
    from support import connect
    a_brand_with_a_manual(world)
    holder = connect("runtime", autocommit=True)
    try:
        holder.execute("SELECT pg_advisory_lock(%s)", (__import__("crawler.auto").auto.LOCK_KEY,))
        summary = auto(world)
        assert summary["state"] == "skipped" and world.site.page_hits() == []
    finally:
        holder.close()  # the lock goes with the session, as when a crashed run's process ends


def test_a_run_left_running_by_a_crash_is_marked_interrupted_and_the_next_run_finishes_the_work(world):
    a_brand_with_a_manual(world)
    q(world.conn, "INSERT INTO runs (state, started_at) VALUES ('running', now() AT TIME ZONE 'UTC') RETURNING id")
    summary = auto(world)
    assert summary["state"] == "done" and summary["interrupted_runs"] == 1
    assert q(world.conn, "SELECT COUNT(*) FROM runs WHERE state = 'running'")[0][0] == 0
    again = auto(world)  # nothing done twice: the page is not due, the document is tagged and live already
    assert again["brands"][world.brand]["fetched"] == 0 and again["documents_added"] == 0
    assert again["brands"][world.brand]["tagged"] == 0


def test_a_stopped_brand_is_skipped(world):
    from crawler.brands import stop
    a_brand_with_a_manual(world)
    stop(world.conn, world.brand)
    summary = auto(world)
    assert world.brand not in summary["brands"] and world.site.page_hits() == []


def test_a_brand_over_its_budget_gets_the_free_steps_but_no_paid_ones(world):
    a_brand_with_a_manual(world)
    brand_id = q(world.conn, "SELECT id FROM brands WHERE name = %s", (world.brand,))[0][0]
    q(world.conn, "UPDATE control.brands SET monthly_budget_usd = 1 WHERE crawl_brand_id = %s", (brand_id,))  # the admin page
    q(world.conn, "INSERT INTO ai_calls (purpose, brand_id, model, cost_usd) VALUES ('extract', %s, 'claude-haiku-5-5', 1.5)",
      (brand_id,))
    ai = FakeAI(lambda r: tags())
    summary = auto(world, ai_client=ai)
    brand = summary["brands"][world.brand]
    assert brand["fetched"] >= 2  # crawling is free
    assert brand["over_budget"] and ai.requests == [] and brand["tagged"] == 0
    assert any("budget" in line for line in summary["needs_you"])


def test_waiting_for_ai_answers_polls_and_gives_up_for_tonight_after_the_limit(world):
    a_brand_with_a_manual(world)

    class Slow(FakeAI):
        def batch_results(self, batch_id):
            return None  # what the real client says while a batch is still processing

    slept = []
    from crawler.auto import run_auto
    summary = run_auto(world.conn, fetcher=world.fetcher(), sleep=slept.append, ai_client=Slow(lambda r: tags()),
                       embedder=BagEmbedder(), link_checker=Checker(), wait_seconds=900, poll_seconds=300)
    assert sum(slept) == 900 and summary["brands"][world.brand]["waiting_for_ai"] == 1
    assert summary["state"] == "done"  # the answers are collected by the next run


def test_the_monthly_budget_default_is_five_dollars_a_brand(world):
    assert q(world.conn, "SELECT monthly_budget_usd FROM brands WHERE name = %s", (world.brand,))[0][0] == Decimal("5")


def test_the_auto_command_runs_once_and_prints_the_report(runtime_conn, monkeypatch):
    import crawler.auto
    from test_p11_cli import cli
    monkeypatch.setattr(crawler.auto, "_prune", lambda owner_connect: {"sections": 0, "terms": 0, "vectors": 0})
    code, out, err = cli(runtime_conn, "auto")  # no AI keys in tests: crawling and sections only
    assert code == 0, err
    assert "Crawler run" in out and "Needs you:" in out
    assert q(runtime_conn, "SELECT state FROM runs ORDER BY id DESC LIMIT 1") == (("done",),)
