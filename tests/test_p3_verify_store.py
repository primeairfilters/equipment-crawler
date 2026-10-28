"""P3 (ported to the P10 schema): the rules that check what the AI said, and the writes into kb.

Nothing here talks to an AI. save_document gets facts that already passed the rules.
"""
import pytest

from p3support import new_brand, q

MODEL = "RA15AZ24AJ3CA"
PAGE = f"Model {MODEL}\nSEER2 15.2\nTonnage 2.0\nRefrigerant R-410A\nMCA 14 A\nWeight 250 lbs\n"


# ---------------------------------------------------------------- verify

def _link_run(conn, page_text, models):
    """A fetched PDF with this text, read by a fake AI that names `models`. Returns the model keys that got linked."""
    from fakeai import FakeAI
    from p3support import make_doc, settle
    name, brand_id = new_brand(conn)
    make_doc(conn, brand_id, [page_text])
    fake = FakeAI(lambda r: {"doc_type": "spec_sheet", "models": list(models)})
    settle(conn, fake, name)
    assert len(fake.requests) == 1  # the AI really was asked, so an empty result below means the check refused
    return {r[0] for r in q(conn, "SELECT m.model_key FROM document_models dm JOIN models m "
                                  "ON m.id = dm.model_id JOIN manufacturers f ON f.id = m.manufacturer_id "
                                  "WHERE f.name = %s", (name,))}


def test_a_model_number_not_on_the_page_is_not_linked(runtime_conn):
    assert _link_run(runtime_conn, PAGE, ["ZZ999FAKE123"]) == set()
    # control (other text, so it is not skipped as already read): the one on the page links, the made-up one does not
    assert _link_run(runtime_conn, PAGE + "Control\n", ["ZZ999FAKE123", MODEL]) == {MODEL}


def test_a_model_number_split_by_spaces_in_the_page_text_still_matches(runtime_conn):
    assert _link_run(runtime_conn, "Model RA1 5AZ24AJ 3CA\nSEER2 15.2", [MODEL]) == {MODEL}


# ---------------------------------------------------------------- keys

@pytest.mark.parametrize("raw", ["ra15az24aj3ca", "RA15AZ-24AJ3CA", "RA1 5AZ24AJ3CA", " RA15AZ24AJ3CA. "])
def test_model_key_is_uppercase_letters_and_digits_only(raw):
    from crawler.store import model_key
    assert model_key(raw) == "RA15AZ24AJ3CA"


@pytest.mark.parametrize("raw,expected", [
    ("Rheem Sales Company, Fort Smith, Arkansas", "RHEEM"),
    ("RHEEM SALES COMPANY, FORT SMITH, ARKANSAS", "RHEEM"),
    ("Rheem Sales Company", "RHEEM"),
    ("Ruud Manufacturing Co., Inc.", "RUUD"),
    ("Carrier Corporation", "CARRIER"),
    ("Goodman Mfg. Co., L.P.", "GOODMAN"),
    ("Lennox Industries Inc.", "LENNOX INDUSTRIES"),
])
def test_alias_key_matches_the_messy_names_on_unit_labels(raw, expected):
    from crawler.store import alias_key
    assert alias_key(raw) == expected


# ---------------------------------------------------------------- save_document

def doc(model=MODEL, base=False):
    return [{"model_number": model, "is_base_model": base}]


def page(conn, brand_id):
    from p3support import make_doc
    return make_doc(conn, brand_id, [PAGE])


def save(conn, brand_id, models, *, page_id=None, url="https://ruud.test/spec", title="Spec sheet", sha="a" * 64,
         by="ai:claude-haiku-5-5", doc_type="spec_sheet"):
    from crawler.store import save_document
    return save_document(conn, brand_id=brand_id, page_id=page_id or page(conn, brand_id), source_url=url, title=title,
                         content_sha=sha, models=models, extracted_by=by, doc_type=doc_type)


