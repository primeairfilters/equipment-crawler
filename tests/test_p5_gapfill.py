"""P5: gap-fill (decision D15): a targeted web search for ENERGY STAR models that still have no specs.

gap_fill(conn, client, *, brand=None, now=None, cap_usd=None, max_searches=MAX_SEARCHES_PER_MONTH) never commits.
Decided by D15 and tested here:
- the worklist comes only from ENERGY STAR models (energy_star_confirmed) of a TRACKED brand that have no specs;
- one search per model family (family = the first 6 characters of the model key), not per model;
- a free check first: if text we already stored mentions the family, no paid search is made for it;
- at most 2,000 searches a month, counted from ai_calls (purpose 'gapfill', column searches);
- a search that found nothing is not repeated for 6 months, one that found documents not for 30 days;
- the search is limited to the brand's approved hosts, one search per call (max_uses 1);
- the search text holds the brand name and the family only, never full model numbers.
The client is a fake with find_documents(model, brand, query, hosts, max_uses, max_tokens), see FakeGapAI.

Every family used here is made up per test (see fam), because the free check looks at ALL stored page text and
the testing database may hold text left over from other runs.
"""
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from fakeai import FakeGapAI
from p3support import make_doc, new_brand, q

T0 = datetime(2031, 5, 10, 9, 0)
HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"


def fam(prefix="Q"):
    """A 6-character family that appears nowhere else: a letter prefix plus random hex."""
    return (prefix + uuid.uuid4().hex.upper())[:6]


def model(family, tail="24XYZ"):
    return family + tail


def tracked(conn, models, host="ruud.test"):
    """A tracked brand with an approved host and ENERGY STAR models that have no specs."""
    from crawler.brands import approve_host
    name, brand_id = new_brand(conn)
    if host:
        approve_host(conn, host, brand=name)
    add_es_models(conn, name, models)
    return name, brand_id


def add_es_models(conn, manufacturer_name, models):
    from crawler.store import alias_key
    with conn.cursor() as cur:
        cur.execute("INSERT INTO manufacturers (name, name_key) VALUES (%s, %s) "
                    "ON CONFLICT (name_key) DO UPDATE SET name_key = EXCLUDED.name_key RETURNING id",
                    (manufacturer_name, alias_key(manufacturer_name)))
        mid = cur.fetchone()[0]
        for m in models:
            cur.execute("INSERT INTO models (manufacturer_id, model_number, model_key, energy_star_confirmed) "
                        "VALUES (%s, %s, %s, true) ON CONFLICT (manufacturer_id, model_key) DO UPDATE SET energy_star_confirmed = true",
                        (mid, m, m))


def run(conn, fake, name, **kw):
    from crawler.gapfill import gap_fill
    kw.setdefault("now", T0)
    return gap_fill(conn, fake, brand=name, **kw)


# ---------------------------------------------------------------- families

@pytest.mark.parametrize("key,family", [("RA15AZ24AJ3CA", "RA15AZ"), ("RD18AY36AJVCA", "RD18AY"),
                                         ("EL16XP1048230A01", "EL16XP"), ("48FCEM08A2A60A0A0", "48FCEM"), ("ABC12", "ABC12")])
def test_family_is_the_first_six_characters(key, family):
    from crawler.gapfill import family as family_of
    assert family_of(key) == family


def test_the_limit_is_2000_searches_a_month():
    # 2026-11-21: 200 searches cost $2.29 in November; at 200 the 16,335 ENERGY STAR models with no document, and the
    # models found only in spec sheets, would take years.
    from crawler.gapfill import MAX_SEARCHES_PER_MONTH
    assert MAX_SEARCHES_PER_MONTH == 2000


# ---------------------------------------------------------------- models found in documents, with no manual

def found_in_docs(conn, name, key, doc_type, trade=None, title=None):
    """A model the crawler found in one of its documents (not ENERGY STAR), and that document's page text."""
    from p3support import linked_doc
    document_id = linked_doc(conn, name, key, doc_type)
    if title:
        with conn.cursor() as cur:
            cur.execute("UPDATE documents SET title = %s WHERE id = %s", (title, document_id))
    if trade:
        with conn.cursor() as cur:
            cur.execute("UPDATE models SET trade = %s WHERE model_key = %s", (trade, key))


