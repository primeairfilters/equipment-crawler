"""P21 (D77): what Breeze could not answer. Breeze's tool inserts a miss (eq_breeze may do that and nothing else
new); each run sorts the new ones: a brand not covered goes to the admin page and the report, a model with no manual
gets one gap-fill search (a repeat on the same model does not), and otherwise the AI says whether one of the closest
sections answers it: search_missed (an eval question with its known page) or not_in_manuals. Daily limits hold; a
miss that cannot be sorted today waits.

Fake AI, fake embedder, `_testing`, one rolled-back transaction per test (the eq_breeze probe rolls back too)."""
import json

import psycopg
import pytest

from fakeai import FakeGapAI
from p11support import world  # noqa: F401  (world is a fixture)
from p3support import q
from support import connect
from test_admin_control import admin_conn  # noqa: F401  (a fixture)
from test_p1_users import privileges, refused
from test_p14_search import BagEmbedder, manual, topic


class Judge(FakeGapAI):
    """Gap-fill searches and the miss check in one fake client. answer(text) -> (answered, section)."""

    def __init__(self, answer=lambda text: (False, 0), documents=None):
        super().__init__(documents)
        self.answer, self.judged = answer, []

    def judge_miss(self, model, text, max_tokens):
        from crawler.ai import DiscoverReply
        self.judged.append(text)
        answered, section = self.answer(text)
        out = {"answered": answered, "section": section, "reason": "fake"}
        return DiscoverReply(output=out, raw_text=json.dumps(out), input_tokens=900, output_tokens=20, searches=0)


def miss(conn, question, **cols):
    cols = {"question": question, **cols}
    return q(conn, f"INSERT INTO breeze.misses ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
             tuple(cols.values()))[0][0]


def sorted_as(conn, miss_id):
    return q(conn, "SELECT state, kind, detail, repeat_of FROM breeze.misses WHERE id = %s", (miss_id,))[0]


def sort(conn, **kw):
    from crawler.misses import sort_misses
    kw.setdefault("embedder", BagEmbedder())
    return sort_misses(conn, cap_usd=5, **kw)


def test_breeze_may_insert_a_miss_and_nothing_else_new():
    conn = connect("breeze")
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO breeze.misses (question, brand, model, equipment_type, trade, source) VALUES "
                        "('What is the subcooling target?', 'Ruud', 'RA15AZ24AJ3CA', 'air_conditioner', 'hvac', "
                        "'live_camera')")
        assert refused(conn, "SELECT 1 FROM breeze.misses LIMIT 0")
        assert refused(conn, "UPDATE breeze.misses SET kind = 'repeat' WHERE false")
        assert refused(conn, "DELETE FROM breeze.misses WHERE false")
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT s")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("INSERT INTO breeze.misses (question, state) VALUES ('x', 'sorted')")
            cur.execute("ROLLBACK TO SAVEPOINT s")
    finally:
        conn.rollback()
        conn.close()


def test_the_admin_page_reads_the_misses(admin_conn, migrate_conn):
    with admin_conn.cursor() as cur:
        cur.execute("SELECT * FROM control.breeze_misses LIMIT 1")
    assert privileges(migrate_conn, "eq_admin", "control.breeze_misses") == {"SELECT"}


def test_a_brand_the_crawler_does_not_have_is_listed_for_the_user(runtime_conn):
    from crawler.misses import uncovered_brands
    first = miss(runtime_conn, "Defrost board pinout?", brand="Zorbex Nowhere Brand")
    miss(runtime_conn, "Error 7?", brand="Zorbex Nowhere Brand")
    assert sort(runtime_conn, client=Judge())["uncovered_brand"] == 2
    assert sorted_as(runtime_conn, first)[:2] == ("sorted", "uncovered_brand")
    assert ("Zorbex Nowhere Brand", 2) in uncovered_brands(runtime_conn)
    assert q(runtime_conn, "SELECT COUNT(*) FROM brands WHERE name_key = 'zorbex nowhere brand'")[0][0] == 0


