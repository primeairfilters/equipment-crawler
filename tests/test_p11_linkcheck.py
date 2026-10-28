"""P11 (D62): the live "Should I crawl this page?" call: what it sends, what it logs, the cap, and suggestions."""
from datetime import datetime
from decimal import Decimal

from fakeai import FakeLinkAI
from p3support import new_brand, q

NOW = datetime(2026, 10, 27, 12, 0, 0)
CONTEXT = {"url": "https://www.ruud.com/products/", "title": "Air conditioners", "heading": "Split systems"}
LINKS = [("https://www.ruud.com/about/", "About us"), ("https://www.ruud.com/range/ra15az/", "RA15AZ series")]


def checker(fake, cap="25"):
    from crawler.linkcheck import LinkChecker
    return LinkChecker(fake, cap_usd=Decimal(cap), now=lambda: NOW)


def test_one_call_carries_every_unsure_link_with_its_text_and_the_page_title(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    fake = FakeLinkAI(lambda url, text: "range" in url)
    answers = checker(fake).check(runtime_conn, brand_id, None, CONTEXT, LINKS)
    assert len(fake.calls) == 1
    _, model, context, links, max_tokens = fake.calls[0]
    assert model == "claude-haiku-5-5" and context == CONTEXT and links == LINKS and max_tokens <= 2000
    assert answers == {LINKS[0][0]: (False, "fake reason"), LINKS[1][0]: (True, "fake reason")}


def test_the_call_is_logged_with_its_cost(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    checker(FakeLinkAI(input_tokens=1000, output_tokens=100)).check(runtime_conn, brand_id, None, CONTEXT, LINKS)
    rows = q(runtime_conn, "SELECT purpose, model, input_tokens, output_tokens, is_batch, rejected, cost_usd FROM ai_calls "
                           "WHERE brand_id = %s", (brand_id,))
    assert rows == (("link_check", "claude-haiku-5-5", 1000, 100, False, False, Decimal("0.000150")),)


def test_at_the_cap_nothing_is_asked_and_no_link_gets_an_answer(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    fake = FakeLinkAI()
    assert checker(fake, cap="0.00001").check(runtime_conn, brand_id, None, CONTEXT, LINKS) == {}
    assert fake.calls == []
    assert q(runtime_conn, "SELECT COUNT(*) FROM ai_calls WHERE brand_id = %s", (brand_id,))[0][0] == 0


def test_an_unusable_answer_is_billed_and_logged_as_rejected_and_answers_nothing(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    fake = FakeLinkAI("I cannot tell.")
    assert checker(fake).check(runtime_conn, brand_id, None, CONTEXT, LINKS) == {}
    assert q(runtime_conn, "SELECT rejected FROM ai_calls WHERE brand_id = %s", (brand_id,)) == ((True,),)


def test_an_answer_for_a_link_number_that_was_not_asked_is_ignored(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    fake = FakeLinkAI({"answers": [{"n": 1, "crawl": True, "reason": "ok"}, {"n": 7, "crawl": True, "reason": "x"}],
                       "suggest_skip": []})
    assert checker(fake).check(runtime_conn, brand_id, None, CONTEXT, LINKS) == {LINKS[0][0]: (True, "ok")}


def test_suggested_skip_keywords_are_kept_for_the_user_and_never_switched_on(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    fake = FakeLinkAI(suggest=["news*", "careers", "not a keyword!", "x" * 80])
    checker(fake).check(runtime_conn, brand_id, None, CONTEXT, LINKS)
    checker(fake).check(runtime_conn, brand_id, None, CONTEXT, LINKS)
    assert q(runtime_conn, "SELECT keyword, times FROM keyword_suggestions WHERE brand_id = %s ORDER BY keyword",
             (brand_id,)) == (("careers", 2), ("news*", 2))
    assert q(runtime_conn, "SELECT COUNT(*) FROM crawl_keywords WHERE keyword IN ('careers', 'news*') "
                           "AND (brand_id = %s OR brand_id IS NULL)", (brand_id,))[0][0] == 0


def test_the_request_tells_the_model_that_page_text_is_data_not_instructions():
    from crawler.linkcheck import LINK_SYSTEM
    assert "data" in LINK_SYSTEM.lower() and "instruction" in LINK_SYSTEM.lower()