def test_a_model_found_only_in_a_spec_sheet_is_searched_for_its_manual(runtime_conn):
    f = fam("S")
    name, brand_id = tracked(runtime_conn, [])
    found_in_docs(runtime_conn, name, model(f), "spec_sheet")
    make_doc(runtime_conn, brand_id, [f"Spec sheet for {model(f)}"])  # the text it was found in: no free skip for it
    fake = FakeGapAI()
    run(runtime_conn, fake, name)
    assert len(fake.searches()) == 1
    query = fake.searches()[0][3]
    assert f in query and "manual" in query and "spec sheet" not in query


def test_a_model_with_an_install_or_service_manual_is_not_searched(runtime_conn):
    a, b = fam("I"), fam("V")
    name, brand_id = tracked(runtime_conn, [])
    found_in_docs(runtime_conn, name, model(a), "install_manual")
    found_in_docs(runtime_conn, name, model(b), "service_manual")
    fake = FakeGapAI()
    run(runtime_conn, fake, name)
    assert fake.searches() == []


@pytest.mark.parametrize("title", ["Owner's Manual Split Air Conditioner", "Use & Care Guide - Electronic Dryer",
                                   "Gas Range Use and Care Manual", "Owners Manual", "Floor Console Ductless System Owner’s Manual",
                                   "WINDOW TYPE ROOM AIR CONDITIONER User's Manual", "Homeowners Manual Ecomatic HS 3220"])
def test_an_owner_only_manual_does_not_count_as_the_models_manual(runtime_conn, title):
    # LIVE_CAMERA_CIRCLE D15 (the user, 2026-11-22): prod had 999 owner's manuals tagged install or service manual, so
    # 574 families counted as having a manual and the tech's install manual was never searched for.
    f = fam("W")
    name, brand_id = tracked(runtime_conn, [])
    found_in_docs(runtime_conn, name, model(f), "install_manual", title=title)
    fake = FakeGapAI()
    run(runtime_conn, fake, name)
    assert [f in s[3] for s in fake.searches()] == [True]


@pytest.mark.parametrize("title", ["Owner's Manual & Installation Instructions", "RANGE HOOD Installation, Use and Care Manual",
                                   "Premium Control Installation and User Guide",
                                   "MEW1 Series Owner's Manual Installation, Operation, & Maintenance Instructions",
                                   "Split System Installation Manual"])
def test_a_manual_that_is_also_for_the_installer_still_counts(runtime_conn, title):
    f = fam("C")
    name, brand_id = tracked(runtime_conn, [])
    found_in_docs(runtime_conn, name, model(f), "install_manual", title=title)
    fake = FakeGapAI()
    run(runtime_conn, fake, name)
    assert fake.searches() == []


def test_the_missing_manual_search_also_asks_for_the_owners_manual(runtime_conn):
    # D15: Tech Support users need the owner's ("Use & Care") manual most.
    f = fam("U")
    name, brand_id = tracked(runtime_conn, [])
    found_in_docs(runtime_conn, name, model(f), "spec_sheet")
    fake = FakeGapAI()
    run(runtime_conn, fake, name)
    query = fake.searches()[0][3].lower()
    assert "installation manual" in query and "service manual" in query
    assert "owner's manual" in query and "use and care" in query


def test_a_found_model_outside_the_brands_trades_is_not_searched(runtime_conn):
    keep, other = fam("K"), fam("O")
    name, brand_id = tracked(runtime_conn, [])
    with runtime_conn.cursor() as cur:
        cur.execute("UPDATE brands SET trades = ARRAY['hvac'] WHERE id = %s", (brand_id,))
    found_in_docs(runtime_conn, name, model(keep), "brochure", trade="hvac")
    found_in_docs(runtime_conn, name, model(other), "brochure", trade="plumbing")
    fake = FakeGapAI()
    run(runtime_conn, fake, name)
    assert [keep in s[3] for s in fake.searches()] == [True]


# ---------------------------------------------------------------- what is searched, and how

def test_one_search_per_family_limited_to_the_brands_approved_hosts(runtime_conn):
    a, b = fam("A"), fam("B")
    models = [model(a, "24AJ3CA"), model(a, "30AJ3CA"), model(a, "36AJ3CA"), model(b, "36AJVCA")]
    name, brand_id = tracked(runtime_conn, models)
    from crawler.brands import approve_host
    approve_host(runtime_conn, "files.myrheem.com", brand=name)
    fake = FakeGapAI()
    run(runtime_conn, fake, name)
    searches = fake.searches()
    assert len(searches) == 2
    queries = [s[3] for s in searches]
    assert sum(a in qu for qu in queries) == 1 and sum(b in qu for qu in queries) == 1
    for _, used_model, brand, query, hosts, max_uses, max_tokens in searches:
        assert used_model == HAIKU and brand == name and name in query
        assert hosts == ("files.myrheem.com", "ruud.test")
        assert max_uses == 1 and isinstance(max_tokens, int) and max_tokens > 0
        assert not any(full in query for full in models)


