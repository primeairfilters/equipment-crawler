"""Tags set by hand (user, 2026-10-29): `./crawl tag <document> --doc-type/--equipment-type/--trade`. The AI tags from a
manual's first pages and gets some wrong (doc 800, a 255-page install and service manual, as spec_sheet; doc 80, a
ductless manual, as heat_pump). A tag the user sets is copied to the live sections at once and never overwritten when
the AI tags the document again."""
import pytest

from p3support import q
from test_p13_embed import live_document
from test_small_fixes import twin_documents


def tags(conn, doc):
    return q(conn, "SELECT doc_type, equipment_type, trade, user_tags FROM documents WHERE id = %s", (doc,))[0]


def section_tags(conn, doc):
    return set(q(conn, "SELECT doc_type, equipment_type, trade FROM sections WHERE document_id = %s AND is_live", (doc,)))


def live_count(conn, doc):
    return q(conn, "SELECT COUNT(*) FROM sections WHERE document_id = %s AND is_live", (doc,))[0][0]


def test_a_tag_set_by_hand_reaches_the_document_and_its_live_sections(runtime_conn, monkeypatch):
    from crawler.kb import set_tags
    doc, _, _ = live_document(runtime_conn, monkeypatch)
    set_tags(runtime_conn, doc, doc_type="service_manual", equipment_type="ductless_mini_split")
    assert tags(runtime_conn, doc) == ("service_manual", "ductless_mini_split", "hvac", ["doc_type", "equipment_type"])
    assert section_tags(runtime_conn, doc) == {("service_manual", "ductless_mini_split", "hvac")}


def test_only_the_values_the_ai_may_use_are_accepted(runtime_conn, monkeypatch):
    from crawler.kb import set_tags
    doc, _, _ = live_document(runtime_conn, monkeypatch)
    with pytest.raises(ValueError, match="doc_type"):
        set_tags(runtime_conn, doc, doc_type="manual")
    with pytest.raises(ValueError, match="nothing to set"):
        set_tags(runtime_conn, doc)
    from crawler.brands import UnknownTarget
    with pytest.raises(UnknownTarget, match="No document"):
        set_tags(runtime_conn, 987654321, doc_type="install_manual")


def ai_tags(conn, doc, **tag_values):
    from crawler.store import save_document
    sha, page_id, url, brand_id = q(conn, "SELECT d.content_sha, du.page_id, du.url, p.brand_id FROM documents d "
                                          "JOIN document_urls du ON du.document_id = d.id JOIN pages p ON p.id = du.page_id "
                                          "WHERE d.id = %s LIMIT 1", (doc,))[0]
    save_document(conn, brand_id=brand_id, page_id=page_id, source_url=url, title="RA15AZ", content_sha=sha, models=[],
                  extracted_by="ai:claude-haiku-5-5", doc_type=tag_values.pop("doc_type"), tags=tag_values)


def test_the_ai_tagging_the_document_again_keeps_the_tags_set_by_hand(runtime_conn):
    from crawler.kb import set_tags
    _, (doc, _) = twin_documents(runtime_conn)
    ai_tags(runtime_conn, doc, doc_type="spec_sheet", equipment_type="heat_pump", trade="hvac")
    set_tags(runtime_conn, doc, doc_type="install_manual")
    ai_tags(runtime_conn, doc, doc_type="spec_sheet", equipment_type="ductless_mini_split", trade="hvac")
    assert tags(runtime_conn, doc)[:3] == ("install_manual", "ductless_mini_split", "hvac")  # the AI still sets the rest


def test_tagged_not_equipment_by_hand_leaves_breeze_and_tagged_back_returns(runtime_conn, monkeypatch):
    from crawler.kb import set_tags
    doc, _, _ = live_document(runtime_conn, monkeypatch)
    before = live_count(runtime_conn, doc)
    set_tags(runtime_conn, doc, doc_type="not_equipment")
    assert live_count(runtime_conn, doc) == 0
    set_tags(runtime_conn, doc, doc_type="install_manual")
    assert live_count(runtime_conn, doc) == before


def test_the_tag_command_sets_tags_and_check_lists_them(runtime_conn, monkeypatch):
    from test_p11_cli import cli
    doc, _, _ = live_document(runtime_conn, monkeypatch)
    code, out, err = cli(runtime_conn, "tag", str(doc), "--doc-type", "install_manual", "--trade", "hvac")
    assert code == 0, err
    assert f"Document {doc}" in out and "doc_type install_manual" in out
    code, out, err = cli(runtime_conn, "check")
    assert f"tags set by hand: {doc} (doc_type install_manual, trade hvac)" in out
