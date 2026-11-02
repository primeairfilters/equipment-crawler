"""P23 (D87–D90, user 2026-10-31): a brand's trades. On the admin page a brand gets one or more of Tracforce's trades
("Goodman" is HVAC, Bosch is HVAC and Appliance Repair); one is required there, a brand added before (Ruud) has none,
which means any trade, as before. The trades steer what the crawler does for that brand:
- the first-night search asks only for the brand's equipment in those trades (Honda: generators, not cars);
- a manual whose tagged trade is none of its brand's trades is kept but not live (Breeze never sees it), and goes live
  again once the trade is added; one already live leaves when its trade is taken off;
- gap-fill and coverage only count ENERGY STAR categories of those trades (Samsung: no TV manuals);
- `./crawl add-brands <file>` loads a list of "name | trade, trade" lines, every brand paused.
Rolled back after each test; fake AI, no network."""
import io
import json
import uuid

import psycopg
import pytest

from fakeai import FakeDiscoverAI, FakeGapAI
from p3support import make_doc, new_brand, q
from support import connect
from test_p12_build import PAGES, document, live
from test_p5_gapfill import T0, add_es_models, fam, model, run

TRADES_OK = ["hvac", "plumbing", "electrical", "commercial_refrigeration", "appliance_repair", "pool_spa",
             "generators", "water_treatment", "garage_doors"]