def test_save_creates_manufacturer_alias_model_document_url_and_link(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    page_id = page(runtime_conn, brand_id)
    document_id = save(runtime_conn, brand_id, doc(), page_id=page_id, sha="a1" * 32)
    m = q(runtime_conn, "SELECT f.name, f.name_key, m.model_number, m.model_key, m.is_base_model FROM models m "
                        "JOIN manufacturers f ON f.id = m.manufacturer_id WHERE f.name = %s", (name,))
    assert len(m) == 1
    assert m[0][1] == name.upper() and m[0][2] == MODEL and m[0][3] == MODEL and m[0][4] is False
    assert q(runtime_conn, "SELECT COUNT(*) FROM manufacturer_aliases a JOIN manufacturers f ON f.id = a.manufacturer_id "
                           "WHERE f.name = %s AND a.alias_key = %s", (name, name.upper()))[0][0] == 1
    assert q(runtime_conn, "SELECT d.canonical_url, d.title, d.content_sha, d.doc_type, f.name FROM documents d "
                           "JOIN manufacturers f ON f.id = d.manufacturer_id WHERE d.id = %s", (document_id,)) == (
        ("https://ruud.test/spec", "Spec sheet", "a1" * 32, "spec_sheet", name),)
    assert q(runtime_conn, "SELECT page_id, url FROM document_urls WHERE document_id = %s", (document_id,)) == (
        (page_id, "https://ruud.test/spec"),)
    assert q(runtime_conn, "SELECT model_key FROM document_models WHERE document_id = %s", (document_id,)) == ((MODEL,),)


def test_model_number_is_never_rewritten_and_a_differently_written_one_is_the_same_model(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    save(runtime_conn, brand_id, doc(model="ra15az24aj3ca"), sha="a2" * 32)
    save(runtime_conn, brand_id, doc(model="RA15AZ-24AJ3CA"), sha="a3" * 32)
    rows = q(runtime_conn, "SELECT m.model_number, m.model_key FROM models m JOIN manufacturers f "
                           "ON f.id = m.manufacturer_id WHERE f.name = %s", (name,))
    assert rows == (("ra15az24aj3ca", MODEL),)


def test_base_model_flag_is_stored(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    save(runtime_conn, brand_id, doc(model="RA15AZ24AJ3", base=True), sha="a4" * 32)
    assert q(runtime_conn, "SELECT m.is_base_model FROM models m JOIN manufacturers f ON f.id = m.manufacturer_id "
                           "WHERE f.name = %s", (name,)) == ((True,),)


def test_the_same_content_at_two_urls_is_one_document_with_two_urls(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    first = save(runtime_conn, brand_id, doc(), url="https://a.ruud.test/spec.pdf", sha="a5" * 32)
    second = save(runtime_conn, brand_id, doc(), url="https://b.ruud.test/spec.pdf", sha="a5" * 32)
    assert first == second
    assert {r[0] for r in q(runtime_conn, "SELECT url FROM document_urls WHERE document_id = %s", (first,))} == {
        "https://a.ruud.test/spec.pdf", "https://b.ruud.test/spec.pdf"}
    assert q(runtime_conn, "SELECT COUNT(*) FROM document_models WHERE document_id = %s", (first,))[0][0] == 1


def test_a_resave_updates_title_and_keeps_a_type_it_is_not_given(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    page_id = page(runtime_conn, brand_id)
    document_id = save(runtime_conn, brand_id, doc(), page_id=page_id, title="Old title", sha="a6" * 32)
    save(runtime_conn, brand_id, doc(), page_id=page_id, title="New title", sha="a6" * 32, doc_type=None)
    assert q(runtime_conn, "SELECT title, doc_type FROM documents WHERE id = %s", (document_id,)) == (
        ("New title", "spec_sheet"),)


@pytest.mark.parametrize("by", ["", "manual", "ai", "ai:", "ai:gpt-4o", "ai:claude-opus-5-5", "AI:claude-haiku-5-5", "rules ",
                                "energystar"])
def test_a_bad_extracted_by_is_refused_and_nothing_is_written(runtime_conn, by):
    from crawler.store import BadExtractedBy
    name, brand_id = new_brand(runtime_conn)
    with pytest.raises(BadExtractedBy):
        save(runtime_conn, brand_id, doc(), by=by, sha="a7" * 32)
    assert q(runtime_conn, "SELECT COUNT(*) FROM manufacturers WHERE name = %s", (name,))[0][0] == 0
    assert q(runtime_conn, "SELECT COUNT(*) FROM documents WHERE content_sha = %s", ("a7" * 32,))[0][0] == 0


def test_a_document_is_written_all_or_nothing(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    page_id = page(runtime_conn, brand_id)
    broken = {"is_base_model": False}  # no model_number, so the write fails after the first model is saved
    with pytest.raises(Exception):
        save(runtime_conn, brand_id, doc() + [broken], page_id=page_id, sha="a8" * 32)
    assert q(runtime_conn, "SELECT COUNT(*) FROM manufacturers WHERE name = %s", (name,))[0][0] == 0
    assert q(runtime_conn, "SELECT COUNT(*) FROM documents WHERE content_sha = %s", ("a8" * 32,))[0][0] == 0
    assert q(runtime_conn, "SELECT COUNT(*) FROM models WHERE model_key = %s AND manufacturer_id IN "
                           "(SELECT id FROM manufacturers WHERE name = %s)", (MODEL, name))[0][0] == 0
    document_id = save(runtime_conn, brand_id, doc(), page_id=page_id, sha="a8" * 32)  # control: the connection still works
    assert q(runtime_conn, "SELECT COUNT(*) FROM document_models WHERE document_id = %s", (document_id,))[0][0] == 1
