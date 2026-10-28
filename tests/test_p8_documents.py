"""P8: gap-fill and `status` count ENERGY STAR models with no linked DOCUMENT (replaces "has specs").

A model is covered when its manufacturer has a document (a documents row with doc_type set) linked to a model
whose key is equal to it or is a prefix of it at least 5 characters long. A 4-character prefix, a document with no
doc_type, a key that is not a prefix, and another manufacturer's document do not cover it. Spec rows no longer count.
The search, the 200 a month limit and the 180-day back-off are unchanged (test_p5_gapfill.py).
"""
import pytest

from fakeai import FakeAI, FakeGapAI
from p3support import answer, linked_doc, make_page, make_pdf, new_brand, q, responder_for, settle, status_line, uncovered_count
from test_p5_gapfill import T0, fam, run, tracked


def searched_families(fake):
    return [s[3] for s in fake.searches()]


# ---------------------------------------------------------------- what covers a model

def test_a_document_on_a_prefix_of_5_or_more_characters_covers_the_model(runtime_conn):
    family = fam("K")
    name, brand_id = tracked(runtime_conn, [family + "AJ1NA"])
    assert uncovered_count(runtime_conn, name) == 1  # before: counted
    linked_doc(runtime_conn, name, family + "AJ")     # an 8-character base model
    assert uncovered_count(runtime_conn, name) == 0
    fake = FakeGapAI()
    run(runtime_conn, fake, name, now=T0)
    assert fake.searches() == []


def test_a_5_character_prefix_is_the_shortest_that_covers(runtime_conn):
    family = fam("K")
    name, brand_id = tracked(runtime_conn, [family + "AJ1NA"])
    assert uncovered_count(runtime_conn, name) == 1
    linked_doc(runtime_conn, name, family[:5])
    assert uncovered_count(runtime_conn, name) == 0
    fake = FakeGapAI()
    run(runtime_conn, fake, name, now=T0)
    assert fake.searches() == []


def test_a_document_on_the_models_own_key_covers_it(runtime_conn):
    family = fam("K")
    name, brand_id = tracked(runtime_conn, [family + "AJ1NA"])
    assert uncovered_count(runtime_conn, name) == 1
    linked_doc(runtime_conn, name, family + "AJ1NA")
    assert uncovered_count(runtime_conn, name) == 0
    fake = FakeGapAI()
    run(runtime_conn, fake, name, now=T0)
    assert fake.searches() == []


def test_a_4_character_prefix_does_not_cover_the_model_so_it_is_counted_and_searched(runtime_conn):
    family = fam("K")
    name, brand_id = tracked(runtime_conn, [family + "AJ1NA"])
    linked_doc(runtime_conn, name, family[:4])
    assert uncovered_count(runtime_conn, name) == 1
    fake = FakeGapAI()
    run(runtime_conn, fake, name, now=T0)
    assert len(fake.searches()) == 1 and family in searched_families(fake)[0]


def test_a_source_with_no_doc_type_does_not_cover_the_model(runtime_conn):
    family = fam("K")
    name, brand_id = tracked(runtime_conn, [family + "AJ1NA"])
    linked_doc(runtime_conn, name, family + "AJ", doc_type=None)
    assert uncovered_count(runtime_conn, name) == 1
    fake = FakeGapAI()
    run(runtime_conn, fake, name, now=T0)
    assert len(fake.searches()) == 1


def test_a_key_that_is_not_a_prefix_does_not_cover_the_model(runtime_conn):
    family = fam("K")
    other = family[:5] + ("Y" if family[4] == "Z" else "Z") + "AJ"
    name, brand_id = tracked(runtime_conn, [family + "AJ1NA"])
    linked_doc(runtime_conn, name, other)
    assert uncovered_count(runtime_conn, name) == 1
    fake = FakeGapAI()
    run(runtime_conn, fake, name, now=T0)
    assert len(fake.searches()) == 1


def test_another_manufacturers_document_does_not_cover_the_model(runtime_conn):
    family = fam("K")
    name, brand_id = tracked(runtime_conn, [family + "AJ1NA"])
    other_name, other_id = new_brand(runtime_conn)
    linked_doc(runtime_conn, other_name, family + "AJ")  # same letters, different manufacturer
    assert uncovered_count(runtime_conn, name) == 1
    fake = FakeGapAI()
    run(runtime_conn, fake, name, now=T0)
    assert len(fake.searches()) == 1


def test_a_not_equipment_document_does_not_cover_the_model(runtime_conn):
    """An annual report that happens to print a model number is not a document for that model (P11b's not_equipment)."""
    family = fam("K")
    name, brand_id = tracked(runtime_conn, [family + "AJ1NA"])
    linked_doc(runtime_conn, name, family + "AJ", doc_type="not_equipment")
    assert uncovered_count(runtime_conn, name) == 1
    fake = FakeGapAI()
    run(runtime_conn, fake, name, now=T0)
    assert len(fake.searches()) == 1


def test_one_covered_family_is_skipped_and_an_uncovered_one_is_still_searched(runtime_conn):
    done, todo = fam("D"), fam("T")
    name, brand_id = tracked(runtime_conn, [done + "24XYZ", todo + "24XYZ"])
    linked_doc(runtime_conn, name, done + "24")
    fake = FakeGapAI()
    run(runtime_conn, fake, name, now=T0)
    assert len(fake.searches()) == 1
    assert todo in searched_families(fake)[0] and done not in searched_families(fake)[0]


# ---------------------------------------------------------------- the status line

def test_status_says_how_many_documents_and_how_many_energy_star_models_have_none(runtime_conn):
    fams = [fam("S") for _ in range(4)]
    name, brand_id = tracked(runtime_conn, [f + "AJ1NA" for f in fams])
    # two real PDFs, read by the fake AI; each names a 5+ character prefix of one ENERGY STAR model
    keys = [fams[0] + "AJ", fams[1] + "AJ"]
    for key in keys:
        make_page(runtime_conn, brand_id, make_pdf([[f"Model {key}", "Installation guide"]]), is_pdf=True)
    fake = FakeAI(responder_for({key: answer(key) for key in keys}))
    settle(runtime_conn, fake, name)
    assert len(fake.requests) == 2  # the documents really were read and linked

    line = status_line(runtime_conn, name)
    assert "2 documents, 2 ENERGY STAR models with no documents" in line
    assert "with no specs" not in line
    assert uncovered_count(runtime_conn, name) == 2
