"""P18 (D74; Q21): the crawler approves hosts by its own judgment. A host whose PDFs are linked from a brand's approved
pages is judged by the AI once (yes/no with a logged reason). A yes starts a trial: 5 of its PDFs are fetched and
tagged; 3 equipment documents confirm the host, fewer stop it and the report says so. A host is approved for PDFs only,
never its web pages. Refused (AHRI) and blocklisted hosts are never asked about; `stop` and the switches turn it off; a
host already trusted for another brand is known without asking.

Fake web server, fake AI, `_testing`, one rolled-back transaction per test."""
import json

from fakeai import FakeAI
from p11support import Checker, serve, serve_pdf, start, world  # noqa: F401  (world is a fixture)
from p3support import q
from test_p11b_tagging import tags
from test_p14_search import BagEmbedder


class FakeHostAI:
    """The real client method is ask_host(model, text, max_tokens) -> crawler.ai.DiscoverReply with output
    {"approve": bool, "reason": str}. answer(host) -> bool."""

    def __init__(self, answer=lambda host: True):
        self.answer, self.calls = answer, []

    def ask_host(self, model, text, max_tokens):
        from crawler.ai import DiscoverReply
        host = json.loads(text.split("\n", 1)[1])["host"]
        self.calls.append(host)
        out = {"approve": bool(self.answer(host)), "reason": f"fake: {host} holds the brand's documents"}
        return DiscoverReply(output=out, raw_text=json.dumps(out), input_tokens=300, output_tokens=30, searches=0)


class Both(FakeAI):
    """One fake client for tagging (FakeAI) and the host check (FakeHostAI)."""

    def __init__(self, responder, host_answer=lambda host: True):
        super().__init__(responder)
        self.hosts = FakeHostAI(host_answer)

    def ask_host(self, *a):
        return self.hosts.ask_host(*a)


def files_site(world, n=6, host="files.test", body=None):
    links = [world.url(host, f"/doc{i}.pdf") for i in range(n)]
    serve(world, "/start", *links, host="ruud.test")
    for i in range(n):
        serve_pdf(world, f"/doc{i}.pdf", body=body(i) if body else None, host=host)
    start(world)


def auto(world, client, **kwargs):
    from crawler.auto import run_auto
    return run_auto(world.conn, fetcher=world.fetcher(), sleep=lambda s: None, ai_client=client,
                    embedder=BagEmbedder(), link_checker=Checker(), public_lists=False, **kwargs)


def decision(world, host="files.test"):
    rows = q(world.conn, "SELECT d.state, d.decided_by, d.reason, b.status, b.scope FROM host_decisions d "
                         "JOIN brand_domains b ON b.brand_id = d.brand_id AND b.host = d.host "
                         "WHERE d.brand_id = %s AND d.host = %s", (world.brand_id, host))
    return rows[0] if rows else None


def fetched(world, host="files.test"):
    return sorted(h["path"] for h in world.site.page_hits() if h["host"] == host)


def test_a_host_with_pdfs_linked_from_approved_pages_is_judged_once_with_its_reason_logged(world):
    files_site(world)
    client = Both(lambda r: tags())
    auto(world, client)  # night 1: the links are found
    auto(world, client)  # night 2: the host is judged, the trial starts
    assert client.hosts.calls == ["files.test"]
    state, by, reason, status, scope = decision(world)
    assert by == "ai" and "holds the brand's documents" in reason and (status, scope) == ("approved", "pdf")
    assert q(world.conn, "SELECT COUNT(*) FROM ai_calls WHERE purpose = 'host_check' AND brand_id = %s",
             (world.brand_id,))[0][0] == 1


def test_a_trial_of_five_real_manuals_confirms_the_host_and_then_it_is_crawled_in_full(world):
    files_site(world, n=6)
    client = Both(lambda r: tags())
    auto(world, client)
    second = auto(world, client)  # judged, 5 trial PDFs fetched and tagged, confirmed
    assert len(fetched(world)) == 5 and decision(world)[0] == "confirmed"
    assert any("files.test" in line and "confirmed" in line for line in second["hosts"])
    world.clock.advance(31 * 24 * 3600)
    auto(world, client)
    assert len(set(fetched(world))) == 6  # the sixth PDF too (the five are also rechecked, being due again)


def test_a_trial_of_junk_stops_the_host_and_the_report_asks_the_user(world):
    files_site(world, n=6)
    client = Both(lambda r: tags(doc_type="not_equipment"))
    auto(world, client)
    second = auto(world, client)
    state, _, _, status, _ = decision(world)
    assert (state, status) == ("stopped", "stopped") and len(fetched(world)) == 5
    assert any("files.test" in line and "stopped" in line for line in second["needs_you"])