def test_sonnet_is_used_when_haiku_cannot_search_the_web(runtime_conn):
    name, brand_id = tracked(runtime_conn, [model(fam())])
    fake = FakeGapAI(web_search_models=(SONNET,))
    run(runtime_conn, fake, name)
    assert [s[1] for s in fake.searches()] == [SONNET]


def test_models_that_already_have_a_document_are_not_searched(runtime_conn):
    from p3support import linked_doc
    done, todo = fam("D"), fam("T")
    name, brand_id = tracked(runtime_conn, [model(done), model(todo)])
    linked_doc(runtime_conn, name, model(done))
    fake = FakeGapAI()
    run(runtime_conn, fake, name)
    assert len(fake.searches()) == 1
    assert todo in fake.searches()[0][3] and done not in fake.searches()[0][3]


def test_a_brand_with_no_approved_host_is_not_searched(runtime_conn):
    name, brand_id = tracked(runtime_conn, [model(fam())], host=None)
    fake = FakeGapAI()
    run(runtime_conn, fake, name)
    assert fake.searches() == []


def test_energy_star_models_of_a_brand_we_do_not_track_are_never_searched(runtime_conn):
    mine, theirs = fam("M"), fam("O")
    name, brand_id = tracked(runtime_conn, [model(mine)])
    other = "Zz" + uuid.uuid4().hex[:10]  # a manufacturer that is not one of our brands
    add_es_models(runtime_conn, other, [model(theirs)])
    fake = FakeGapAI()
    from crawler.gapfill import gap_fill
    gap_fill(runtime_conn, fake, now=T0)  # no brand given: every tracked brand
    assert all(theirs not in s[3] for s in fake.searches())
    assert any(mine in s[3] for s in fake.searches())  # control: the tracked brand's family was searched


def test_a_found_document_on_an_approved_host_becomes_a_start_url_and_ahri_never_does(runtime_conn):
    name, brand_id = tracked(runtime_conn, [model(fam())])
    docs = {"documents": [
        {"url": "https://ruud.test/docs/ra15az-spec.pdf", "evidence_url": "https://ruud.test/ra15az"},
        {"url": "https://www.ahridirectory.org/x", "evidence_url": "https://www.ahridirectory.org/x"},
    ]}
    run(runtime_conn, FakeGapAI(docs), name)
    rows = q(runtime_conn, "SELECT url, found_by FROM start_urls WHERE brand_id = %s", (brand_id,))
    assert rows == (("https://ruud.test/docs/ra15az-spec.pdf", "gapfill"),)  # labelled for what found it (P20; was "discover")


def test_the_search_is_logged_with_its_one_search_and_cost(runtime_conn):
    name, brand_id = tracked(runtime_conn, [model(fam())])
    run(runtime_conn, FakeGapAI(input_tokens=2000, output_tokens=300, searches=1), name)
    rows = q(runtime_conn, "SELECT purpose, model, input_tokens, output_tokens, searches, is_batch, rejected, cost_usd "
                           "FROM ai_calls WHERE brand_id = %s", (brand_id,))
    assert len(rows) == 1
    purpose, used_model, tin, tout, searches, is_batch, rejected, cost = rows[0]
    assert (purpose, used_model, tin, tout, searches, is_batch, rejected) == ("gapfill", HAIKU, 2000, 300, 1, 0, 0)
    assert Decimal(str(cost)) == Decimal("0.01035")  # 2000 in + 300 out at Haiku prices + one $0.01 search


# ---------------------------------------------------------------- the free check comes first

def test_the_free_check_runs_before_any_paid_search(runtime_conn):
    held, open_ = fam("AA"), fam("ZZ")  # held sorts first, so a search-first order would spend the only search on it
    name, brand_id = tracked(runtime_conn, [model(held), model(open_)])
    other_name, other_id = new_brand(runtime_conn)
    spaced = " ".join([model(held)[:3], model(held)[3:]])  # stray space inside the model, as PDF text often has
    make_doc(runtime_conn, other_id, [f"Model {spaced}\nSEER2 15.2"])  # text we already hold, even under another brand
    fake = FakeGapAI()
    run(runtime_conn, fake, name, max_searches=1)
    searches = fake.searches()
    assert len(searches) == 1
    assert open_ in searches[0][3] and held not in searches[0][3]


