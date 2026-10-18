"""P3: the rules that check what the AI said, and the writes into the equipment tables.

Nothing here talks to an AI. save_document gets facts that already passed the rules.
"""
from decimal import Decimal

import pytest

from p3support import new_brand, q

MODEL = "RA15AZ24AJ3CA"
PAGE = f"Model {MODEL}\nSEER2 15.2\nTonnage 2.0\nRefrigerant R-410A\nMCA 14 A\nWeight 250 lbs\n"


def check(model, key, value, unit=None, texts=(PAGE,)):
    from crawler.verify import verify
    out = {"models": [{"model_number": model, "specs": [{"key": key, "value": value, "unit": unit}]}]}
    result = verify(out, list(texts))
    assert len(result) == 1
    return result[0]


# ---------------------------------------------------------------- verify

@pytest.mark.parametrize("key,value,unit", [
    ("seer2", "15.2", None),
    ("tonnage", "2.0", "tons"),
    ("refrigerant", "R-410A", None),
    ("mca", "14", "A"),
])
def test_a_fact_that_is_on_the_page_passes(key, value, unit):
    c = check(MODEL, key, value, unit)
    assert c.ok is True and c.reason is None
    assert (c.model_number, c.key, c.value, c.unit) == (MODEL, key, value, unit)


def test_a_model_number_not_on_the_page_fails_every_one_of_its_facts():
    from crawler.verify import verify
    out = {"models": [{"model_number": "ZZ999FAKE123", "specs": [
        {"key": "seer2", "value": "15.2", "unit": None}, {"key": "tonnage", "value": "2.0", "unit": "tons"}]}]}
    result = verify(out, [PAGE])
    assert [(c.ok, c.reason) for c in result] == [(False, "model_not_on_page")] * 2


def test_a_model_number_split_by_spaces_in_the_page_text_still_matches():
    c = check(MODEL, "seer2", "15.2", texts=["Model RA1 5AZ24AJ 3CA\nSEER2 15.2"])
    assert c.ok is True


def test_a_number_that_is_not_on_the_page_fails():
    c = check(MODEL, "seer2", "16.5")
    assert (c.ok, c.reason) == (False, "number_not_on_page")


def test_a_number_on_another_page_of_the_same_document_counts():
    c = check(MODEL, "tonnage", "2.0", "tons", texts=[f"Model {MODEL}", "Tonnage 2.0"])
    assert c.ok is True


def test_a_key_outside_the_fixed_list_fails():
    c = check(MODEL, "favorite_color", "blue", texts=[PAGE + "blue"])
    assert (c.ok, c.reason) == (False, "unknown_key")


def test_a_wrong_unit_fails():
    c = check(MODEL, "tonnage", "2.0", "lbs")
    assert (c.ok, c.reason) == (False, "bad_unit")


@pytest.mark.parametrize("key,value,unit", [("tonnage", "250", "tons"), ("tonnage", "0", "tons"), ("seer2", "999", None)])
def test_a_value_outside_a_sane_range_fails_even_when_it_is_on_the_page(key, value, unit):
    c = check(MODEL, key, value, unit, texts=[PAGE + "999 0"])
    assert (c.ok, c.reason) == (False, "out_of_range")


def test_results_keep_the_order_of_the_answer():
    from crawler.verify import verify
    out = {"models": [
        {"model_number": MODEL, "specs": [{"key": "seer2", "value": "15.2", "unit": None},
                                          {"key": "seer2", "value": "99.9", "unit": None}]},
        {"model_number": "ZZ999FAKE123", "specs": [{"key": "tonnage", "value": "2.0", "unit": "tons"}]}]}
    got = [(c.model_number, c.key, c.ok) for c in verify(out, [PAGE])]
    assert got == [(MODEL, "seer2", True), (MODEL, "seer2", False), ("ZZ999FAKE123", "tonnage", False)]


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

