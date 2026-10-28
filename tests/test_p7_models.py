"""P7: the AI tags each PDF's type and lists the model numbers it covers; passing models are linked to the document.

Fake client, `_testing` database, one rolled-back transaction per test. No network and no key.
The answer shape is {"doc_type": <one of seven>, "models": ["RA15AZ24AJ3CA", ...]}. Only PDFs are
read; HTML is never sent. A model is linked only if its key is at least 5 characters and is printed
on the sent pages. (Ported to the P10 schema: documents, document_urls, document_models.)
"""
import re

import pytest

from fakeai import FakeAI
from p3support import make_doc, make_page, make_pdf, new_brand, q, settle
from test_p1_migrate import migrate_ok

MODEL = "RA15AZ24AJ3CA"
DOC_TYPES = ["spec_sheet", "install_manual", "service_manual", "parts_list", "warranty", "brochure", "other"]


@pytest.fixture(scope="module", autouse=True)
def _migrated():
    """`migrate` only adds, and the testing database needs the current schema before the tests below."""
    migrate_ok()


def reply(doc_type, *models):
    return {"doc_type": doc_type, "models": list(models)}


def pdf_doc(conn, brand_id, pages):
    """A fetched PDF whose text is real (parsed by the PDF child). pages: list of lists of lines."""
    return make_page(conn, brand_id, make_pdf(pages), is_pdf=True)


def status(conn, page_id):
    return q(conn, "SELECT extraction_status FROM pages WHERE id = %s", (page_id,))[0][0]


def linked_keys(conn, brand_name):
    return {r[0] for r in q(conn, "SELECT m.model_key FROM document_models ms "
                                  "JOIN models m ON m.id = ms.model_id "
                                  "JOIN manufacturers f ON f.id = m.manufacturer_id WHERE f.name = %s", (brand_name,))}


def model_keys(conn, brand_name):
    return {r[0] for r in q(conn, "SELECT m.model_key FROM models m JOIN manufacturers f "
                                  "ON f.id = m.manufacturer_id WHERE f.name = %s", (brand_name,))}


def doc_type_of(conn, page_id):
    rows = q(conn, "SELECT s.doc_type FROM documents s JOIN document_urls du ON du.document_id = s.id WHERE du.page_id = %s", (page_id,))
    return rows[0][0] if rows else "NO SOURCE ROW"


def count(conn, table):
    return q(conn, f"SELECT COUNT(*) FROM {table}")[0][0]


# ---------------------------------------------------------------- the schema and the budget

def test_the_answer_schema_has_a_doc_type_enum_and_a_plain_list_of_model_strings():
    """P11b added more tags (test_p11b_tagging.py); the type and the model list are unchanged."""
    from crawler.extract import OUTPUT_SCHEMA
    props = OUTPUT_SCHEMA["properties"]
    assert {"doc_type", "models"} <= set(props)
    assert props["doc_type"]["enum"] == DOC_TYPES + ["not_equipment"]
    assert props["models"]["type"] == "array" and props["models"]["items"] == {"type": "string"}
    assert {"doc_type", "models"} <= set(OUTPUT_SCHEMA["required"])
    assert OUTPUT_SCHEMA["additionalProperties"] is False


def test_the_default_answer_budget_is_4000_tokens_and_every_request_carries_it(runtime_conn):
    from crawler.extract import DEFAULT_MAX_TOKENS
    assert DEFAULT_MAX_TOKENS == 4000
    name, brand_id = new_brand(runtime_conn)
    pdf_doc(runtime_conn, brand_id, [[f"Model {MODEL}"]])
    fake = FakeAI(lambda r: reply("spec_sheet", MODEL))
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 1  # the path ran
    assert fake.requests[0].max_tokens == 4000


def test_the_extractor_version_moved_on_from_2():
    """P11b made it 3 (test_p11b_tagging.py), so documents tagged under 2 are tagged again once."""
    from crawler.extract import EXTRACTOR_VERSION
    assert EXTRACTOR_VERSION != "2"


# ---------------------------------------------------------------- linking