def test_a_no_is_recorded_and_the_host_is_not_asked_about_again(world):
    files_site(world)
    client = Both(lambda r: tags(), host_answer=lambda host: False)
    for _ in range(3):
        auto(world, client)
    assert client.hosts.calls == ["files.test"] and decision(world) is None
    assert q(world.conn, "SELECT state FROM host_decisions WHERE host = 'files.test'") == (("rejected",),)
    assert fetched(world) == []


def test_an_approved_pdf_host_never_gives_its_web_pages(world):
    serve(world, "/start", world.url("files.test", "/doc0.pdf"), world.url("files.test", "/catalog.html"),
          host="ruud.test")
    serve_pdf(world, "/doc0.pdf", host="files.test")
    serve(world, "/catalog.html", host="files.test")
    start(world)
    q(world.conn, "INSERT INTO brand_domains (brand_id, host, status, found_by, scope) VALUES (%s, 'files.test', "
                  "'approved', 'ai', 'pdf')", (world.brand_id,))
    q(world.conn, "INSERT INTO host_decisions (brand_id, host, state, decided_by, reason) VALUES (%s, 'files.test', "
                  "'confirmed', 'ai', 'test')", (world.brand_id,))
    auto(world, Both(lambda r: tags()))
    assert fetched(world) == ["/doc0.pdf"]


def test_a_pdf_link_that_answers_with_a_web_page_is_refused_on_a_pdf_only_host(world):
    from crawler.fetch import Fetcher  # noqa: F401
    q(world.conn, "INSERT INTO brand_domains (brand_id, host, status, found_by, scope) VALUES (%s, 'files.test', "
                  "'approved', 'ai', 'pdf')", (world.brand_id,))
    serve(world, "/fake.pdf", host="files.test")  # HTML behind a .pdf address
    result = world.fetcher().fetch(world.url("files.test", "/fake.pdf"), world.brand_id)
    assert (result.outcome, result.reason) == ("failed", "pdf_only_host")


def test_refused_and_blocklisted_hosts_are_never_asked_about(world):
    serve(world, "/start", "https://www.ahridirectory.org/doc.pdf", "https://www.facebook.com/doc.pdf",
          "https://drive.google.com/doc.pdf", world.url("files.test", "/doc0.pdf"), host="ruud.test")
    serve_pdf(world, "/doc0.pdf", host="files.test")
    start(world)
    client = Both(lambda r: tags())
    auto(world, client)
    auto(world, client)
    assert client.hosts.calls == ["files.test"]


def test_stop_undoes_an_automatic_approval_and_it_is_never_judged_again(world):
    from crawler.brands import stop
    files_site(world)
    client = Both(lambda r: tags())
    auto(world, client)
    auto(world, client)
    stop(world.conn, "files.test")
    world.clock.advance(31 * 24 * 3600)
    before = len(fetched(world))
    auto(world, client)
    assert decision(world)[3] == "stopped" and len(fetched(world)) == before and client.hosts.calls == ["files.test"]


def test_auto_approval_can_be_switched_off_for_a_brand_or_everywhere(world, monkeypatch):
    files_site(world)
    client = Both(lambda r: tags())
    q(world.conn, "UPDATE control.brands SET auto_hosts = false WHERE crawl_brand_id = %s", (world.brand_id,))
    auto(world, client)
    summary = auto(world, client)
    assert client.hosts.calls == [] and any("files.test" in line for line in summary["needs_you"])
    q(world.conn, "UPDATE control.brands SET auto_hosts = true WHERE crawl_brand_id = %s", (world.brand_id,))
    monkeypatch.setenv("AUTO_APPROVE_HOSTS", "off")
    auto(world, client)
    assert client.hosts.calls == []


def test_a_host_trusted_for_one_brand_is_known_for_another_without_asking(world):
    from crawler.brands import add_brand, approve_host
    sister = add_brand(world.conn, f"{world.brand} Sister")
    approve_host(world.conn, "files.test", brand=f"{world.brand} Sister")  # trusted by the user for the sister brand
    q(world.conn, "UPDATE brands SET discovered_at = now() WHERE id = %s", (sister,))  # set up by hand, not from the page
    files_site(world)
    client = Both(lambda r: tags())
    auto(world, client)
    auto(world, client)
    state, by, reason, status, scope = decision(world)
    assert client.hosts.calls == [] and (state, by, status, scope) == ("confirmed", "rule", "approved", "pdf")
    assert "Sister" in reason


