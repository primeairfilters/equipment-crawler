"""P3: a page whose paid answer was unusable is not read again until its text or the extractor changes.

There is no time-based retry for a paid failure. Only a batch that comes back 404 (nothing was
billed) is tried again after a day. Time comes from the `now` argument of extract_pass.
"""
from datetime import datetime, timedelta

import pytest

from fakeai import FakeAI, Truncated
from p3support import answer, make_page, new_brand, q, responder_for, settle, spec_html

MODEL = "RA15AZ24AJ3CA"
T0 = datetime(2031, 3, 1, 9, 0)


def status(conn, page_id):
    return q(conn, "SELECT extraction_status FROM pages WHERE id = %s", (page_id,))[0][0]


def failed_page(conn, bad):
    """A page read once by a fake AI that answered `bad`. Returns (brand name, page id, the fake)."""
    name, brand_id = new_brand(conn)
    page_id = make_page(conn, brand_id, spec_html(MODEL))
    fake = FakeAI(lambda r: bad)
    settle(conn, fake, name, now=T0)
    assert len(fake.requests) == 1  # it really was read and billed once
    return name, page_id, fake


@pytest.mark.parametrize("bad", [Truncated('{"models": [{"model_number": "RA15'), "Sorry, I cannot do that."])
def test_a_cut_off_or_unreadable_answer_ends_failed_and_is_logged_as_rejected(runtime_conn, bad):
    name, page_id, fake = failed_page(runtime_conn, bad)
    assert status(runtime_conn, page_id) == "failed"
    rows = q(runtime_conn, "SELECT rejected, cost_usd FROM ai_calls WHERE page_id = %s", (page_id,))
    assert len(rows) == 1 and rows[0][0] == 1 and rows[0][1] > 0
    assert q(runtime_conn, "SELECT COUNT(*) FROM found_models WHERE page_id = %s", (page_id,))[0][0] == 0


@pytest.mark.parametrize("bad", [Truncated('{"models": [{"model_number": "RA15'), "Sorry, I cannot do that."])
def test_a_failed_page_is_not_read_again_31_days_later(runtime_conn, bad):
    name, page_id, fake = failed_page(runtime_conn, bad)
    calls = list(fake.calls)
    settle(runtime_conn, fake, name, now=T0 + timedelta(days=31))
    assert fake.calls == calls
    assert status(runtime_conn, page_id) == "failed"


def test_indexing_the_same_bytes_again_keeps_the_page_failed_and_makes_no_calls(runtime_conn):
    from crawler.textextract import index_page
    name, page_id, fake = failed_page(runtime_conn, "Sorry, I cannot do that.")
    calls = list(fake.calls)
    index_page(runtime_conn, page_id, spec_html(MODEL), is_pdf=False)  # same bytes, same cleaned text
    assert status(runtime_conn, page_id) == "failed"
    settle(runtime_conn, fake, name, now=T0 + timedelta(days=31))
    assert fake.calls == calls
    assert status(runtime_conn, page_id) == "failed"
    assert q(runtime_conn, "SELECT COUNT(*) FROM ai_calls WHERE page_id = %s", (page_id,))[0][0] == 1


def test_changed_text_reopens_a_failed_page_and_it_is_read_once(runtime_conn):
    from crawler.textextract import index_page
    name, page_id, fake = failed_page(runtime_conn, "Sorry, I cannot do that.")
    changed = spec_html(MODEL, seer2="16.0")
    index_page(runtime_conn, page_id, changed, is_pdf=False)
    assert status(runtime_conn, page_id) == "waiting"

    good = FakeAI(responder_for({MODEL: answer(MODEL, seer2="16.0")}))
    settle(runtime_conn, good, name, now=T0 + timedelta(days=2))
    assert len(good.requests) == 1
    assert status(runtime_conn, page_id) == "extracted"

    settle(runtime_conn, good, name, now=T0 + timedelta(days=40))
    assert len(good.requests) == 1  # and not again


def test_a_new_extractor_version_reopens_a_failed_page(runtime_conn, monkeypatch):
    import crawler.extract
    name, page_id, fake = failed_page(runtime_conn, "Sorry, I cannot do that.")
    monkeypatch.setattr(crawler.extract, "EXTRACTOR_VERSION", "test-next-version")
    good = FakeAI(responder_for({MODEL: answer(MODEL)}))
    settle(runtime_conn, good, name, now=T0 + timedelta(hours=1))
    assert len(good.requests) == 1
    assert status(runtime_conn, page_id) == "extracted"


def test_a_lost_batch_is_not_billed_and_is_tried_again_after_a_day(runtime_conn):
    from crawler.extract import extract_pass
    name, brand_id = new_brand(runtime_conn)
    page_id = make_page(runtime_conn, brand_id, spec_html(MODEL))
    fake = FakeAI(responder_for({MODEL: answer(MODEL)}))

    extract_pass(runtime_conn, fake, brand=name, now=T0)       # submits
    assert len(fake.requests) == 1
    fake.gone_ids.add("batch_1")                                # the batch has vanished (404)
    extract_pass(runtime_conn, fake, brand=name, now=T0)       # asks for it, gets 404
    assert status(runtime_conn, page_id) == "failed"
    assert q(runtime_conn, "SELECT COUNT(*) FROM ai_calls WHERE page_id = %s", (page_id,))[0][0] == 0  # nothing was billed

    extract_pass(runtime_conn, fake, brand=name, now=T0 + timedelta(hours=2))
    assert len(fake.requests) == 1                              # too soon

    settle(runtime_conn, fake, name, now=T0 + timedelta(days=1, hours=1))
    assert len(fake.requests) == 2                              # a day later it is sent again
    assert status(runtime_conn, page_id) == "extracted"