@pytest.fixture
def admin_conn():
    conn = connect("admin", autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def set_trades(conn, brand_id, trades):
    q(conn, "UPDATE brands SET trades = %s WHERE id = %s", (trades, brand_id))


def found_by(conn, document_id, brand_id):
    """Link a document to a page of the brand, as a crawl does."""
    page_id = make_doc(conn, brand_id, ["x"])
    q(conn, "INSERT INTO document_urls (document_id, page_id, url) VALUES (%s, %s, 'http://p3.test/x')",
      (document_id, page_id))


# ---------------------------------------------------------------- the column and who may write it

def test_the_admin_page_can_add_and_change_a_brands_trades(admin_conn):
    with admin_conn.cursor() as cur:
        cur.execute("INSERT INTO control.brands (name, state, trades) VALUES (%s, 'paused', %s) RETURNING id",
                    ("Probe Trades " + uuid.uuid4().hex[:6], ["hvac", "appliance_repair"]))
        brand_id = cur.fetchone()[0]
        cur.execute("UPDATE control.brands SET trades = %s WHERE id = %s", (["hvac"], brand_id))
        cur.execute("SELECT trades FROM control.brands WHERE id = %s", (brand_id,))
        assert cur.fetchone()[0] == ["hvac"]


def test_a_new_trade_added_to_tracforces_list_is_not_refused_by_the_database(admin_conn):
    """User 2026-11-01: one trades list, so a trade added to the main app later is stored without a migration. The
    admin page and add-brands check the list; the database keeps only the shape (0024)."""
    with admin_conn.cursor() as cur:
        cur.execute("INSERT INTO control.brands (name, trades) VALUES (%s, %s)",
                    ("Probe New Trade " + uuid.uuid4().hex[:6], ["hvac", "solar_panels"]))


@pytest.mark.parametrize("bad", [["HVAC"], ["hvac,plumbing"], [""], ["pool spa"]])
def test_a_trade_that_is_not_a_plain_lowercase_key_is_refused(admin_conn, bad):
    with pytest.raises(psycopg.errors.CheckViolation):
        with admin_conn.cursor() as cur:
            cur.execute("INSERT INTO control.brands (name, trades) VALUES (%s, %s)",
                        ("Probe Bad Trade " + uuid.uuid4().hex[:6], bad))


def test_a_brand_with_no_trades_set_has_an_empty_list(runtime_conn):
    _, brand_id = new_brand(runtime_conn)
    assert q(runtime_conn, "SELECT trades FROM brands WHERE id = %s", (brand_id,)) == (([],),)


def test_sync_copies_the_pages_trades_to_the_crawler_brand(runtime_conn):
    from crawler.control import sync
    name = "Zz" + uuid.uuid4().hex[:10]
    q(runtime_conn, "INSERT INTO control.brands (name, state, trades) VALUES (%s, 'paused', %s)", (name, ["generators"]))
    sync(runtime_conn)
    assert q(runtime_conn, "SELECT b.trades FROM brands b JOIN control.brands c ON c.crawl_brand_id = b.id "
                           "WHERE c.name = %s", (name,)) == ((["generators"],),)
    q(runtime_conn, "UPDATE control.brands SET trades = %s WHERE name = %s", (["generators", "electrical"], name))
    sync(runtime_conn)
    assert q(runtime_conn, "SELECT b.trades FROM brands b JOIN control.brands c ON c.crawl_brand_id = b.id "
                           "WHERE c.name = %s", (name,)) == ((["generators", "electrical"],),)


# ---------------------------------------------------------------- the first-night search

def test_the_first_night_search_asks_only_for_the_brands_trades(runtime_conn):
    from crawler.discover import discover
    name, brand_id = new_brand(runtime_conn)
    set_trades(runtime_conn, brand_id, ["generators"])
    fake = FakeDiscoverAI({"domains": [], "document_hosts": [], "start_urls": []})
    discover(runtime_conn, fake, name)
    assert fake.discover_calls()[-1][5] == ["generators"]


def test_a_brand_with_no_trades_is_searched_as_before(runtime_conn):
    from crawler.discover import discover
    name, _ = new_brand(runtime_conn)
    fake = FakeDiscoverAI({"domains": [], "document_hosts": [], "start_urls": []})
    discover(runtime_conn, fake, name)
    assert fake.discover_calls()[-1][5] == []


def test_the_search_question_names_the_trades_in_plain_words():
    from crawler.ai import discover_question
    assert discover_question("Honda", []) == "Brand: Honda"
    text = discover_question("Honda", ["generators", "pool_spa"])
    assert text.startswith("Brand: Honda\n") and "generators" in text.lower() and "pool and spa" in text.lower()


# ---------------------------------------------------------------- manuals outside the brand's trades

def test_a_manual_outside_its_brands_trades_is_kept_but_never_goes_live(runtime_conn):
    from crawler.kb import blocked, build, publish
    name, brand_id = new_brand(runtime_conn)
    set_trades(runtime_conn, brand_id, ["generators"])
    doc, _, _ = document(runtime_conn, name, trade="hvac")
    found_by(runtime_conn, doc, brand_id)
    build(runtime_conn, doc, pages=PAGES)
    assert blocked(runtime_conn, doc) == "its trade is not one of its brand's trades"
    assert publish(runtime_conn, doc)[0] is False
    assert live(runtime_conn, doc) == () and q(runtime_conn, "SELECT state FROM documents WHERE id = %s", (doc,)) == (("built",),)


def test_a_manual_in_one_of_its_brands_trades_goes_live(runtime_conn):
    from crawler.kb import blocked
    name, brand_id = new_brand(runtime_conn)
    set_trades(runtime_conn, brand_id, ["hvac", "appliance_repair"])
    doc, _, _ = document(runtime_conn, name, trade="appliance_repair")
    found_by(runtime_conn, doc, brand_id)
    assert blocked(runtime_conn, doc) is None


def test_a_brand_with_no_trades_never_holds_a_manual_back(runtime_conn):
    from crawler.kb import blocked
    name, brand_id = new_brand(runtime_conn)
    doc, _, _ = document(runtime_conn, name, trade="other")
    found_by(runtime_conn, doc, brand_id)
    assert blocked(runtime_conn, doc) is None


def test_a_manual_found_by_two_brands_goes_live_when_either_has_its_trade(runtime_conn):
    from crawler.kb import blocked
    name, generators = new_brand(runtime_conn)
    _, plumbing = new_brand(runtime_conn)
    set_trades(runtime_conn, generators, ["generators"])
    set_trades(runtime_conn, plumbing, ["plumbing"])
    doc, _, _ = document(runtime_conn, name, trade="plumbing")
    found_by(runtime_conn, doc, generators)
    found_by(runtime_conn, doc, plumbing)
    assert blocked(runtime_conn, doc) is None


def test_adding_the_trade_later_puts_the_held_manual_live_without_tagging_it_again(runtime_conn):
    from crawler.kb import build, build_and_publish, publish
    name, brand_id = new_brand(runtime_conn)
    set_trades(runtime_conn, brand_id, ["generators"])
    doc, _, _ = document(runtime_conn, name, trade="hvac")
    found_by(runtime_conn, doc, brand_id)
    build(runtime_conn, doc, pages=PAGES)
    publish(runtime_conn, doc)
    assert live(runtime_conn, doc) == ()
    set_trades(runtime_conn, brand_id, ["generators", "hvac"])
    build_and_publish(runtime_conn)
    assert live(runtime_conn, doc) != ()


def test_taking_a_trade_off_takes_its_live_manuals_out_of_breeze_and_putting_it_back_returns_them(runtime_conn):
    from crawler.kb import build, follow_trades, publish
    name, brand_id = new_brand(runtime_conn)
    doc, _, _ = document(runtime_conn, name, trade="hvac")
    found_by(runtime_conn, doc, brand_id)
    build(runtime_conn, doc, pages=PAGES)
    assert publish(runtime_conn, doc)[0] is True
    shown = live(runtime_conn, doc)
    assert shown != ()

    set_trades(runtime_conn, brand_id, ["generators"])
    assert follow_trades(runtime_conn) == {"withdrawn": 1, "restored": 0}
    assert live(runtime_conn, doc) == ()
    assert q(runtime_conn, "SELECT state FROM documents WHERE id = %s", (doc,)) == (("live",),)  # its build is kept

    set_trades(runtime_conn, brand_id, ["generators", "hvac"])
    assert follow_trades(runtime_conn) == {"withdrawn": 0, "restored": 1}
    assert live(runtime_conn, doc) == shown


def test_follow_trades_never_brings_back_a_hidden_manual(runtime_conn):
    from crawler.kb import build, follow_trades, hide, publish
    name, brand_id = new_brand(runtime_conn)
    doc, _, _ = document(runtime_conn, name, trade="hvac")
    found_by(runtime_conn, doc, brand_id)
    build(runtime_conn, doc, pages=PAGES)
    publish(runtime_conn, doc)
    hide(runtime_conn, "document", doc, reason="takedown request", by_whom="test")
    assert follow_trades(runtime_conn)["restored"] == 0
    assert live(runtime_conn, doc) == ()


# ---------------------------------------------------------------- gap-fill and coverage

def test_energy_star_categories_belong_to_trades():
    from crawler.coverage import category_trades
    assert category_trades("Furnaces") == {"hvac"}
    assert category_trades("Boilers") == {"hvac", "plumbing"}
    assert category_trades("Water Heaters - Non Solar Electric and Non Solar Gas") == {"plumbing"}
    assert category_trades("Pool Pumps") == {"pool_spa"}
    assert category_trades("Clothes Washers") == {"appliance_repair"}
    assert category_trades("Televisions") == set() and category_trades("Computers") == set()
    assert category_trades(None) == set()


def es_listed(conn, brand_id, family_name, category):
    q(conn, "INSERT INTO public_models (brand_id, source, list_id, brand_name, model_number, model_key, family, category) "
            "VALUES (%s, 'energystar_index', %s, 'x', %s, %s, %s, %s)",
      (brand_id, uuid.uuid4().hex, model(family_name), model(family_name), family_name, category))


def test_gap_fill_skips_families_outside_the_brands_trades(runtime_conn):
    from crawler.brands import approve_host
    name, brand_id = new_brand(runtime_conn)
    approve_host(runtime_conn, "samsung.test", brand=name)
    washer, tv = fam("W"), fam("T")
    add_es_models(runtime_conn, name, [model(washer), model(tv)])
    es_listed(runtime_conn, brand_id, washer, "Clothes Washers")
    es_listed(runtime_conn, brand_id, tv, "Televisions")
    set_trades(runtime_conn, brand_id, ["hvac", "appliance_repair"])
    fake = FakeGapAI()
    run(runtime_conn, fake, name)
    queries = [s[3] for s in fake.searches()]
    assert any(washer in query for query in queries) and not any(tv in query for query in queries)


def test_gap_fill_for_a_brand_with_no_trades_is_as_before(runtime_conn):
    from crawler.brands import approve_host
    name, brand_id = new_brand(runtime_conn)
    approve_host(runtime_conn, "samsung.test", brand=name)
    tv = fam("T")
    add_es_models(runtime_conn, name, [model(tv)])
    es_listed(runtime_conn, brand_id, tv, "Televisions")
    fake = FakeGapAI()
    run(runtime_conn, fake, name)
    assert any(tv in s[3] for s in fake.searches())


def test_coverage_only_counts_families_in_the_brands_trades(runtime_conn):
    from crawler.coverage import families
    _, brand_id = new_brand(runtime_conn)
    washer, tv = fam("W"), fam("T")
    es_listed(runtime_conn, brand_id, washer, "Clothes Washers")
    es_listed(runtime_conn, brand_id, tv, "Televisions")
    assert {f for f, _, _ in families(runtime_conn, brand_id)} == {washer, tv}
    set_trades(runtime_conn, brand_id, ["appliance_repair"])
    assert {f for f, _, _ in families(runtime_conn, brand_id)} == {washer}


# ---------------------------------------------------------------- the brand list

def control_row(conn, name):
    rows = q(conn, "SELECT state, trades, added_by FROM control.brands WHERE name_key = lower(%s)", (name,))
    return rows[0] if rows else None


def test_a_brand_list_adds_every_brand_paused_with_its_trades(runtime_conn):
    from crawler.brandlist import load
    a, b = "Zz" + uuid.uuid4().hex[:8], "Zz" + uuid.uuid4().hex[:8]
    out = load(runtime_conn, ["# a comment", "", f"{a} | hvac", f"  {b}  |  plumbing , pool_spa  "], TRADES_OK)
    assert out == {"added": 2, "trades_set": 0, "already_there": 0}
    assert control_row(runtime_conn, a) == ("paused", ["hvac"], "brand list")
    assert control_row(runtime_conn, b) == ("paused", ["plumbing", "pool_spa"], "brand list")


def test_the_same_brand_twice_is_one_brand_with_both_trades(runtime_conn):
    from crawler.brandlist import load
    a = "Zz" + uuid.uuid4().hex[:8]
    load(runtime_conn, [f"{a} | hvac", f"{a.upper()} | appliance_repair, hvac"], TRADES_OK)
    assert control_row(runtime_conn, a)[1] == ["hvac", "appliance_repair"]


def test_a_brand_already_there_with_no_trades_gets_them_and_one_with_trades_is_left_alone(runtime_conn):
    from crawler.brandlist import load
    bare, set_already = "Zz" + uuid.uuid4().hex[:8], "Zz" + uuid.uuid4().hex[:8]
    q(runtime_conn, "INSERT INTO control.brands (name, state) VALUES (%s, 'active')", (bare,))
    q(runtime_conn, "INSERT INTO control.brands (name, state, trades) VALUES (%s, 'active', %s)", (set_already, ["hvac"]))
    out = load(runtime_conn, [f"{bare} | hvac, plumbing", f"{set_already} | plumbing"], TRADES_OK)
    assert out == {"added": 0, "trades_set": 1, "already_there": 1}
    assert control_row(runtime_conn, bare)[:2] == ("active", ["hvac", "plumbing"])  # its state is not touched
    assert control_row(runtime_conn, set_already)[1] == ["hvac"]


def test_a_list_with_a_bad_line_adds_nothing_and_says_which_line(runtime_conn):
    from crawler.brandlist import BadList, load
    a = "Zz" + uuid.uuid4().hex[:8]
    for bad in (f"{a}", f"{a} |", f"{a} | automotive", " | hvac"):
        with pytest.raises(BadList) as err:
            load(runtime_conn, [f"{a} | hvac", bad], TRADES_OK)
        assert "line 2" in str(err.value)
        assert control_row(runtime_conn, a) is None


def test_the_users_brand_list_is_valid_and_has_every_trade():
    from crawler.brandlist import parse
    with open("ops/brands-2026-10-31.txt") as handle:
        brands = parse(handle.read().splitlines(), TRADES_OK)
    assert len(brands) == 354
    assert {t for _, trades in brands.values() for t in trades} == set(TRADES_OK)
    assert brands["rheem"][1] == ["hvac", "plumbing", "pool_spa"] and brands["ruud"][1] == ["hvac", "plumbing"]


# ---------------------------------------------------------------- Tracforce's one trades list (user 2026-11-01)

class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_trades_come_from_tracforces_list_without_other():
    from crawler.trades import fetch
    body = {"trades": [{"value": "hvac", "label": "HVAC"}, {"value": "solar_panels", "label": "Solar Panels"},
                       {"value": "other", "label": "Other"}]}
    asked = []
    def opener(url, timeout):
        asked.append(url)
        return _Answer(json.dumps(body).encode())
    assert fetch("http://main.test/api/public/trades", opener) == ["hvac", "solar_panels"]
    assert asked == ["http://main.test/api/public/trades"]


@pytest.mark.parametrize("answer", [b"not json", b'{"trades": []}', b'{"nope": 1}'])
def test_an_unreadable_trades_list_says_so(answer):
    from crawler.trades import Unavailable, fetch
    with pytest.raises(Unavailable):
        fetch("http://main.test/x", lambda url, timeout: _Answer(answer))


def test_a_brand_list_takes_a_trade_new_to_tracforces_list(runtime_conn):
    from crawler.brandlist import load
    a = "Zz" + uuid.uuid4().hex[:8]
    load(runtime_conn, [f"{a} | solar_panels, hvac"], ["hvac", "solar_panels"])
    assert control_row(runtime_conn, a)[1] == ["hvac", "solar_panels"]


def test_add_brands_with_the_trades_list_down_adds_nothing(runtime_conn, monkeypatch, tmp_path):
    import crawler.trades
    from test_p11_cli import cli
    def down(url=None, opener=None):
        raise crawler.trades.Unavailable("Could not read Tracforce's trades list")
    monkeypatch.setattr(crawler.trades, "fetch", down)
    a = "Zz" + uuid.uuid4().hex[:8]
    listing = tmp_path / "brands.txt"
    listing.write_text(f"{a} | hvac\n")
    code, out, err = cli(runtime_conn, "add-brands", str(listing))
    assert code == 1 and "Nothing added" in out and "trades list" in out
    assert control_row(runtime_conn, a) is None


def test_a_manual_taken_out_for_its_trade_is_not_live_without_sections(runtime_conn):
    # prod parse issues 57-61, 2026-11-02: Raypak pool heater manuals and a return form under Ruud (hvac, plumbing)
    # left Breeze as they should, but check and the parse-issue scan both listed them as live with no live sections.
    from crawler.issues import scan
    from crawler.kb import build, check, follow_trades, publish
    name, brand_id = new_brand(runtime_conn)
    doc, _, _ = document(runtime_conn, name, trade="hvac")
    found_by(runtime_conn, doc, brand_id)
    build(runtime_conn, doc, pages=PAGES)
    publish(runtime_conn, doc)
    set_trades(runtime_conn, brand_id, ["generators"])
    assert follow_trades(runtime_conn)["withdrawn"] == 1
    assert doc not in check(runtime_conn)["live_without_sections"]
    scan(runtime_conn)
    assert q(runtime_conn, "SELECT COUNT(*) FROM parse_issues WHERE document_id = %s AND kind = 'no_sections'",
             (doc,))[0][0] == 0