def test_a_pdf_is_read_once_and_its_type_and_model_are_linked(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    page_id = pdf_doc(runtime_conn, brand_id, [[f"Model {MODEL}", "Service manual"]])
    fake = FakeAI(lambda r: reply("service_manual", MODEL))
    settle(runtime_conn, fake, name)

    assert len(fake.requests) == 1
    assert status(runtime_conn, page_id) == "extracted"
    assert doc_type_of(runtime_conn, page_id) == "service_manual"
    assert model_keys(runtime_conn, name) == {MODEL}
    assert linked_keys(runtime_conn, name) == {MODEL}
    src = q(runtime_conn, "SELECT du.url FROM documents s JOIN document_models ms ON ms.document_id = s.id "
                          "JOIN document_urls du ON du.document_id = s.id "
                          "JOIN models m ON m.id = ms.model_id JOIN manufacturers f ON f.id = m.manufacturer_id "
                          "WHERE f.name = %s", (name,))
    assert src == ((q(runtime_conn, "SELECT url FROM pages WHERE id = %s", (page_id,))[0][0],),)
    assert q(runtime_conn, "SELECT dm.model_key FROM document_models dm JOIN document_urls du ON du.document_id = dm.document_id WHERE du.page_id = %s", (page_id,)) == ((MODEL,),)


@pytest.mark.parametrize("doc_type", DOC_TYPES)
def test_each_of_the_seven_document_types_is_stored(runtime_conn, doc_type):
    name, brand_id = new_brand(runtime_conn)
    page_id = pdf_doc(runtime_conn, brand_id, [[f"Model {MODEL}"]])
    fake = FakeAI(lambda r: reply(doc_type, MODEL))
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 1
    assert doc_type_of(runtime_conn, page_id) == doc_type


def test_every_model_that_passes_is_linked(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    models = ["RA15AZ24AJ3CA", "RA15AZ30AJ3CA", "RA15AZ36AJ3CA"]
    pdf_doc(runtime_conn, brand_id, [[f"Model {models[0]}", f"Model {models[1]}"], [f"Model {models[2]}"]])
    fake = FakeAI(lambda r: reply("spec_sheet", *models))
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 1
    assert linked_keys(runtime_conn, name) == set(models)
    assert q(runtime_conn, "SELECT COUNT(*) FROM document_models ms JOIN models m ON m.id = ms.model_id "
                           "JOIN manufacturers f ON f.id = m.manufacturer_id WHERE f.name = %s", (name,))[0][0] == 3


def test_a_model_that_is_not_on_the_page_is_not_linked_and_not_stored(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    page_id = pdf_doc(runtime_conn, brand_id, [[f"Model {MODEL}"]])
    fake = FakeAI(lambda r: reply("spec_sheet", MODEL, "ZZ999FAKE123"))
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 1                                   # the path ran
    assert linked_keys(runtime_conn, name) == {MODEL}                # control: the real one was linked
    assert model_keys(runtime_conn, name) == {MODEL}                 # the made-up one has no model row either
    assert q(runtime_conn, "SELECT COUNT(*) FROM ai_calls WHERE page_id = %s AND NOT rejected", (page_id,))[0][0] == 1


def test_a_model_key_under_5_characters_is_not_linked_even_when_it_is_on_the_page(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    pdf_doc(runtime_conn, brand_id, [[f"Model {MODEL}", "Series RA15", "Part RA15A", "Code R-410", "Plan AB-1"]])
    fake = FakeAI(lambda r: reply("spec_sheet", MODEL, "RA15", "RA-15", "R-410", "AB-1", "RA15A"))
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 1
    assert linked_keys(runtime_conn, name) == {MODEL, "RA15A"}       # 5 characters is the smallest that links
    assert model_keys(runtime_conn, name) == {MODEL, "RA15A"}        # the 4-character keys were never stored


def test_a_partial_model_printed_on_the_page_is_linked_when_it_is_5_or_more_characters(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    pdf_doc(runtime_conn, brand_id, [["Series RA15AZ condensing units"]])
    fake = FakeAI(lambda r: reply("brochure", "RA15AZ"))
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 1
    assert linked_keys(runtime_conn, name) == {"RA15AZ"}


def test_a_document_with_no_models_is_still_typed_and_ends_extracted(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    page_id = pdf_doc(runtime_conn, brand_id, [["Warranty terms and conditions apply to all units"]])
    fake = FakeAI(lambda r: reply("warranty"))
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 1
    assert status(runtime_conn, page_id) == "extracted"
    assert doc_type_of(runtime_conn, page_id) == "warranty"
    assert linked_keys(runtime_conn, name) == set()


# ---------------------------------------------------------------- a doc_type outside the list

@pytest.mark.parametrize("bad_type", ["cookbook", "SPEC_SHEET", "", None])
def test_a_doc_type_outside_the_list_is_rejected_and_nothing_reaches_the_equipment_tables(runtime_conn, bad_type):
    name, brand_id = new_brand(runtime_conn)
    page_id = pdf_doc(runtime_conn, brand_id, [[f"Model {MODEL}"]])
    fake = FakeAI(lambda r: {"doc_type": bad_type, "models": [MODEL]}, usage=lambda r, counted: (500, 20))
    settle(runtime_conn, fake, name)

    assert len(fake.requests) == 1                                   # the path ran and was billed
    rows = q(runtime_conn, "SELECT rejected, cost_usd FROM ai_calls WHERE page_id = %s", (page_id,))
    assert len(rows) == 1 and rows[0][0] == 1 and rows[0][1] > 0
    assert status(runtime_conn, page_id) == "failed"
    assert q(runtime_conn, "SELECT COUNT(*) FROM manufacturers WHERE name = %s", (name,))[0][0] == 0
    assert q(runtime_conn, "SELECT COUNT(*) FROM document_models dm JOIN document_urls du ON du.document_id = dm.document_id WHERE du.page_id = %s", (page_id,))[0][0] == 0
    assert q(runtime_conn, "SELECT COUNT(*) FROM documents s JOIN document_urls du ON du.document_id = s.id WHERE du.page_id = %s",
             (page_id,))[0][0] == 0

    calls = list(fake.calls)
    settle(runtime_conn, fake, name)
    assert fake.calls == calls                                       # a paid failure is not retried on unchanged text


# ---------------------------------------------------------------- never twice

def test_a_second_pass_over_an_unchanged_pdf_makes_zero_calls(runtime_conn):
    from crawler.extract import EXTRACTOR_VERSION
    name, brand_id = new_brand(runtime_conn)
    page_id = pdf_doc(runtime_conn, brand_id, [[f"Model {MODEL}"]])
    fake = FakeAI(lambda r: reply("spec_sheet", MODEL))
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 1                                   # it really was read the first time
    row = q(runtime_conn, "SELECT ai_extracted_sha, ai_extractor_version FROM pages WHERE id = %s", (page_id,))[0]
    assert row[0] and row[1] == EXTRACTOR_VERSION

    calls, billed = list(fake.calls), count(runtime_conn, "ai_calls")
    links = count(runtime_conn, "document_models")
    settle(runtime_conn, fake, name)
    assert fake.calls == calls                                       # not even a token count
    assert count(runtime_conn, "ai_calls") == billed
    assert count(runtime_conn, "document_models") == links


def test_a_pdf_read_under_an_older_version_is_read_once_more_under_the_current_one(runtime_conn, monkeypatch):
    import crawler.extract
    real = crawler.extract.EXTRACTOR_VERSION
    assert real != "1"

    name, brand_id = new_brand(runtime_conn)
    page_id = pdf_doc(runtime_conn, brand_id, [[f"Model {MODEL}"]])
    fake = FakeAI(lambda r: reply("spec_sheet", MODEL))

    monkeypatch.setattr(crawler.extract, "EXTRACTOR_VERSION", "1")   # the old reader
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 1
    assert q(runtime_conn, "SELECT ai_extractor_version FROM pages WHERE id = %s", (page_id,)) == (("1",),)

    monkeypatch.setattr(crawler.extract, "EXTRACTOR_VERSION", real)
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 2                                   # read again under the current version
    assert q(runtime_conn, "SELECT ai_extractor_version FROM pages WHERE id = %s", (page_id,)) == ((real,),)

    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 2                                   # and only once


# ---------------------------------------------------------------- HTML is never sent

def test_html_is_never_sent_to_the_ai_but_the_pdf_next_to_it_is(runtime_conn):
    from p3support import spec_html
    name, brand_id = new_brand(runtime_conn)
    html_id = make_page(runtime_conn, brand_id, spec_html("HT99ZZ11HTMLONLY"))
    pdf_id = pdf_doc(runtime_conn, brand_id, [[f"Model {MODEL}"]])
    fake = FakeAI(lambda r: reply("spec_sheet", MODEL, "HT99ZZ11HTMLONLY"))
    settle(runtime_conn, fake, name)

    assert len(fake.requests) == 1                                   # the PDF went; the path ran
    assert MODEL in fake.requests[0].text and "HT99ZZ11HTMLONLY" not in fake.requests[0].text
    assert q(runtime_conn, "SELECT COUNT(*) FROM ai_calls WHERE page_id = %s", (html_id,))[0][0] == 0
    assert linked_keys(runtime_conn, name) == {MODEL}
    assert status(runtime_conn, html_id) != "extracted"
    assert q(runtime_conn, "SELECT COUNT(*) FROM documents s JOIN document_urls du ON du.document_id = s.id WHERE du.page_id = %s",
             (html_id,))[0][0] == 0


def test_an_html_page_alone_makes_no_call_at_all(runtime_conn):
    from p3support import spec_html
    name, brand_id = new_brand(runtime_conn)
    make_page(runtime_conn, brand_id, spec_html(MODEL))
    fake = FakeAI(lambda r: reply("spec_sheet", MODEL))
    settle(runtime_conn, fake, name)
    assert fake.calls == []


def test_a_waiting_html_row_is_not_picked_up_either(runtime_conn):
    """Even if an HTML page is forced to 'waiting' with its text stored, extraction leaves it alone."""
    from p3support import spec_html
    name, brand_id = new_brand(runtime_conn)
    page_id = make_page(runtime_conn, brand_id, spec_html(MODEL))
    with runtime_conn.cursor() as cur:
        cur.execute("UPDATE pages SET extraction_status = 'waiting' WHERE id = %s", (page_id,))
    pdf_doc(runtime_conn, brand_id, [["Warranty terms and conditions apply"]])  # control: extraction does run
    fake = FakeAI(lambda r: reply("other"))
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 1
    assert MODEL not in fake.requests[0].text
    assert q(runtime_conn, "SELECT COUNT(*) FROM ai_calls WHERE page_id = %s", (page_id,))[0][0] == 0


# ---------------------------------------------------------------- no text, and which pages go

def test_a_pdf_with_no_text_is_skipped_and_never_sent(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    page_id = make_doc(runtime_conn, brand_id, [""])
    assert status(runtime_conn, page_id) == "waiting"                # so extraction is what changes it
    fake = FakeAI(lambda r: reply("other"))
    settle(runtime_conn, fake, name)
    assert status(runtime_conn, page_id) == "skipped"
    assert fake.calls == []


def test_a_real_pdf_with_no_text_is_skipped_when_it_is_indexed(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    page_id = pdf_doc(runtime_conn, brand_id, [[]])
    assert q(runtime_conn, "SELECT COUNT(*) FROM page_text WHERE page_id = %s AND body <> ''", (page_id,))[0][0] == 0
    assert status(runtime_conn, page_id) == "skipped"
    fake = FakeAI(lambda r: reply("other"))
    settle(runtime_conn, fake, name)
    assert fake.calls == []


def test_page_1_is_sent_even_when_nothing_in_the_document_looks_like_a_model(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    page_id = pdf_doc(runtime_conn, brand_id, [["Installation instructions for the outdoor unit"], ["Read all safety notes"]])
    assert status(runtime_conn, page_id) == "waiting"                # indexing no longer needs a label and a model
    fake = FakeAI(lambda r: reply("install_manual"))
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 1
    assert "Installation instructions" in fake.requests[0].text
    assert "Read all safety notes" not in fake.requests[0].text      # page 2 has no letter+digit token
    assert doc_type_of(runtime_conn, page_id) == "install_manual"


def test_only_page_1_and_pages_with_a_letter_and_digit_token_are_sent(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    texts = [
        "FRONTPAGE welcome to the manual",
        "SAFETYPAGE read all instructions 12 34 56",       # words and bare numbers only
        f"MODELPAGE Model {MODEL}",
        "PLAINPAGE nothing to see here",
        "TOKENPAGE series code AB12CD",
    ]
    make_doc(runtime_conn, brand_id, texts)
    fake = FakeAI(lambda r: reply("spec_sheet", MODEL))
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 1
    sent = fake.requests[0].text
    assert "FRONTPAGE" in sent and "MODELPAGE" in sent and "TOKENPAGE" in sent
    assert "SAFETYPAGE" not in sent and "PLAINPAGE" not in sent


def test_at_most_20_pages_are_sent(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    texts = [f"SENTMARK{n:02d} Model RA15AZ{n:02d}AJ3CA" for n in range(1, 31)]
    make_doc(runtime_conn, brand_id, texts)
    fake = FakeAI(lambda r: reply("spec_sheet"))
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 1
    sent = fake.requests[0].text
    assert all(f"SENTMARK{n:02d}" in sent for n in range(1, 21))
    assert not any(f"SENTMARK{n:02d}" in sent for n in range(21, 31))


# ---------------------------------------------------------------- a split document

def test_a_split_document_unions_its_models_and_takes_the_type_from_the_part_with_page_1(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    filler = "filler words go here " * 1900      # about 40k characters, about 10k tokens by the fake's len//4
    models = [f"RA15AZ{n:02d}BX" for n in range(20)]
    texts = [f"PAGEMARK{n:02d} Model {m}\n{filler}" for n, m in enumerate(models)]
    texts[0] = f"FIRSTPAGEMARK Model {models[0]}\n{filler}"
    page_id = make_doc(runtime_conn, brand_id, texts)

    def respond(request):
        flat = request.text.replace(" ", "")
        found = [m for m in models if m in flat]
        return reply("install_manual" if "FIRSTPAGEMARK" in request.text else "warranty", *found)

    fake = FakeAI(respond)
    settle(runtime_conn, fake, name)

    assert len(fake.requests) >= 3                                   # three parts, so "most common" or "last" would be warranty
    for r in fake.requests:
        assert (len(r.system) + len(r.text)) // 4 <= 100_000
    assert linked_keys(runtime_conn, name) == set(models)            # the union of every part
    assert doc_type_of(runtime_conn, page_id) == "install_manual"
    assert status(runtime_conn, page_id) == "extracted"
    assert q(runtime_conn, "SELECT COUNT(*) FROM ai_calls WHERE page_id = %s", (page_id,))[0][0] == len(fake.requests)

    submitted = len(fake.requests)
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == submitted                           # a split document is not read again


# ---------------------------------------------------------------- a PDF the v1 keyword filter skipped

def _skipped_by_v1(conn, brand_id, texts, version):
    """A PDF row as v1 left it: page_text stored, status 'skipped', extractor version NULL or '1'."""
    page_id = make_doc(conn, brand_id, texts)
    with conn.cursor() as cur:
        cur.execute("UPDATE pages SET extraction_status = 'skipped', ai_extractor_version = %s WHERE id = %s",
                    (version, page_id))
    return page_id


@pytest.mark.parametrize("version", [None, "1"])
def test_a_pdf_the_v1_filter_skipped_is_read_once_under_v2_and_linked(runtime_conn, version):
    from crawler.extract import EXTRACTOR_VERSION
    name, brand_id = new_brand(runtime_conn)
    page_id = _skipped_by_v1(runtime_conn, brand_id, [f"Model {MODEL}", "Installation notes"], version)
    assert status(runtime_conn, page_id) == "skipped"
    fake = FakeAI(lambda r: reply("install_manual", MODEL))
    settle(runtime_conn, fake, name)

    assert len(fake.requests) == 1
    assert status(runtime_conn, page_id) == "extracted"
    assert doc_type_of(runtime_conn, page_id) == "install_manual"
    assert linked_keys(runtime_conn, name) == {MODEL}
    assert q(runtime_conn, "SELECT ai_extractor_version FROM pages WHERE id = %s", (page_id,)) == ((EXTRACTOR_VERSION,),)


@pytest.mark.parametrize("version", [None, "1"])
def test_a_second_pass_over_a_formerly_skipped_pdf_makes_zero_calls(runtime_conn, version):
    name, brand_id = new_brand(runtime_conn)
    page_id = _skipped_by_v1(runtime_conn, brand_id, [f"Model {MODEL}"], version)
    fake = FakeAI(lambda r: reply("spec_sheet", MODEL))
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 1                                   # it really was read the first time
    calls, billed = list(fake.calls), count(runtime_conn, "ai_calls")
    settle(runtime_conn, fake, name)
    assert fake.calls == calls
    assert count(runtime_conn, "ai_calls") == billed
    assert status(runtime_conn, page_id) == "extracted"


@pytest.mark.parametrize("version", [None, "1"])
def test_a_skipped_pdf_with_no_text_makes_no_call_and_stays_out(runtime_conn, version):
    name, brand_id = new_brand(runtime_conn)
    page_id = _skipped_by_v1(runtime_conn, brand_id, [""], version)
    fake = FakeAI(lambda r: reply("other"))
    settle(runtime_conn, fake, name)
    assert fake.calls == []
    assert status(runtime_conn, page_id) == "skipped"
    settle(runtime_conn, fake, name)                                 # and again on a later pass
    assert fake.calls == []
    assert status(runtime_conn, page_id) == "skipped"


# ---------------------------------------------------------------- the type column

def test_documents_have_a_nullable_text_doc_type_column(migrate_conn):
    with migrate_conn.cursor() as cur:
        cur.execute("SELECT data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'kb' AND table_name = 'documents' AND column_name = 'doc_type'")
        rows = cur.fetchall()
    assert rows == (("text", "YES"),)
