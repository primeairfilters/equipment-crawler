"""P11b: one Batch tagging call per document now also returns trade, equipment type, language, the printed form
number and title, and a not_equipment type. Tags land on the document (and the models it covers). Rules check what
can be checked: a form number must be printed on the sent pages; a value outside its list is dropped, not guessed.

Fake client, `_testing`, one rolled-back transaction per test. No network and no key.
"""
import pytest

from fakeai import FakeAI
from p3support import make_page, make_pdf, new_brand, q, settle

MODEL = "RA15AZ24AJ3CA"
TEXT = [f"Ruud RA15AZ Installation Instructions", f"Model {MODEL}", "Form No. A22-336 Rev. 4", "SEER2 15.2"]


def tags(doc_type="install_manual", models=(MODEL,), **extra):
    out = {"doc_type": doc_type, "models": list(models), "trade": "hvac", "equipment_type": "air_conditioner",
           "language": "en", "form_number": "A22-336", "title": "RA15AZ Installation Instructions"}
    out.update(extra)
    return out


def tagged(conn, answer, lines=TEXT):
    name, brand_id = new_brand(conn)
    page_id = make_page(conn, brand_id, make_pdf([lines]), is_pdf=True)
    fake = FakeAI(lambda r: answer)
    settle(conn, fake, name)
    assert len(fake.requests) == 1  # the document really was sent
    doc = q(conn, "SELECT d.id, d.doc_type, d.trade, d.equipment_type, d.language, d.form_number, d.title, d.state, "
                  "d.tagger_version, d.tagged_by FROM documents d JOIN document_urls du ON du.document_id = d.id "
                  "WHERE du.page_id = %s", (page_id,))
    return name, brand_id, page_id, (doc[0] if doc else None), fake


def test_the_schema_asks_for_every_tag():
    from crawler.extract import DOC_TYPES, EQUIPMENT_TYPES, OUTPUT_SCHEMA, TRADES
    props = OUTPUT_SCHEMA["properties"]
    assert set(props) == {"doc_type", "models", "trade", "equipment_type", "language", "form_number", "title"}
    assert set(OUTPUT_SCHEMA["required"]) == set(props) and OUTPUT_SCHEMA["additionalProperties"] is False
    assert "not_equipment" in DOC_TYPES and props["doc_type"]["enum"] == DOC_TYPES
    assert props["trade"]["enum"] == TRADES and "hvac" in TRADES
    assert props["equipment_type"]["enum"] == EQUIPMENT_TYPES and "heat_pump" in EQUIPMENT_TYPES


def test_the_extractor_version_is_3_so_documents_tagged_before_are_tagged_again_once():
    from crawler.extract import EXTRACTOR_VERSION
    assert EXTRACTOR_VERSION == "3"


def test_every_tag_is_stored_on_the_document(runtime_conn):
    from crawler.extract import EXTRACTOR_VERSION
    _, _, _, doc, _ = tagged(runtime_conn, tags())
    assert doc[1:] == ("install_manual", "hvac", "air_conditioner", "en", "A22-336", "RA15AZ Installation Instructions",
                       "tagged", EXTRACTOR_VERSION, "ai:claude-haiku-5-5")


def test_the_models_it_covers_get_the_trade_and_equipment_type(runtime_conn):
    name, *_ = tagged(runtime_conn, tags())
    assert q(runtime_conn, "SELECT m.trade, m.equipment_type FROM models m JOIN manufacturers f ON f.id = m.manufacturer_id "
                           "WHERE f.name = %s", (name,)) == (("hvac", "air_conditioner"),)


def test_a_form_number_that_is_not_printed_is_dropped(runtime_conn):
    _, _, _, doc, _ = tagged(runtime_conn, tags(form_number="Z99-999"))
    assert doc[5] is None and doc[1] == "install_manual"  # the rest of the answer still counts


@pytest.mark.parametrize("field,bad", [("trade", "rockets"), ("equipment_type", "spaceship"), ("language", "english"),
                                       ("language", "EN1"), ("title", "x" * 600)])
def test_a_tag_outside_its_list_or_shape_is_dropped_not_guessed(runtime_conn, field, bad):
    _, _, page_id, doc, _ = tagged(runtime_conn, tags(**{field: bad}))
    column = {"trade": 2, "equipment_type": 3, "language": 4, "title": 6}[field]
    assert doc[column] is None
    assert q(runtime_conn, "SELECT extraction_status FROM pages WHERE id = %s", (page_id,)) == (("extracted",),)


def test_an_empty_form_number_or_title_means_none_printed(runtime_conn):
    _, _, _, doc, _ = tagged(runtime_conn, tags(form_number="", title=""))
    assert doc[5] is None and doc[6] is None


def test_a_not_equipment_document_is_stored_with_that_type(runtime_conn):
    _, _, _, doc, _ = tagged(runtime_conn, tags(doc_type="not_equipment", models=(), trade="other",
                                                equipment_type="other", form_number="", title="Annual Report 2025"),
                             lines=["Annual Report 2025", "Shareholder letter", "Revenue grew"])
    assert doc[1] == "not_equipment" and doc[7] == "tagged"


def test_a_second_pass_makes_zero_ai_calls(runtime_conn):
    name, _, _, _, fake = tagged(runtime_conn, tags())
    calls = list(fake.calls)
    settle(runtime_conn, fake, name)
    assert fake.calls == calls


def test_a_new_version_with_the_same_printed_form_number_replaces_the_old_one(runtime_conn):
    """D55: a manual moved to a new address is linked to the one it replaces by manufacturer + form number."""
    from crawler.extract import extract_pass  # noqa: F401  (settle runs it)
    name, brand_id = new_brand(runtime_conn)
    old = make_page(runtime_conn, brand_id, make_pdf([TEXT + ["Revision 3"]]), is_pdf=True, url="http://p3.test/old.pdf")
    fake = FakeAI(lambda r: tags())
    settle(runtime_conn, fake, name)
    new = make_page(runtime_conn, brand_id, make_pdf([TEXT + ["Revision 4"]]), is_pdf=True, url="http://p3.test/new.pdf")
    settle(runtime_conn, fake, name)
    old_doc, new_doc = (q(runtime_conn, "SELECT document_id FROM document_urls WHERE page_id = %s", (p,))[0][0]
                        for p in (old, new))
    assert old_doc != new_doc
    assert q(runtime_conn, "SELECT replaces_id FROM documents WHERE id = %s", (new_doc,)) == ((old_doc,),)
    assert q(runtime_conn, "SELECT replaces_id FROM documents WHERE id = %s", (old_doc,)) == ((None,),)


def test_the_prompt_treats_document_text_as_data():
    from crawler.extract import SYSTEM
    assert "instruction" in SYSTEM.lower() and "data" in SYSTEM.lower()