def doc(model=MODEL, seer2="15.2", base=False, extra=()):
    specs = [{"key": "seer2", "value": seer2, "unit": None}, {"key": "tonnage", "value": "2.0", "unit": "tons"}, *extra]
    return [{"model_number": model, "is_base_model": base, "specs": specs}]


def save(conn, brand_id, models, *, url="https://ruud.test/spec", title="Spec sheet", sha="a" * 64,
         by="ai:claude-haiku-5-5"):
    from crawler.store import save_document
    return save_document(conn, brand_id=brand_id, source_url=url, title=title, content_sha=sha,
                         models=models, extracted_by=by)


def specs(conn, brand_name, key="seer2"):
    return q(conn, "SELECT s.value_text, s.superseded FROM equipment_specs s JOIN equipment_models m ON m.id = s.model_id "
                   "JOIN manufacturers f ON f.id = m.manufacturer_id WHERE f.name = %s AND s.spec_key = %s "
                   "ORDER BY s.id", (brand_name, key))


def test_save_creates_manufacturer_alias_model_source_and_specs(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    save(runtime_conn, brand_id, doc())
    m = q(runtime_conn, "SELECT f.name, f.name_key, m.model_number, m.model_key, m.is_base_model FROM equipment_models m "
                        "JOIN manufacturers f ON f.id = m.manufacturer_id WHERE f.name = %s", (name,))
    assert len(m) == 1
    assert m[0][1] == name.upper() and m[0][2] == MODEL and m[0][3] == MODEL and m[0][4] == 0
    assert q(runtime_conn, "SELECT COUNT(*) FROM manufacturer_aliases a JOIN manufacturers f ON f.id = a.manufacturer_id "
                           "WHERE f.name = %s AND a.alias_key = %s", (name, name.upper()))[0][0] == 1
    assert q(runtime_conn, "SELECT s.url, s.title, s.content_sha FROM equipment_sources s WHERE s.url = %s",
             ("https://ruud.test/spec",)) == (("https://ruud.test/spec", "Spec sheet", "a" * 64),)
    assert q(runtime_conn, "SELECT COUNT(*) FROM equipment_model_sources ms JOIN equipment_models m ON m.id = ms.model_id "
                           "JOIN manufacturers f ON f.id = m.manufacturer_id WHERE f.name = %s", (name,))[0][0] == 1
    assert specs(runtime_conn, name) == (("15.2", 0),)
    assert specs(runtime_conn, name, "tonnage") == (("2.0", 0),)
    row = q(runtime_conn, "SELECT s.value_num, s.unit, s.extracted_by FROM equipment_specs s WHERE s.spec_key = 'seer2' "
                          "AND s.model_id IN (SELECT m.id FROM equipment_models m JOIN manufacturers f ON f.id = m.manufacturer_id "
                          "WHERE f.name = %s)", (name,))
    assert row == ((Decimal("15.2"), None, "ai:claude-haiku-5-5"),)


def test_saving_the_same_document_again_changes_nothing(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    save(runtime_conn, brand_id, doc())
    first = q(runtime_conn, "SELECT COUNT(*) FROM equipment_specs")[0][0]
    save(runtime_conn, brand_id, doc())
    assert q(runtime_conn, "SELECT COUNT(*) FROM equipment_specs")[0][0] == first
    assert specs(runtime_conn, name) == (("15.2", 0),)


def test_model_number_is_never_rewritten_and_a_differently_written_one_is_the_same_model(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    save(runtime_conn, brand_id, doc(model="ra15az24aj3ca"))
    save(runtime_conn, brand_id, doc(model="RA15AZ-24AJ3CA"))
    rows = q(runtime_conn, "SELECT m.model_number, m.model_key FROM equipment_models m JOIN manufacturers f "
                           "ON f.id = m.manufacturer_id WHERE f.name = %s", (name,))
    assert rows == (("ra15az24aj3ca", MODEL),)


def test_base_model_flag_is_stored(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    save(runtime_conn, brand_id, doc(model="RA15AZ24AJ3", base=True))
    assert q(runtime_conn, "SELECT m.is_base_model FROM equipment_models m JOIN manufacturers f ON f.id = m.manufacturer_id "
                           "WHERE f.name = %s", (name,)) == ((1,),)


def test_source_title_and_hash_are_updated_in_place(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    save(runtime_conn, brand_id, doc(), title="Old title", sha="a" * 64)
    save(runtime_conn, brand_id, doc(), title="New title", sha="b" * 64)
    assert q(runtime_conn, "SELECT title, content_sha FROM equipment_sources WHERE url = %s",
             ("https://ruud.test/spec",)) == (("New title", "b" * 64),)


def test_a_changed_value_is_added_and_the_old_one_is_superseded(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    save(runtime_conn, brand_id, doc(seer2="15.2"))
    save(runtime_conn, brand_id, doc(seer2="16.0"))
    assert specs(runtime_conn, name) == (("15.2", 1), ("16.0", 0))


def test_going_back_to_an_old_value_brings_that_row_back_instead_of_adding_another(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    save(runtime_conn, brand_id, doc(seer2="15.2"))   # A
    save(runtime_conn, brand_id, doc(seer2="16.0"))   # B
    assert specs(runtime_conn, name) == (("15.2", 1), ("16.0", 0))
    save(runtime_conn, brand_id, doc(seer2="15.2"))   # A again
    assert specs(runtime_conn, name) == (("15.2", 0), ("16.0", 1))


def test_two_sources_with_different_values_are_both_current(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    save(runtime_conn, brand_id, doc(seer2="15.2"), url="https://ruud.test/one")
    save(runtime_conn, brand_id, doc(seer2="16.0"), url="https://ruud.test/two")
    assert specs(runtime_conn, name) == (("15.2", 0), ("16.0", 0))


@pytest.mark.parametrize("by", ["", "manual", "ai", "ai:", "ai:gpt-4o", "ai:claude-opus-5-5", "AI:claude-haiku-5-5", "rules ",
                                "energystar"])
def test_a_bad_extracted_by_is_refused_and_nothing_is_written(runtime_conn, by):
    from crawler.store import BadExtractedBy
    name, brand_id = new_brand(runtime_conn)
    with pytest.raises(BadExtractedBy):
        save(runtime_conn, brand_id, doc(), by=by)
    assert q(runtime_conn, "SELECT COUNT(*) FROM manufacturers WHERE name = %s", (name,))[0][0] == 0
    assert specs(runtime_conn, name) == ()


@pytest.mark.parametrize("by", ["energy_star", "rules", "ai:claude-haiku-5-5", "ai:claude-sonnet-5-5"])
def test_the_allowed_extracted_by_values_are_accepted(runtime_conn, by):
    name, brand_id = new_brand(runtime_conn)
    save(runtime_conn, brand_id, doc(), by=by)
    assert q(runtime_conn, "SELECT s.extracted_by FROM equipment_specs s WHERE s.spec_key = 'seer2' AND s.model_id IN "
                           "(SELECT m.id FROM equipment_models m JOIN manufacturers f ON f.id = m.manufacturer_id "
                           "WHERE f.name = %s)", (name,)) == ((by,),)


def test_a_document_is_written_all_or_nothing(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    too_long = {"key": "refrigerant", "value": "R" * 600, "unit": None}  # does not fit the column, so the write fails
    with pytest.raises(Exception):
        save(runtime_conn, brand_id, doc(extra=[too_long]))
    assert q(runtime_conn, "SELECT COUNT(*) FROM manufacturers WHERE name = %s", (name,))[0][0] == 0
    assert specs(runtime_conn, name) == ()
    save(runtime_conn, brand_id, doc())  # control: the same connection is still usable and the good document saves
    assert specs(runtime_conn, name) == (("15.2", 0),)
