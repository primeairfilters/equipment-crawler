"""The brand of an upload with none picked on the page ("Work it out from the file"), user 2026-11-01. In order:
1. the brand picked on the page;
2. the brand the file's name names (one brand, named once is enough: "Fujitsu AirStage H-Series – Document Links.xlsx");
3. the brand the text names most (at least twice, more than any other: as before);
4. the brand the AI read in the document when it tags it, if it is printed there (a web address counts:
   docs.fujitsugeneral.com) and is a brand already on the crawler's list. It never replaces one found earlier.

Local library in a tmp folder (DOCS_DIR, inbox/ inside it), fake AI, `_testing`, one rolled-back transaction per test."""
import uuid

from fakeai import FakeAI
from p11support import world  # noqa: F401  (world is a fixture)
from p3support import q
from test_auto_run import auto
from test_p11b_tagging import tags
from test_uploads import a_pdf, discovered, maker_of, outcome, process, queue


def new_brand(world):
    from crawler.brands import add_brand
    name = "Zorbex" + uuid.uuid4().hex[:6]
    from crawler.brands import stop
    brand_id = add_brand(world.conn, name)
    stop(world.conn, name)  # paused, so the nightly run does not try to crawl it
    return name, brand_id


def maker_of_document(conn, upload):
    document_id = outcome(conn, upload)[2]
    return q(conn, "SELECT manufacturer_id FROM documents WHERE id = %s", (document_id,))[0][0], document_id


# ---------------------------------------------------------------- the file's name

def test_a_brand_named_in_the_file_name_is_used(world):
    name, brand_id = new_brand(world)
    upload = queue(world.conn, a_pdf("Document links", "Model TX1000"), name=f"{name} AirStage H-Series – Document Links.pdf")
    process(world.conn)
    assert outcome(world.conn, upload)[3] == maker_of(world.conn, brand_id)
    assert maker_of_document(world.conn, upload)[0] == maker_of(world.conn, brand_id)


def test_a_file_name_with_underscores_names_its_brand_too(world):
    name, brand_id = new_brand(world)
    upload = queue(world.conn, a_pdf("Wiring", "Model TX1000"), name=f"{name}_wiring_2024.pdf")
    process(world.conn)
    assert outcome(world.conn, upload)[3] == maker_of(world.conn, brand_id)


def test_a_file_name_that_names_two_brands_leaves_it_to_the_text(world):
    one, _ = new_brand(world)
    two, _ = new_brand(world)
    upload = queue(world.conn, a_pdf("Cross reference", "Model TX1000"), name=f"{one} to {two} cross reference.pdf")
    process(world.conn)
    assert outcome(world.conn, upload)[3] is None


def test_the_brand_picked_on_the_page_wins_over_the_file_name(world):
    name, _ = new_brand(world)
    upload = queue(world.conn, a_pdf("Notes", "Model TX1000"), name=f"{name} notes.pdf", brand=world.brand)
    process(world.conn)
    assert outcome(world.conn, upload)[3] == maker_of(world.conn, world.brand_id)


# ---------------------------------------------------------------- the AI, when nothing else found one

def test_the_ai_reads_the_brand_when_nothing_else_found_one(world):
    name, brand_id = new_brand(world)
    discovered(world)
    body = a_pdf("Document index", f"https://docs.{name.lower()}general.com/assets/manual.pdf", "Model RA15AZ24AJ3CA")
    upload = queue(world.conn, body, name="Document Links.pdf")
    process(world.conn)
    assert outcome(world.conn, upload)[3] is None  # nothing named it before tagging
    auto(world, ai_client=FakeAI(lambda r: tags(brand=f"{name} General")))
    maker, document_id = maker_of_document(world.conn, upload)
    assert maker == maker_of(world.conn, brand_id)
    assert outcome(world.conn, upload)[3] == maker
    assert q(world.conn, "SELECT manufacturer_id FROM document_brands WHERE document_id = %s", (document_id,)) == ((maker,),)
    assert q(world.conn, "SELECT DISTINCT manufacturer_id FROM document_models WHERE document_id = %s",
             (document_id,)) == ((maker,),)


def test_a_brand_the_ai_did_not_read_in_the_document_is_ignored(world):
    name, _ = new_brand(world)
    discovered(world)
    upload = queue(world.conn, a_pdf("Generic thermostat wiring guide", "Model TX1000"), name="Wiring.pdf")
    process(world.conn)
    auto(world, ai_client=FakeAI(lambda r: tags(brand=name)))  # outside knowledge: not printed in the text
    assert maker_of_document(world.conn, upload)[0] is None


def test_a_brand_that_is_not_on_the_list_is_ignored(world):
    discovered(world)
    unknown = "Quorvanta" + uuid.uuid4().hex[:6]
    upload = queue(world.conn, a_pdf(f"{unknown} heat pump guide", "Model TX1000"), name="Guide.pdf")
    process(world.conn)
    auto(world, ai_client=FakeAI(lambda r: tags(brand=unknown)))
    assert maker_of_document(world.conn, upload)[0] is None
    assert not q(world.conn, "SELECT 1 FROM brands WHERE name = %s", (unknown,))  # no brand is made from an answer


def test_the_ai_never_replaces_a_brand_found_before_tagging(world):
    name, _ = new_brand(world)
    discovered(world)
    upload = queue(world.conn, a_pdf(f"Replaces a {name} unit", "Model TX1000"), name="Notes.pdf", brand=world.brand)
    process(world.conn)
    auto(world, ai_client=FakeAI(lambda r: tags(brand=name)))
    assert maker_of_document(world.conn, upload)[0] == maker_of(world.conn, world.brand_id)


def test_the_tagger_is_asked_for_the_brand():
    from crawler.extract import OUTPUT_SCHEMA, SYSTEM
    assert OUTPUT_SCHEMA["properties"]["brand"] == {"type": "string"} and "brand" in OUTPUT_SCHEMA["required"]
    assert "- brand is" in SYSTEM


def test_the_tagger_is_told_service_bulletins_are_equipment_documents():
    # user 2026-11-01: a saved Ruud service bulletin (DS-AH102301, with an email footer) came back not_equipment
    from crawler.extract import SYSTEM
    assert "Service bulletins and service notes about equipment are never not_equipment" in SYSTEM