def test_a_family_we_already_hold_text_for_is_not_searched_at_all(runtime_conn):
    held = fam("H")
    name, brand_id = tracked(runtime_conn, [model(held)])
    other_name, other_id = new_brand(runtime_conn)
    make_doc(runtime_conn, other_id, [f"Model {model(held)}\nSEER2 15.2"])
    fake = FakeGapAI()
    run(runtime_conn, fake, name)
    assert fake.searches() == []
    assert q(runtime_conn, "SELECT COUNT(*) FROM ai_calls WHERE brand_id = %s", (brand_id,))[0][0] == 0


# ---------------------------------------------------------------- the limits

def five_families(conn):
    families = [fam(p) for p in "ABCDE"]
    name, brand_id = tracked(conn, [model(f) for f in families])
    return name, brand_id, families


def test_the_monthly_search_limit_holds_and_the_rest_wait_for_next_month(runtime_conn):
    name, brand_id, families = five_families(runtime_conn)
    fake = FakeGapAI()
    run(runtime_conn, fake, name, max_searches=3, now=T0)
    first = [s[3] for s in fake.searches()]
    assert len(first) == 3

    run(runtime_conn, fake, name, max_searches=3, now=T0 + timedelta(days=1))
    assert len(fake.searches()) == 3  # same month: nothing more

    run(runtime_conn, fake, name, max_searches=3, now=T0 + timedelta(days=31))
    later = [s[3] for s in fake.searches()][3:]
    assert len(later) == 2
    covered = {f for f in families if any(f in query for query in first + later)}
    assert covered == set(families)  # the next month searched exactly the two that were left


def test_searches_already_used_this_month_count_against_the_limit(runtime_conn):
    from crawler.gapfill import MAX_SEARCHES_PER_MONTH
    name, brand_id, families = five_families(runtime_conn)
    with runtime_conn.cursor() as cur:
        cur.execute("INSERT INTO ai_calls (purpose, brand_id, model, searches, cost_usd, created_at) "
                    "VALUES ('gapfill', %s, %s, %s, 1.98, %s)", (brand_id, HAIKU, MAX_SEARCHES_PER_MONTH - 2,
                                                                 "2031-05-02 10:00:00"))
    fake = FakeGapAI()
    run(runtime_conn, fake, name)  # the default monthly limit
    assert len(fake.searches()) == 2


def test_searches_from_discovery_do_not_count_against_the_gap_fill_limit(runtime_conn):
    name, brand_id, families = five_families(runtime_conn)
    with runtime_conn.cursor() as cur:
        cur.execute("INSERT INTO ai_calls (purpose, brand_id, model, searches, cost_usd, created_at) "
                    "VALUES ('discover', %s, %s, 150, 1.5, %s)", (brand_id, HAIKU, "2031-05-02 10:00:00"))
    fake = FakeGapAI()
    run(runtime_conn, fake, name, max_searches=5)
    assert len(fake.searches()) == 5


def test_a_dollar_cap_below_one_search_makes_no_call(runtime_conn):
    name, brand_id = tracked(runtime_conn, [model(fam())])
    fake = FakeGapAI()
    run(runtime_conn, fake, name, cap_usd=Decimal("0.005"))
    assert fake.searches() == []
    run(runtime_conn, fake, name, cap_usd=Decimal("25"))  # control: with room the same family is searched
    assert len(fake.searches()) == 1


def reserve_in_flight(conn, other_brand_id, amount):
    """An extraction batch that is on its way and has set money aside for itself."""
    page_id = make_doc(conn, other_brand_id, ["Some document text"])
    with conn.cursor() as cur:
        cur.execute("UPDATE pages SET ai_batch_id = 'batch_in_flight', ai_reserved_usd = %s WHERE id = %s", (amount, page_id))


