"""P3: extraction with a FAKE AI client: cost, cap, never-twice, splitting, logging, and what reaches the equipment tables.

No network and no key. The fake client lives in fakeai.py; every pass is run against a brand
that belongs to the test, so rows already in the testing database cannot take part.
"""
from datetime import datetime
from decimal import Decimal

import pytest

from fakeai import FakeAI
from p3support import answer, make_doc, make_page, make_pdf, new_brand, q, responder_for, settle, spec_html

MODEL = "RA15AZ24AJ3CA"
HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"


def dec(value):
    return Decimal(str(value))


# ---------------------------------------------------------------- prices

@pytest.mark.parametrize("model,kwargs,expected", [
    # Haiku at or under 100,000 prompt tokens: $0.10 in / $0.50 out per MTok, batch half
    (HAIKU, dict(input_tokens=100_000), "0.01"),
    (HAIKU, dict(output_tokens=1_000_000), "0.50"),
    (HAIKU, dict(input_tokens=100_000, batch=True), "0.005"),
    (HAIKU, dict(output_tokens=1_000_000, batch=True), "0.25"),
    # Haiku over 100,000 prompt tokens bills 5x on input and output (the default)
    (HAIKU, dict(input_tokens=100_001), "0.0500005"),
    (HAIKU, dict(input_tokens=100_001, output_tokens=1_000), "0.0525005"),
    (HAIKU, dict(input_tokens=100_001, output_tokens=1_000, batch=True), "0.02625025"),
    # Sonnet has no long-prompt rate
    (SONNET, dict(input_tokens=1_000_000), "2"),
    (SONNET, dict(input_tokens=150_000), "0.3"),
    (SONNET, dict(output_tokens=1_000_000), "10"),
    (HAIKU, dict(searches=3), "0.03"),
])
def test_cost_uses_the_price_table(model, kwargs, expected):
    from crawler.ai import cost
    assert dec(cost(model, **kwargs)) == dec(expected)


@pytest.mark.parametrize("model", ["gpt-4o", "claude-haiku-4", "", "claude-haiku-5-5-preview"])
def test_cost_of_an_unknown_model_is_an_error_not_a_guess(model):
    from crawler.ai import UnknownModel, cost
    with pytest.raises(UnknownModel):
        cost(model, input_tokens=1000, output_tokens=1000)


def test_monthly_spend_counts_only_the_calendar_month_of_now(runtime_conn):
    from crawler.ai import monthly_spend
    with runtime_conn.cursor() as cur:
        for when, amount in (("2031-01-15 10:00:00", "0.5"), ("2031-01-30 23:59:59", "0.25"), ("2030-12-31 23:59:59", "9")):
            cur.execute("INSERT INTO ai_calls (purpose, model, cost_usd, created_at) VALUES ('test', %s, %s, %s)",
                        (HAIKU, amount, when))
    assert dec(monthly_spend(runtime_conn, datetime(2031, 1, 31, 12, 0))) == dec("0.75")
    assert dec(monthly_spend(runtime_conn, datetime(2031, 2, 1, 0, 5))) == dec("0")
    assert dec(monthly_spend(runtime_conn, datetime(2030, 12, 31, 12, 0))) == dec("9")


# ---------------------------------------------------------------- the happy path

def one_doc(conn, model=MODEL, **html):
    name, brand_id = new_brand(conn)
    page_id = make_page(conn, brand_id, spec_html(model, **html))
    return name, brand_id, page_id


def test_a_document_goes_to_ai_once_and_its_checked_facts_reach_the_equipment_tables(runtime_conn):
    name, brand_id, page_id = one_doc(runtime_conn)
    fake = FakeAI(responder_for({MODEL: answer(MODEL)}))
    settle(runtime_conn, fake, name)

    assert len(fake.requests) == 1
    req = fake.requests[0]
    assert req.model == HAIKU and req.max_tokens > 0
    assert MODEL in req.text and "SEER2\t15.2" in req.text  # cleaned text with tab-separated table rows
    assert req.system  # the fixed instructions

    assert q(runtime_conn, "SELECT extraction_status FROM pages WHERE id = %s", (page_id,)) == (("extracted",),)
    got = q(runtime_conn, "SELECT m.model_key, s.spec_key, s.value_text, s.extracted_by, s.superseded FROM equipment_specs s "
                          "JOIN equipment_models m ON m.id = s.model_id JOIN manufacturers f ON f.id = m.manufacturer_id "
                          "WHERE f.name = %s ORDER BY s.spec_key", (name,))
    assert got == ((MODEL, "seer2", "15.2", f"ai:{HAIKU}", 0), (MODEL, "tonnage", "2.0", f"ai:{HAIKU}", 0))
    src = q(runtime_conn, "SELECT s.url FROM equipment_sources s JOIN equipment_model_sources ms ON ms.source_id = s.id "
                          "JOIN equipment_models m ON m.id = ms.model_id JOIN manufacturers f ON f.id = m.manufacturer_id "
                          "WHERE f.name = %s", (name,))
    assert src == ((q(runtime_conn, "SELECT url FROM pages WHERE id = %s", (page_id,))[0][0],),)
    found = q(runtime_conn, "SELECT fs.spec_key, fs.check_status, fs.extracted_by FROM found_specs fs "
                            "JOIN found_models fm ON fm.id = fs.found_model_id WHERE fm.page_id = %s ORDER BY fs.spec_key",
              (page_id,))
    assert found == (("seer2", "passed", f"ai:{HAIKU}"), ("tonnage", "passed", f"ai:{HAIKU}"))