def test_a_model_with_no_manual_gets_one_search_and_a_repeat_gets_none(world):
    client = Judge(documents={"documents": [{"url": world.url("ruud.test", "/docs/zx99-install.pdf"),
                                              "evidence_url": world.url("ruud.test", "/support")}]})
    first = miss(world.conn, "Wiring diagram for the ZX99AB?", brand=world.brand, model="ZX99AB12")
    again = miss(world.conn, "ZX99AB fan speed?", brand=world.brand, model="ZX99AB12")
    done = sort(world.conn, client=client)
    assert (done["no_manual"], done["repeat"]) == (1, 1) and len(client.searches()) == 1
    state, kind, detail, _ = sorted_as(world.conn, first)
    assert (kind, detail["family"], detail["queued"]) == ("no_manual", "ZX99AB", 1)
    assert sorted_as(world.conn, again)[1:] == ("repeat", {"model_key": "ZX99AB12"}, first)
    assert q(world.conn, "SELECT found_by FROM start_urls WHERE brand_id = %s AND url LIKE %s",
             (world.brand_id, "%zx99-install.pdf")) == (("gapfill",),)


def test_searches_stop_at_the_daily_limit_and_the_rest_wait(world, monkeypatch):
    import crawler.misses
    monkeypatch.setattr(crawler.misses, "SEARCHES_PER_DAY", 1)
    miss(world.conn, "Q1", brand=world.brand, model="ZX11AA00")
    later = miss(world.conn, "Q2", brand=world.brand, model="ZX22BB00")
    done = sort(world.conn, client=Judge())
    assert (done["no_manual"], done["waiting"]) == (1, 1)
    assert sorted_as(world.conn, later)[0] == "new"


def test_an_answer_a_manual_has_is_a_search_miss_and_an_eval_question(runtime_conn, monkeypatch):
    from crawler.embed import embed_pass
    from crawler.misses import eval_cases
    doc, _ = manual(runtime_conn, monkeypatch, topic("Error Codes", "diagnostic", "Code E4 means the pressure switch "
                                                     "opened."), brand_name="Misstest")
    embed_pass(runtime_conn, BagEmbedder(), cap_usd=5)
    sha = q(runtime_conn, "SELECT content_sha FROM documents WHERE id = %s", (doc,))[0][0]
    client = Judge(answer=lambda text: (True, next(n for n in range(1, 21) if f"[{n}]" in text
                                                    and "Error Codes" in text.split(f"[{n}]")[1][:200])))
    asked = miss(runtime_conn, "what does diagnostic code E4 mean")
    assert sort(runtime_conn, client=client)["search_missed"] >= 1
    state, kind, detail, _ = sorted_as(runtime_conn, asked)
    assert (kind, detail["document_id"], detail["content_sha"]) == ("search_missed", doc, sha)
    assert {"question": "what does diagnostic code E4 mean",
            "expect": [{"document": sha[:16], "pages": detail["pages"]}]} in eval_cases(runtime_conn)
    assert q(runtime_conn, "SELECT COUNT(*) FROM ai_calls WHERE purpose = 'miss_check'")[0][0] >= 1


def test_a_question_no_manual_answers_is_logged_as_not_in_the_manuals(runtime_conn):
    asked = miss(runtime_conn, "how much should I charge for a service call")
    sort(runtime_conn, client=Judge())
    assert sorted_as(runtime_conn, asked)[1] == "not_in_manuals"


def test_without_an_ai_key_misses_wait_for_a_later_run(runtime_conn):
    asked = miss(runtime_conn, "what is the subcooling target")
    assert sort(runtime_conn, client=None)["waiting"] >= 1
    assert sorted_as(runtime_conn, asked)[0] == "new"


def test_misses_is_a_command():
    from crawler.__main__ import build_parser
    assert build_parser().parse_args(["misses"]).command == "misses"