def test_money_set_aside_for_extraction_batches_in_flight_counts_against_the_cap(runtime_conn):
    name, brand_id = tracked(runtime_conn, [model(fam())])
    other_name, other_id = new_brand(runtime_conn)
    reserve_in_flight(runtime_conn, other_id, "24.99")  # $0.01 is left under a $25 cap: less than one search's worst case
    fake = FakeGapAI()
    run(runtime_conn, fake, name, cap_usd=Decimal("25"))
    assert fake.searches() == []
    with runtime_conn.cursor() as cur:
        cur.execute("UPDATE pages SET ai_reserved_usd = 0 WHERE ai_batch_id = 'batch_in_flight'")
    run(runtime_conn, fake, name, cap_usd=Decimal("25"))  # control: with the money free again the same family is searched
    assert len(fake.searches()) == 1


def test_a_second_gap_fill_at_the_same_time_is_refused(runtime_conn):
    from crawler.gapfill import gap_fill
    from support import connect
    name, brand_id = tracked(runtime_conn, [model(fam())])
    other = connect("runtime", autocommit=True)
    try:
        with other.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext('crawler-gapfill'))")
            assert cur.fetchone()[0] is True  # the other run holds the lock
        fake = FakeGapAI()
        with pytest.raises(RuntimeError):
            gap_fill(runtime_conn, fake, brand=name, now=T0)
        assert fake.calls == []
        with other.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(hashtext('crawler-gapfill'))")
    finally:
        other.close()
    run(runtime_conn, FakeGapAI(), name)  # control: once released it runs


# ---------------------------------------------------------------- not searched again

def test_a_miss_is_not_searched_again_for_six_months(runtime_conn):
    name, brand_id = tracked(runtime_conn, [model(fam("A")), model(fam("B"))])
    fake = FakeGapAI()  # finds nothing
    run(runtime_conn, fake, name, now=T0)
    assert len(fake.searches()) == 2

    run(runtime_conn, fake, name, now=T0 + timedelta(days=60))
    run(runtime_conn, fake, name, now=T0 + timedelta(days=179))
    assert len(fake.searches()) == 2  # still inside the back-off

    run(runtime_conn, fake, name, now=T0 + timedelta(days=185))
    assert len(fake.searches()) == 4  # six months have passed


GOOD_DOCS = {"documents": [{"url": "https://ruud.test/docs/spec.pdf", "evidence_url": "https://ruud.test/p"}]}


def test_a_search_that_found_documents_is_not_repeated_for_180_days(runtime_conn):
    name, brand_id = tracked(runtime_conn, [model(fam())])
    fake = FakeGapAI(GOOD_DOCS)
    run(runtime_conn, fake, name, now=T0)
    assert len(fake.searches()) == 1
    assert q(runtime_conn, "SELECT COUNT(*) FROM start_urls WHERE brand_id = %s", (brand_id,))[0][0] == 1  # it did find one

    run(runtime_conn, fake, name, now=T0 + timedelta(days=31))
    run(runtime_conn, fake, name, now=T0 + timedelta(days=179))
    assert len(fake.searches()) == 1  # a hit waits as long as a miss
    run(runtime_conn, fake, name, now=T0 + timedelta(days=181))
    assert len(fake.searches()) == 2


def test_documents_that_cannot_be_used_count_as_a_miss_not_a_find(runtime_conn):
    name, brand_id = tracked(runtime_conn, [model(fam())])
    unusable = {"documents": [{"url": "https://www.ahridirectory.org/x", "evidence_url": "https://www.ahridirectory.org/x"},
                              {"url": "https://elsewhere.test/spec.pdf", "evidence_url": "https://elsewhere.test/"}]}
    fake = FakeGapAI(unusable)
    run(runtime_conn, fake, name, now=T0)
    assert len(fake.searches()) == 1
    assert q(runtime_conn, "SELECT COUNT(*) FROM start_urls WHERE brand_id = %s", (brand_id,))[0][0] == 0
    run(runtime_conn, fake, name, now=T0 + timedelta(days=31))
    assert len(fake.searches()) == 1  # still inside the six month back-off


def test_a_miss_for_one_family_does_not_stop_the_others(runtime_conn):
    a, b, c = fam("A"), fam("B"), fam("C")
    name, brand_id = tracked(runtime_conn, [model(a), model(b)])
    run(runtime_conn, FakeGapAI(), name, now=T0)
    add_es_models(runtime_conn, name, [model(c)])  # a new family appears later
    fake = FakeGapAI()
    run(runtime_conn, fake, name, now=T0 + timedelta(days=30))
    assert len(fake.searches()) == 1 and c in fake.searches()[0][3]