def test_a_page_that_failed_the_free_filter_is_never_sent(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    make_page(runtime_conn, brand_id, b"<html><body>Products. Contact us.</body></html>")
    fake = FakeAI(lambda r: {"models": []})
    settle(runtime_conn, fake, name)
    assert fake.calls == []


# ---------------------------------------------------------------- never twice

def test_a_second_run_over_unchanged_text_makes_no_ai_calls_at_all(runtime_conn):
    name, brand_id, page_id = one_doc(runtime_conn)
    fake = FakeAI(responder_for({MODEL: answer(MODEL)}))
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 1  # the path ran the first time
    row = q(runtime_conn, "SELECT ai_extracted_sha, ai_extractor_version FROM pages WHERE id = %s", (page_id,))[0]
    from crawler.extract import EXTRACTOR_VERSION
    assert row[0] and row[1] == EXTRACTOR_VERSION

    calls, billed = list(fake.calls), q(runtime_conn, "SELECT COUNT(*) FROM ai_calls")[0][0]
    settle(runtime_conn, fake, name)
    assert fake.calls == calls  # not even a token count
    assert q(runtime_conn, "SELECT COUNT(*) FROM ai_calls")[0][0] == billed


def test_changed_text_is_read_again(runtime_conn):
    from crawler.textextract import index_page
    name, brand_id, page_id = one_doc(runtime_conn)
    fake = FakeAI(responder_for({MODEL: answer(MODEL)}))
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 1
    index_page(runtime_conn, page_id, spec_html(MODEL, seer2="16.0"), is_pdf=False)
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 2


def test_a_new_extractor_version_reads_unchanged_text_again(runtime_conn, monkeypatch):
    import crawler.extract
    name, brand_id, page_id = one_doc(runtime_conn)
    fake = FakeAI(responder_for({MODEL: answer(MODEL)}))
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 1
    monkeypatch.setattr(crawler.extract, "EXTRACTOR_VERSION", "test-next-version")
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 2
    assert q(runtime_conn, "SELECT ai_extractor_version FROM pages WHERE id = %s", (page_id,)) == (("test-next-version",),)


def test_the_same_text_under_another_brand_is_not_paid_for_twice(runtime_conn):
    name_a, brand_a, page_a = one_doc(runtime_conn)
    name_b, brand_b = new_brand(runtime_conn)
    page_b = make_page(runtime_conn, brand_b, spec_html(MODEL))  # same cleaned text, another brand
    fake = FakeAI(responder_for({MODEL: answer(MODEL)}))
    settle(runtime_conn, fake, name_a)
    assert len(fake.requests) == 1  # the first brand really was read

    calls = list(fake.calls)
    settle(runtime_conn, fake, name_b)
    assert fake.calls == calls      # no count, no submit for the second brand
    row = q(runtime_conn, "SELECT extraction_status, ai_extracted_sha, ai_extractor_version FROM pages WHERE id = %s", (page_b,))[0]
    from crawler.extract import EXTRACTOR_VERSION
    assert row[0] == "extracted" and row[1] and row[2] == EXTRACTOR_VERSION
    assert q(runtime_conn, "SELECT COUNT(*) FROM manufacturers WHERE name = %s", (name_b,))[0][0] == 0  # facts stay with the first brand
    assert q(runtime_conn, "SELECT COUNT(*) FROM ai_calls WHERE page_id = %s", (page_b,))[0][0] == 0


def test_a_document_that_shrinks_never_sends_its_old_last_page(runtime_conn):
    from crawler.textextract import index_page
    name, brand_id = new_brand(runtime_conn)
    old = [f"STALEPAGE{n} Model RA15AZ2{n}AJ3CA\nSEER2 15.2\nTonnage 2.0" for n in (1, 2, 3)]
    page_id = make_doc(runtime_conn, brand_id, old)
    new_pdf = make_pdf([[f"Model {MODEL}", "SEER2 15.2", "Tonnage 2.0"], ["Model RA15AZ25AJ3CA", "SEER2 15.2", "Tonnage 2.0"]])
    index_page(runtime_conn, page_id, new_pdf, is_pdf=True)  # now two pages; row 3 is left over in page_text
    assert q(runtime_conn, "SELECT COUNT(*) FROM page_text WHERE page_id = %s", (page_id,))[0][0] == 3  # nothing deleted

    fake = FakeAI(lambda r: {"models": []})
    settle(runtime_conn, fake, name)
    assert len(fake.requests) >= 1  # the document was sent
    sent = "".join(r.text for r in fake.requests)
    assert MODEL in sent and "RA15AZ25AJ3CA" in sent
    assert "STALEPAGE3" not in sent and "RA15AZ23AJ3CA" not in sent


# ---------------------------------------------------------------- what the rules reject

def test_a_model_the_ai_made_up_is_rejected_and_never_reaches_the_equipment_tables(runtime_conn):
    name, brand_id, page_id = one_doc(runtime_conn)
    made_up = {"models": [
        {"model_number": MODEL, "specs": [{"key": "seer2", "value": "15.2", "unit": None}]},
        {"model_number": "ZZ999FAKE123", "specs": [{"key": "seer2", "value": "15.2", "unit": None}]},
    ]}
    fake = FakeAI(lambda r: made_up)
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 1  # the path ran

    keys = {r[0] for r in q(runtime_conn, "SELECT m.model_key FROM equipment_models m JOIN manufacturers f "
                                          "ON f.id = m.manufacturer_id WHERE f.name = %s", (name,))}
    assert keys == {MODEL}
    failed = q(runtime_conn, "SELECT fs.check_status, fs.fail_reason FROM found_specs fs JOIN found_models fm "
                             "ON fm.id = fs.found_model_id WHERE fm.page_id = %s AND fm.model_key = 'ZZ999FAKE123'", (page_id,))
    assert failed == (("failed", "model_not_on_page"),)
    assert q(runtime_conn, "SELECT COUNT(*) FROM ai_calls WHERE page_id = %s", (page_id,))[0][0] == 1  # still billed and logged


def test_a_number_that_is_not_on_the_page_is_rejected_but_the_good_facts_next_to_it_are_kept(runtime_conn):
    name, brand_id, page_id = one_doc(runtime_conn)
    wrong = answer(MODEL, seer2="16.5")  # the page says 15.2
    fake = FakeAI(lambda r: wrong)
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 1
    keys = [r[0] for r in q(runtime_conn, "SELECT s.spec_key FROM equipment_specs s JOIN equipment_models m ON m.id = s.model_id "
                                          "JOIN manufacturers f ON f.id = m.manufacturer_id WHERE f.name = %s", (name,))]
    assert keys == ["tonnage"]
    assert q(runtime_conn, "SELECT fs.fail_reason FROM found_specs fs JOIN found_models fm ON fm.id = fs.found_model_id "
                           "WHERE fm.page_id = %s AND fs.check_status = 'failed'", (page_id,)) == (("number_not_on_page",),)


def test_an_unreadable_answer_is_logged_as_rejected_and_writes_nothing(runtime_conn):
    name, brand_id, page_id = one_doc(runtime_conn)
    fake = FakeAI(lambda r: "Sorry, I cannot help with that.", usage=lambda r, counted: (500, 20))
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 1
    row = q(runtime_conn, "SELECT rejected, input_tokens, output_tokens, cost_usd FROM ai_calls WHERE page_id = %s", (page_id,))
    assert len(row) == 1
    rejected, tin, tout, cost = row[0]
    assert (rejected, tin, tout) == (1, 500, 20)
    assert cost > 0  # it was billed, so it is logged with its cost
    assert q(runtime_conn, "SELECT extraction_status FROM pages WHERE id = %s", (page_id,)) == (("failed",),)
    assert q(runtime_conn, "SELECT COUNT(*) FROM found_models WHERE page_id = %s", (page_id,))[0][0] == 0
    assert q(runtime_conn, "SELECT COUNT(*) FROM manufacturers WHERE name = %s", (name,))[0][0] == 0


# ---------------------------------------------------------------- ai_calls

def test_every_billed_response_is_logged_with_its_batch_price(runtime_conn):
    name, brand_id, page_id = one_doc(runtime_conn)
    fake = FakeAI(responder_for({MODEL: answer(MODEL)}), usage=lambda r, counted: (20_000, 1_000))
    settle(runtime_conn, fake, name)
    rows = q(runtime_conn, "SELECT purpose, brand_id, model, input_tokens, output_tokens, is_batch, rejected, cost_usd "
                           "FROM ai_calls WHERE page_id = %s", (page_id,))
    assert len(rows) == 1
    purpose, b, model, tin, tout, is_batch, rejected, cost = rows[0]
    assert (purpose, b, model, tin, tout, is_batch, rejected) == ("extract", brand_id, HAIKU, 20_000, 1_000, 1, 0)
    assert dec(cost) == dec("0.00125")  # 20k in at $0.05/MTok plus 1k out at $0.25/MTok


# ---------------------------------------------------------------- unknown model, cap

def test_an_unknown_model_name_is_an_error_before_any_call(runtime_conn):
    from crawler.ai import UnknownModel
    from crawler.extract import extract_pass
    name, brand_id, page_id = one_doc(runtime_conn)
    fake = FakeAI(responder_for({MODEL: answer(MODEL)}))
    with pytest.raises(UnknownModel):
        extract_pass(runtime_conn, fake, brand=name, model="gpt-4o")
    assert fake.calls == []
    assert q(runtime_conn, "SELECT COUNT(*) FROM ai_calls WHERE page_id = %s", (page_id,))[0][0] == 0
    assert q(runtime_conn, "SELECT extraction_status FROM pages WHERE id = %s", (page_id,)) == (("waiting",),)


def three_docs(conn):
    name, brand_id = new_brand(conn)
    models = [f"RA15AZ{n}AJ3CA" for n in (21, 22, 23)]
    pages = [make_page(conn, brand_id, spec_html(m)) for m in models]
    known = {m: answer(m) for m in models}
    # every request is billed at its worst case: 60k prompt tokens in, max_tokens out
    fake = FakeAI(responder_for(known), tokens=60_000, usage=lambda r, counted: (60_000, r.max_tokens))
    return name, pages, fake


def statuses(conn, pages):
    return [q(conn, "SELECT extraction_status FROM pages WHERE id = %s", (p,))[0][0] for p in pages]


def test_with_a_big_enough_cap_all_three_documents_are_read(runtime_conn):
    name, pages, fake = three_docs(runtime_conn)
    settle(runtime_conn, fake, name, cap_usd=Decimal("100"), max_tokens=4096)
    assert statuses(runtime_conn, pages) == ["extracted"] * 3
    assert len(fake.requests) == 3


def test_the_monthly_cap_holds_a_batch_that_could_go_over(runtime_conn):
    name, pages, fake = three_docs(runtime_conn)
    cap = Decimal("0.01")  # one request is at most $0.004024 at batch prices, so three could cost $0.012
    settle(runtime_conn, fake, name, cap_usd=cap, max_tokens=4096)
    assert 1 <= len(fake.requests) <= 2                 # something went out, so the cap logic ran
    assert "waiting" in statuses(runtime_conn, pages)   # and at least one document is held, not failed
    assert "failed" not in statuses(runtime_conn, pages)
    brand_id = q(runtime_conn, "SELECT id FROM brands WHERE name = %s", (name,))[0][0]
    spent = q(runtime_conn, "SELECT COALESCE(SUM(cost_usd), 0) FROM ai_calls WHERE brand_id = %s", (brand_id,))[0][0]
    assert 0 < dec(spent) <= cap


def test_a_cap_too_small_for_even_one_request_sends_nothing_and_does_not_fail(runtime_conn):
    name, pages, fake = three_docs(runtime_conn)
    settle(runtime_conn, fake, name, cap_usd=Decimal("0.001"), max_tokens=4096)
    assert fake.requests == []
    assert statuses(runtime_conn, pages) == ["waiting"] * 3


def test_at_the_cap_ai_pauses_until_the_first_of_the_next_month(runtime_conn):
    name, pages, fake = three_docs(runtime_conn)
    with runtime_conn.cursor() as cur:
        cur.execute("INSERT INTO ai_calls (purpose, model, cost_usd, created_at) VALUES ('test', %s, %s, %s)",
                    (HAIKU, "24.999999", "2031-01-15 10:00:00"))
    cap = Decimal("25")
    settle(runtime_conn, fake, name, cap_usd=cap, now=datetime(2031, 1, 20, 9, 0), max_tokens=4096)
    assert fake.requests == []
    assert statuses(runtime_conn, pages) == ["waiting"] * 3

    settle(runtime_conn, fake, name, cap_usd=cap, now=datetime(2031, 2, 1, 0, 5), max_tokens=4096)
    assert len(fake.requests) == 3  # control: the same documents go out once the month turns
    assert statuses(runtime_conn, pages) == ["extracted"] * 3


# ---------------------------------------------------------------- splitting a big document

def test_a_document_over_100k_prompt_tokens_is_split_into_requests_under_the_limit(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    filler = "filler words go here " * 1900  # about 40k characters, about 10k tokens by the fake's len//4
    models = [f"RA15AZ{n:02d}BX" for n in range(12)]
    texts = [f"PAGEMARK{n:02d} Model {m}\nTonnage 2.0\nSEER2 15.2\n{filler}" for n, m in enumerate(models)]
    page_id = make_doc(runtime_conn, brand_id, texts)
    fake = FakeAI(responder_for({m: answer(m) for m in models}))
    settle(runtime_conn, fake, name)

    assert len(fake.requests) >= 2
    for r in fake.requests:
        assert (len(r.system) + len(r.text)) // 4 <= 100_000
    sent = "".join(r.text for r in fake.requests)
    for n in range(12):
        assert f"PAGEMARK{n:02d}" in sent  # every page went out in some part
    keys = {r[0] for r in q(runtime_conn, "SELECT m.model_key FROM equipment_models m JOIN manufacturers f "
                                          "ON f.id = m.manufacturer_id WHERE f.name = %s", (name,))}
    assert keys == set(models)
    assert q(runtime_conn, "SELECT COUNT(*) FROM ai_calls WHERE page_id = %s", (page_id,))[0][0] == len(fake.requests)
    assert q(runtime_conn, "SELECT extraction_status FROM pages WHERE id = %s", (page_id,)) == (("extracted",),)

    submitted = len(fake.requests)
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == submitted  # a split document is not read again either


# ---------------------------------------------------------------- crawl -> text -> AI, end to end

def test_run_stores_text_for_fetched_pages_and_extraction_fills_the_equipment_tables(runtime_conn):
    from crawler.brands import add_start_url
    from crawler.crawl import run
    from fakesite import FakeSite  # noqa: F401
    from p3support import make_pdf
    from test_p2_fetch import World

    w = World(runtime_conn)
    try:
        w.approve("ruud.test")
        add_start_url(runtime_conn, w.brand, w.url("ruud.test", "/start"))
        html = {"Content-Type": "text/html"}
        w.site.route("/start", b'<a href="/spec">s</a><a href="/plain">p</a><a href="/doc.pdf">d</a>', headers=html)
        w.site.route("/spec", spec_html("RA15AZ31AJ3CA"), headers=html)
        w.site.route("/plain", b"<html><body>About us. Contact.</body></html>", headers=html)
        pdf = make_pdf([["Model RA15AZ32AJ3CA", "SEER2 15.2", "Tonnage 2.0"]])
        w.site.route("/doc.pdf", pdf, headers={"Content-Type": "application/pdf"})
        run(runtime_conn, w.fetcher(), brand=w.brand)

        def status(path):
            return q(runtime_conn, "SELECT extraction_status FROM pages WHERE brand_id = %s AND url LIKE %s",
                     (w.brand_id, "%" + path))[0][0]

        assert status("/spec") == "waiting" and status("/doc.pdf") == "waiting"
        assert status("/plain") == "skipped" and status("/start") == "skipped"
        assert q(runtime_conn, "SELECT COUNT(*) FROM page_text pt JOIN pages p ON p.id = pt.page_id "
                               "WHERE p.brand_id = %s AND p.url LIKE '%%/doc.pdf'", (w.brand_id,))[0][0] == 1

        known = {m: answer(m) for m in ("RA15AZ31AJ3CA", "RA15AZ32AJ3CA")}
        fake = FakeAI(responder_for(known))
        settle(runtime_conn, fake, w.brand)
        assert len(fake.requests) == 2
        keys = {r[0] for r in q(runtime_conn, "SELECT m.model_key FROM equipment_models m JOIN manufacturers f "
                                              "ON f.id = m.manufacturer_id WHERE f.name = %s", (w.brand,))}
        assert keys == {"RA15AZ31AJ3CA", "RA15AZ32AJ3CA"}
    finally:
        w.site.stop()
