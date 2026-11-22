"""Manuals with no model numbers get the brand's known model numbers printed in their text (free, no AI).

Prod 2026-11-21: 840 of 2,638 install and service manuals over 20 pages had no model linked. In a sample of 40 with a
text layer, 25 printed a known model number of their own brand in the first 20 pages (the text the AI was sent), 5 only
after page 20, and 10 none. A manual with no model cannot be tied to a unit, and the missing-manual search pays to
look for it again.
"""
from datetime import datetime, timedelta

from fakeai import FakeAI
from p3support import make_page, make_pdf, new_brand, q, settle
from test_p11b_tagging import tags

T0 = datetime(2031, 3, 1, 3, 0)


def manual(conn, lines, *, doc_type="install_manual", models=()):
    name, brand_id = new_brand(conn)
    make_page(conn, brand_id, make_pdf([lines]), is_pdf=True)
    settle(conn, FakeAI(lambda r: tags(doc_type=doc_type, models=models)), name)
    doc_id = q(conn, "SELECT d.id FROM documents d JOIN document_urls du ON du.document_id = d.id JOIN pages p "
                     "ON p.id = du.page_id WHERE p.brand_id = %s", (brand_id,))[0][0]
    return name, doc_id


def known(conn, name, *numbers):
    from crawler.store import manufacturer_for, save_model
    maker = manufacturer_for(conn, name)
    for number in numbers:
        save_model(conn, maker, number)


def linked(conn, doc_id):
    return sorted(r[0] for r in q(conn, "SELECT model_key FROM document_models WHERE document_id = %s", (doc_id,)))


def run(conn, now=T0):
    from crawler.known_models import link_known_models
    return link_known_models(conn, now=now)


def test_a_manual_with_no_models_gets_the_brands_known_models_printed_in_it(runtime_conn):
    name, doc = manual(runtime_conn, ["Installation Instructions", "Units: RA15AZ24AJ3CA and RA15-AZ36AJ3CA"])
    known(runtime_conn, name, "RA15AZ24AJ3CA", "RA15AZ36AJ3CA", "RP20AZ48AJ3CA")  # the last is not in the text
    assert linked(runtime_conn, doc) == []
    assert run(runtime_conn) == 2
    assert linked(runtime_conn, doc) == ["RA15AZ24AJ3CA", "RA15AZ36AJ3CA"]


def test_a_manual_the_ai_gave_models_to_is_left_as_it_is(runtime_conn):
    name, doc = manual(runtime_conn, ["Model RA15AZ24AJ3CA", "Also fits RA15AZ36AJ3CA"], models=("RA15AZ24AJ3CA",))
    known(runtime_conn, name, "RA15AZ36AJ3CA")
    run(runtime_conn)
    assert linked(runtime_conn, doc) == ["RA15AZ24AJ3CA"]


def test_short_or_digit_only_numbers_are_not_linked(runtime_conn):
    name, doc = manual(runtime_conn, ["Call 18005551234 or see part AB12C"])
    known(runtime_conn, name, "18005551234", "AB12C")
    assert run(runtime_conn) == 0 and linked(runtime_conn, doc) == []


def test_a_spec_sheet_is_not_looked_at(runtime_conn):
    name, doc = manual(runtime_conn, ["Model RA15AZ24AJ3CA"], doc_type="spec_sheet")
    known(runtime_conn, name, "RA15AZ24AJ3CA")
    assert run(runtime_conn) == 0 and linked(runtime_conn, doc) == []


# Sister brands (2026-11-21): a Bryant manual printing a Payne model number left the Payne model looking uncovered, and
# Breeze, asked about a Payne unit, missed it in the model and brand lookups and marked it down as another brand's.
# On prod, with keys of 8+ characters, every pair was a real family (Rheem/Ruud/Friedrich, Carrier/Bryant/Payne/Midea,
# Trane/American Standard, York/Johnson Controls/Coleman/Luxaire, ...): 2,487 links over 307 manuals.

def sister_link(conn, filed_brand_models, sister_numbers):
    name, doc = manual(conn, ["Units: " + " ".join(filed_brand_models)], models=filed_brand_models)
    sister, _ = new_brand(conn)
    known(conn, sister, *sister_numbers)
    from crawler.store import manufacturer_for
    return doc, manufacturer_for(conn, sister)


def test_a_model_a_sister_brand_also_sells_is_linked_under_that_brand_too(runtime_conn):
    from crawler.known_models import link_sister_brand_models
    doc, sister = sister_link(runtime_conn, ("PA14NA024000",), ("PA14NA024000",))
    assert link_sister_brand_models(runtime_conn) >= 1
    assert q(runtime_conn, "SELECT model_key FROM document_models WHERE document_id = %s AND manufacturer_id = %s",
             (doc, sister)) == (("PA14NA024000",),)
    assert q(runtime_conn, "SELECT 1 FROM document_brands WHERE document_id = %s AND manufacturer_id = %s",
             (doc, sister)) == ((1,),)
    from crawler.search import model_documents
    assert doc in model_documents(runtime_conn, "PA14NA024000", sister)  # Breeze's model lookup finds it now


def test_a_short_shared_number_is_not_taken_as_the_same_unit(runtime_conn):
    from crawler.known_models import link_sister_brand_models
    doc, sister = sister_link(runtime_conn, ("AC1000",), ("AC1000",))
    link_sister_brand_models(runtime_conn)
    assert q(runtime_conn, "SELECT 1 FROM document_models WHERE document_id = %s AND manufacturer_id = %s",
             (doc, sister)) == ()


def test_linking_sister_brands_twice_adds_nothing_the_second_time(runtime_conn):
    from crawler.known_models import link_sister_brand_models
    doc, sister = sister_link(runtime_conn, ("PA14NA036000",), ("PA14NA036000",))
    link_sister_brand_models(runtime_conn)
    before = q(runtime_conn, "SELECT COUNT(*) FROM document_models WHERE document_id = %s", (doc,))[0][0]
    link_sister_brand_models(runtime_conn)
    assert q(runtime_conn, "SELECT COUNT(*) FROM document_models WHERE document_id = %s", (doc,))[0][0] == before


def test_a_manual_is_looked_at_again_only_after_30_days(runtime_conn):
    name, doc = manual(runtime_conn, ["Units: RA15AZ24AJ3CA"])
    run(runtime_conn)  # nothing known yet
    known(runtime_conn, name, "RA15AZ24AJ3CA")
    assert run(runtime_conn, now=T0 + timedelta(days=1)) == 0  # checked yesterday: not read again
    assert run(runtime_conn, now=T0 + timedelta(days=31)) == 1
    assert linked(runtime_conn, doc) == ["RA15AZ24AJ3CA"]
