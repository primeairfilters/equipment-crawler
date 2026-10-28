"""P13 (D42, D52): section vectors. One table per embedding model and size; a section is embedded once per model
(the same text reuses its vector); every request is logged and the monthly cap holds; a fake client, no network."""
import hashlib
from decimal import Decimal

import pytest

from p3support import q
from test_p12_build import PAGES, document, live

TABLE = "section_vectors_te3s_1536"


class FakeEmbedder:
    """Same shape as crawler.embed.OpenAIEmbedder: embed(texts) -> (vectors, billed tokens)."""
    model = "text-embedding-3-small"
    dims = 1536

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def embed(self, texts):
        if self.fail:
            from crawler.embed import EmbedError
            raise EmbedError("the embeddings API answered 500")
        self.calls.append(list(texts))
        vectors = []
        for text in texts:
            seed = hashlib.sha256(text.encode()).digest()
            vectors.append([((seed[i % 32] / 255.0) - 0.5) for i in range(self.dims)])
        return vectors, sum(len(t) // 4 for t in texts)


def live_document(conn, monkeypatch, pages=PAGES, **kw):
    import crawler.kb
    from crawler.kb import build, publish
    doc, mid, name = document(conn, **kw)
    monkeypatch.setattr(crawler.kb, "document_pages", lambda c, d, **k: pages)
    number = build(conn, doc)
    assert publish(conn, doc, number)[0]
    return doc, mid, name


def vectors(conn, doc):
    return q(conn, f"SELECT v.section_id, v.is_live, v.manufacturer_id, v.doc_type FROM {TABLE} v "
                   "JOIN sections s ON s.id = v.section_id WHERE s.document_id = %s ORDER BY 1", (doc,))


def test_every_live_section_gets_one_vector_with_its_filters_and_a_second_pass_calls_nothing(runtime_conn, monkeypatch):
    from crawler.embed import embed_pass
    doc, mid, _ = live_document(runtime_conn, monkeypatch)
    client = FakeEmbedder()
    summary = embed_pass(runtime_conn, client, cap_usd=5)
    sections = [r[0] for r in q(runtime_conn, "SELECT id FROM sections WHERE document_id = %s AND is_live ORDER BY id",
                                (doc,))]
    assert [r[0] for r in vectors(runtime_conn, doc)] == sections
    assert all(r[1] is True and r[2] == mid and r[3] == "install_manual" for r in vectors(runtime_conn, doc))
    assert summary["embedded"] == len(sections) and len(client.calls) == 1
    again = FakeEmbedder()
    assert embed_pass(runtime_conn, again, cap_usd=5)["embedded"] == 0 and again.calls == []


def test_the_text_embedded_starts_with_a_header_naming_maker_type_title_path_and_pages(runtime_conn, monkeypatch):
    from crawler.embed import embed_pass
    _, _, name = live_document(runtime_conn, monkeypatch)
    client = FakeEmbedder()
    embed_pass(runtime_conn, client, cap_usd=5)
    first = client.calls[0][0]
    assert first.splitlines()[0] == f"{name} · Install manual · Install manual · Refrigerant Charging · p.1"
    assert "Error E4 means LO PS tripped." in first


def test_a_rebuilt_section_with_the_same_text_reuses_its_vector_without_a_call(runtime_conn, monkeypatch):
    import crawler.kb
    from crawler.embed import embed_pass
    from crawler.kb import build, publish
    doc, _, _ = live_document(runtime_conn, monkeypatch)
    embed_pass(runtime_conn, FakeEmbedder(), cap_usd=5)
    monkeypatch.setattr(crawler.kb, "SECTIONER_VERSION", "test-next")
    real_terms = crawler.kb.terms
    monkeypatch.setattr(crawler.kb, "terms", lambda text: real_terms(text) | {"NEWTERM"})  # a new build, same text
    number = build(runtime_conn, doc)
    assert number == 2 and publish(runtime_conn, doc, number)[0]
    client = FakeEmbedder()
    summary = embed_pass(runtime_conn, client, cap_usd=5)
    assert client.calls == [] and summary["reused"] == len(live(runtime_conn, doc)) and summary["embedded"] == 0
    assert all(r[1] for r in vectors(runtime_conn, doc) if r[0] in {s[0] for s in q(
        runtime_conn, "SELECT id FROM sections WHERE document_id = %s AND build = 2", (doc,))})


def test_each_request_is_logged_with_its_cost(runtime_conn, monkeypatch):
    from crawler.embed import embed_pass
    live_document(runtime_conn, monkeypatch)
    client = FakeEmbedder()
    embed_pass(runtime_conn, client, cap_usd=5)
    rows = q(runtime_conn, "SELECT purpose, model, input_tokens, cost_usd FROM ai_calls WHERE purpose = 'embed'")
    billed = sum(len(t) // 4 for t in client.calls[0])
    price = (Decimal(billed) * Decimal("0.02") / 1_000_000).quantize(Decimal("0.000001"))
    assert rows == (("embed", "text-embedding-3-small", billed, price),)


def test_the_monthly_cap_holds(runtime_conn, monkeypatch):
    from crawler.embed import embed_pass
    doc, _, _ = live_document(runtime_conn, monkeypatch)
    q(runtime_conn, "INSERT INTO ai_calls (purpose, model, cost_usd) VALUES ('tag', 'claude-haiku-5-5', 4.9999999)")
    client = FakeEmbedder()
    summary = embed_pass(runtime_conn, client, cap_usd=5)
    assert client.calls == [] and summary["capped"] and vectors(runtime_conn, doc) == ()


def test_requests_are_split_so_one_stays_small(runtime_conn, monkeypatch):
    import crawler.embed
    from crawler.embed import embed_pass
    live_document(runtime_conn, monkeypatch)
    monkeypatch.setattr(crawler.embed, "MAX_INPUTS_PER_REQUEST", 1)
    client = FakeEmbedder()
    embed_pass(runtime_conn, client, cap_usd=5)
    assert len(client.calls) == len(q(runtime_conn, "SELECT 1 FROM sections WHERE is_live")) and all(
        len(c) == 1 for c in client.calls)


def test_an_api_error_stops_the_pass_and_keeps_what_was_done(runtime_conn, monkeypatch):
    from crawler.embed import embed_pass
    doc, _, _ = live_document(runtime_conn, monkeypatch)
    summary = embed_pass(runtime_conn, FakeEmbedder(fail=True), cap_usd=5)
    assert summary["error"] == "the embeddings API answered 500" and vectors(runtime_conn, doc) == ()


def test_hiding_a_manual_takes_its_vectors_out_too_and_unhide_brings_them_back(runtime_conn, monkeypatch):
    from crawler.embed import embed_pass
    from crawler.kb import hide, unhide
    doc, _, name = live_document(runtime_conn, monkeypatch)
    embed_pass(runtime_conn, FakeEmbedder(), cap_usd=5)
    hide(runtime_conn, "manufacturer", name, reason="legal", by_whom="tester")
    assert not any(r[1] for r in vectors(runtime_conn, doc))
    unhide(runtime_conn, "manufacturer", name, reason="ok", by_whom="tester")
    assert all(r[1] for r in vectors(runtime_conn, doc))


def test_a_retag_keeps_the_vector_filters_true(runtime_conn, monkeypatch):
    from crawler.embed import embed_pass
    doc, _, _ = live_document(runtime_conn, monkeypatch)
    embed_pass(runtime_conn, FakeEmbedder(), cap_usd=5)
    q(runtime_conn, "UPDATE sections SET doc_type = 'service_manual' WHERE document_id = %s", (doc,))
    assert {r[3] for r in vectors(runtime_conn, doc)} == {"service_manual"}


def test_the_runtime_role_cannot_delete_vectors_and_breeze_can_read_them(runtime_conn, migrate_conn):
    import psycopg
    rows = q(migrate_conn, "SELECT grantee, privilege_type FROM information_schema.role_table_grants "
                           "WHERE table_schema = 'kb' AND table_name = %s", (TABLE,))
    granted = {(g, p) for g, p in rows}
    assert ("eq_breeze", "SELECT") in granted and ("eq_runtime", "INSERT") in granted
    assert ("eq_runtime", "DELETE") not in granted and ("eq_breeze", "INSERT") not in granted
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        q(runtime_conn, f"DELETE FROM {TABLE}")


def test_check_counts_live_sections_without_a_vector(runtime_conn, monkeypatch):
    from crawler.embed import embed_pass
    from crawler.kb import check
    live_document(runtime_conn, monkeypatch)
    before = check(runtime_conn)["live_without_vectors"]
    assert before == len(q(runtime_conn, "SELECT 1 FROM sections WHERE is_live"))
    embed_pass(runtime_conn, FakeEmbedder(), cap_usd=5)
    assert check(runtime_conn)["live_without_vectors"] == 0


def test_without_a_key_the_real_client_says_which_one_is_missing(monkeypatch):
    from crawler.embed import OpenAIEmbedder
    monkeypatch.delenv("OPENAI_API_KEY_CRAWLER", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY_CRAWLER"):
        OpenAIEmbedder()


def test_the_embed_command_reports_what_it_did_and_says_when_there_is_no_key(runtime_conn, monkeypatch):
    import crawler.embed
    from test_p11_cli import cli
    live_document(runtime_conn, monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY_CRAWLER", raising=False)
    code, out, err = cli(runtime_conn, "embed")
    assert code == 0, err
    assert "No OpenAI key is set (OPENAI_API_KEY_CRAWLER)" in out
    monkeypatch.setattr(crawler.embed, "OpenAIEmbedder", FakeEmbedder)
    code, out, err = cli(runtime_conn, "embed")
    assert code == 0, err
    assert out.startswith("Embedded 3 sections in 1 requests") and "0 reused" in out
    code, out, _ = cli(runtime_conn, "check")
    assert "0 live sections have no vector yet" in out


def test_prune_removes_the_vectors_of_builds_that_can_never_be_live_again(monkeypatch):
    import crawler.kb
    from support import connect
    from crawler.embed import embed_pass
    from crawler.kb import build, prune, publish
    from test_p12_build import t, words
    conn = connect("migrate", autocommit=False)  # prune runs as the migrate role
    try:
        doc, _, _ = live_document(conn, monkeypatch)
        embed_pass(conn, FakeEmbedder(), cap_usd=5)
        old = {r[0] for r in vectors(conn, doc)}
        monkeypatch.setattr(crawler.kb, "document_pages", lambda c, d, **k: [[t("Charging", 14), t(words(320, "new"))]])
        assert publish(conn, doc, build(conn, doc))[0]
        embed_pass(conn, FakeEmbedder(), cap_usd=5)
        gone = prune(conn)
        assert gone["vectors"] == len(old) and not old & {r[0] for r in vectors(conn, doc)} and vectors(conn, doc) != ()
    finally:
        conn.rollback()
        conn.close()


def test_a_text_in_a_script_that_costs_many_tokens_a_letter_is_cut_to_fit_the_models_limit():
    # garbled Arabic (presentation forms and U+FFFD): 5,031 characters were over 8,192 tokens, and the API refused it
    from crawler.embed import MAX_INPUT_TOKENS, _estimate, embed_text
    text = embed_text("Ruud", "install_manual", "Manual", "Section", 1, 1, "ﻲ�ﺎ�ﺮهﻜﻟا�قوﺪﻨﺼﻟا " * 600)
    assert _estimate(text) <= MAX_INPUT_TOKENS and text.startswith("Ruud · Install manual")
    english = embed_text("Ruud", "install_manual", "Manual", "Section", 1, 1, "Check the charge. " * 400)
    assert english.endswith("Check the charge. ")  # ordinary text of that length is not cut


def test_when_the_api_refuses_a_request_its_reason_is_kept(monkeypatch):
    import httpx
    from crawler.embed import EmbedError, OpenAIEmbedder

    class Refused:
        status_code = 400

        def json(self):
            return {"error": {"message": "Invalid 'input[9]': maximum input length is 8192 tokens."}}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: Refused())
    with pytest.raises(EmbedError, match=r"400: Invalid 'input\[9\]': maximum input length is 8192 tokens"):
        OpenAIEmbedder(api_key="test-key").embed(["x"])