def test_pdfs_past_their_start_pages_depth_are_never_trial_pdfs(world):
    # Ruud run 2: media.rheem.com's PDFs sat at depth 2 under a start of depth 1, so its trial could never be fetched
    deep = [world.url("deep.test", f"/doc{i}.pdf") for i in range(3)]
    near = [world.url("files.test", f"/doc{i}.pdf") for i in range(3)]
    serve(world, "/start", "/hub", *near, host="ruud.test")
    serve(world, "/hub", *deep, host="ruud.test")
    for i in range(3):
        serve_pdf(world, f"/doc{i}.pdf", host="files.test")
        serve_pdf(world, f"/doc{i}.pdf", host="deep.test")
    start(world, depth=1)
    client = Both(lambda r: tags())
    auto(world, client)
    auto(world, client)
    assert client.hosts.calls == ["files.test"]  # deep.test's links can never be reached: nothing to judge
    assert decision(world)[0] == "confirmed"


def test_a_trial_stuck_on_unreachable_pdfs_is_topped_up_with_reachable_ones(world):
    from crawler.hostjudge import evaluate_trials
    links = [world.url("files.test", f"/doc{i}.pdf") for i in range(4)]
    serve(world, "/start", *links, host="ruud.test")
    for i in range(4):
        serve_pdf(world, f"/doc{i}.pdf", host="files.test")
    start(world, depth=1)
    auto(world, Both(lambda r: tags()))  # the links are recorded out of scope
    stuck = q(world.conn, "INSERT INTO pages (brand_id, host_id, url, url_sha, depth, fetch_state) SELECT brand_id, "
                          "host_id, url || '?far', md5(url || '?far'), 5, 'out_of_scope' FROM pages WHERE brand_id = %s "
                          "AND url LIKE %s LIMIT 1 RETURNING id", (world.brand_id, "%files.test%doc0.pdf"))[0][0]
    q(world.conn, "INSERT INTO brand_domains (brand_id, host, status, found_by, scope) VALUES (%s, 'files.test', "
                  "'approved', 'ai', 'pdf')", (world.brand_id,))
    q(world.conn, "INSERT INTO host_decisions (brand_id, host, state, decided_by, reason, trial_pages) VALUES "
                  "(%s, 'files.test', 'trial', 'ai', 'test', %s)", (world.brand_id, [stuck]))
    evaluate_trials(world.conn, world.brand_id)
    trial = q(world.conn, "SELECT trial_pages FROM host_decisions WHERE host = 'files.test'")[0][0]
    assert stuck not in trial and len(trial) == 4
    auto(world, Both(lambda r: tags()))
    assert decision(world)[0] == "confirmed"


def test_a_trial_with_no_pdf_the_crawl_can_reach_waits_instead_of_stopping(world):
    # Ruud run 3: media.rheem.com's PDFs were all past their start's depth; its trial read "0 of 0" and was stopped
    from crawler.hostjudge import evaluate_trials
    start(world, depth=1)
    q(world.conn, "INSERT INTO hosts (host) VALUES ('files.test') ON CONFLICT (host) DO NOTHING")
    far = q(world.conn, "INSERT INTO pages (brand_id, host_id, url, url_sha, depth, fetch_state) VALUES (%s, (SELECT id "
                        "FROM hosts WHERE host = 'files.test'), %s, md5(%s), 5, 'out_of_scope') RETURNING id",
            (world.brand_id, world.url("files.test", "/far.pdf"), "far"))[0][0]
    q(world.conn, "INSERT INTO brand_domains (brand_id, host, status, found_by, scope) VALUES (%s, 'files.test', "
                  "'approved', 'ai', 'pdf')", (world.brand_id,))
    q(world.conn, "INSERT INTO host_decisions (brand_id, host, state, decided_by, reason, trial_pages) VALUES "
                  "(%s, 'files.test', 'trial', 'ai', 'test', %s)", (world.brand_id, [far]))
    assert evaluate_trials(world.conn, world.brand_id) == []
    assert decision(world)[0] == "trial" and decision(world)[3] == "approved"


def test_a_host_only_on_trial_for_another_brand_is_not_trusted_for_this_one(world):
    # review 2026-10-30: a trial's PDF-only approval counted as "already trusted" for every other brand
    from crawler.brands import add_brand
    sister = add_brand(world.conn, f"{world.brand} Trialing")
    q(world.conn, "INSERT INTO brand_domains (brand_id, host, status, found_by, scope) VALUES (%s, 'files.test', "
                  "'approved', 'ai', 'pdf')", (sister,))
    q(world.conn, "INSERT INTO host_decisions (brand_id, host, state, decided_by, reason) VALUES (%s, 'files.test', "
                  "'trial', 'ai', 'fake')", (sister,))
    q(world.conn, "UPDATE brands SET discovered_at = now() WHERE id = %s", (sister,))
    files_site(world)
    client = Both(lambda r: tags())
    auto(world, client)  # finds the links
    auto(world, client)  # judges the host
    assert client.hosts.calls == ["files.test"]  # judged on its own evidence, not waved through
    assert decision(world)[1] == "ai"  # its own trial, not "rule: already trusted"
