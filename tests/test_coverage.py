"""P20 (D76; Q24): coverage and gap filling. A brand's public model list comes from the ENERGY STAR Model Index
(every category, so every trade it certifies); coverage is the share of its model families with a manual, per
category; each run searches the web (gap-fill, within its caps) and the brand site's own search for families with no
manual; a brand with nothing new and nothing left to search is in upkeep. The admin page reads control.brand_coverage.

Fake web server (also standing in for data.energystar.gov), fake AI, `_testing`, one rolled-back transaction per test."""
import json
from datetime import datetime, timedelta

import pytest

from fakeai import FakeAI, FakeGapAI
from p11support import HTML, crawl, serve, start, world  # noqa: F401  (world is a fixture)
from p3support import make_page, make_pdf, q, settle
from test_p11b_tagging import tags

T0 = datetime(2031, 6, 1, 3, 0)


def index_rows(world):
    name = world.brand.upper()
    return [
        {"pd_id": "1", "brand_name": name, "model_number": "RA15AZ24AJ3CA", "product_category": "Central Air Conditioners",
         "product_type": "CAC - Split System"},
        {"pd_id": "2", "brand_name": name, "model_number": "RA15AZ36AJ3CA", "product_category": "Central Air Conditioners",
         "product_type": "CAC - Split System"},
        {"pd_id": "3", "brand_name": name, "model_number": "RP20AZ**AJ", "product_category": "Heat Pumps",
         "product_type": "HP - Split System"},
        {"pd_id": "4", "brand_name": name, "model_number": "PROG50-38N", "product_category": "Water Heaters",
         "product_type": "Gas Storage"},
        {"pd_id": "5", "brand_name": name, "model_number": "AB*", "product_category": "Water Heaters"},  # too short
    ]


@pytest.fixture
def energystar(world, monkeypatch):
    """The Model Index on the fake server at es.test, answering with this brand's rows."""
    import crawler.coverage
    world.site.route(f"/resource/{crawler.coverage.MODEL_INDEX}.json", json.dumps(index_rows(world)).encode(),
                     headers={"Content-Type": "application/json"}, host="es.test")
    monkeypatch.setattr(crawler.coverage, "BASE_URL", world.url("es.test", "").rstrip("/"))
    monkeypatch.setattr(crawler.coverage, "ES_HOSTS", {"es.test"})
    return world


def pull(world, now=T0):
    from crawler.coverage import pull_models
    return pull_models(world.conn, world.fetcher(), world.brand_id, world.brand, now=now)


def a_manual_for(world, model="RA15AZ24AJ3CA"):
    """A tagged manual of the brand covering the model (its family RA15AZ)."""
    make_page(world.conn, world.brand_id, make_pdf([[f"Installation Instructions {model}", f"Model {model}"]]), is_pdf=True)
    settle(world.conn, FakeAI(lambda r: tags(models=(model,))), world.brand)


# ---------------------------------------------------------------- the public list

def test_listed_model_numbers_keep_their_fixed_part():
    from crawler.coverage import fixed_key
    assert fixed_key("RP20AZ**AJ") == "RP20AZ" and fixed_key("RA15AZ24AJ3CA") == "RA15AZ24AJ3CA"
    assert fixed_key("GGF*****24") == "GGF"


def test_the_brands_public_models_are_pulled_once_a_month_and_saved_as_its_models(energystar):
    from crawler.store import brand_manufacturer
    world = energystar
    assert pull(world) == {"status": "pulled", "rows": 4}
    assert sorted(q(world.conn, "SELECT family, category FROM public_models WHERE brand_id = %s", (world.brand_id,))) == [
        ("PROG50", "Water Heaters"), ("RA15AZ", "Central Air Conditioners"), ("RA15AZ", "Central Air Conditioners"),
        ("RP20AZ", "Heat Pumps")]
    maker = brand_manufacturer(world.conn, world.brand_id)
    assert q(world.conn, "SELECT COUNT(*) FROM models WHERE manufacturer_id = %s AND energy_star_confirmed",
             (maker,))[0][0] == 4
    assert pull(world, T0 + timedelta(days=10))["status"] == "recent"
    assert pull(world, T0 + timedelta(days=31))["status"] == "pulled"


def test_a_failed_pull_is_tried_again_next_run(world, monkeypatch):
    import crawler.coverage
    monkeypatch.setattr(crawler.coverage, "BASE_URL", world.url("es.test", "").rstrip("/"))
    monkeypatch.setattr(crawler.coverage, "ES_HOSTS", {"es.test"})
    assert pull(world)["status"] == "failed"  # nothing served: 404
    assert q(world.conn, "SELECT COUNT(*) FROM public_model_pulls WHERE brand_id = %s", (world.brand_id,))[0][0] == 0


# ---------------------------------------------------------------- coverage

def test_coverage_counts_families_with_a_manual_overall_and_per_category(energystar):
    from crawler.coverage import measure
    world = energystar
    pull(world)
    a_manual_for(world)
    row = measure(world.conn, world.brand_id)
    assert (row["families"], row["covered"]) == (3, 1)
    assert row["by_category"] == {"Central Air Conditioners": {"families": 1, "covered": 1},
                                  "Heat Pumps": {"families": 1, "covered": 0},
                                  "Water Heaters": {"families": 1, "covered": 0}}


def test_the_phase_says_what_the_brand_needs(energystar):
    from crawler.coverage import measure
    world = energystar
    assert measure(world.conn, world.brand_id)["phase"] == "upkeep"  # nothing listed, nothing to do
    start(world)
    assert measure(world.conn, world.brand_id)["phase"] == "no_public_list"  # a start address still to visit
    pull(world)
    assert measure(world.conn, world.brand_id, gap_searches=2)["phase"] == "filling_gaps"
    q(world.conn, "UPDATE start_urls SET status = 'stopped' WHERE brand_id = %s", (world.brand_id,))
    assert measure(world.conn, world.brand_id)["phase"] == "filling_gaps"  # its families are not searched yet
    for fam in ("PROG50", "RA15AZ", "RP20AZ"):
        q(world.conn, "INSERT INTO gapfill_searches (brand_id, family, searched_at) VALUES (%s, %s, now())",
          (world.brand_id, fam))
    assert measure(world.conn, world.brand_id)["phase"] == "upkeep"


# ---------------------------------------------------------------- the site's own search

FORM = ('<html><body><form action="/search" method="get"><input type="hidden" name="lang" value="en">'
        '<input type="text" name="q"></form><a href="/x">x</a></body></html>')


def test_a_sites_search_form_becomes_an_address_with_the_words_in_it():
    from crawler.coverage import search_template
    assert search_template(FORM, "https://www.ruud.com/products/") == "https://www.ruud.com/search?lang=en&q={q}"
    assert search_template('<form action="/find"><input type="search" name="term"></form>',
                           "https://www.ruud.com/") == "https://www.ruud.com/find?term={q}"
    assert search_template('<form method="post" action="/s"><input name="q"></form>', "https://www.ruud.com/") is None
    assert search_template('<form action="https://other.example/s"><input name="q"></form>',
                           "https://www.ruud.com/") is None
    assert search_template('<form action="/login"><input name="user"></form>', "https://www.ruud.com/") is None


def test_the_crawl_keeps_the_brand_sites_search_form(world):
    world.site.route("/start", FORM.encode(), headers=HTML, host="ruud.test")
    start(world)
    crawl(world)
    assert q(world.conn, "SELECT host, template FROM site_search_templates WHERE brand_id = %s", (world.brand_id,)) == (
        ("ruud.test", world.url("ruud.test", "/search").split("/search")[0] + "/search?lang=en&q={q}"),)


def test_site_search_queues_a_search_per_uncovered_family_a_few_each_run(energystar, monkeypatch):
    import crawler.coverage
    from crawler.coverage import site_search
    world = energystar
    pull(world)
    a_manual_for(world)
    q(world.conn, "INSERT INTO site_search_templates (brand_id, host, template, found_on) VALUES (%s, 'ruud.test', "
                  "'https://ruud.test/search?q={q}', 'https://ruud.test/')", (world.brand_id,))
    monkeypatch.setattr(crawler.coverage, "SITE_SEARCHES_PER_RUN", 1)
    assert site_search(world.conn, world.brand_id, now=T0) == 1
    assert site_search(world.conn, world.brand_id, now=T0) == 1
    assert site_search(world.conn, world.brand_id, now=T0) == 0  # both uncovered families searched: wait 180 days
    urls = sorted(r[0] for r in q(world.conn, "SELECT url FROM start_urls WHERE brand_id = %s AND found_by = 'gapfill'",
                                  (world.brand_id,)))
    assert urls == ["https://ruud.test/search?q=PROG50", "https://ruud.test/search?q=RP20AZ"]
    assert site_search(world.conn, world.brand_id, now=T0 + timedelta(days=181)) == 1


def test_site_search_only_uses_a_site_approved_for_its_pages(energystar):
    from crawler.coverage import site_search
    world = energystar
    pull(world)
    q(world.conn, "INSERT INTO site_search_templates (brand_id, host, template, found_on) VALUES (%s, 'files.test', "
                  "'https://files.test/search?q={q}', 'https://files.test/')", (world.brand_id,))
    q(world.conn, "INSERT INTO brand_domains (brand_id, host, status, found_by, scope) VALUES (%s, 'files.test', "
                  "'approved', 'ai', 'pdf')", (world.brand_id,))
    assert site_search(world.conn, world.brand_id, now=T0) == 0


# ---------------------------------------------------------------- in the nightly run

class Searching(FakeAI):
    """Tagging and gap-fill in one fake client."""

    def __init__(self):
        super().__init__(lambda r: tags())
        self.gap = FakeGapAI()

    def supports_web_search(self, model):
        return True

    def find_documents(self, *a):
        return self.gap.find_documents(*a)


def test_the_nightly_run_measures_coverage_and_fills_gaps(energystar):
    from crawler.auto import run_auto
    from p11support import Checker
    from test_p14_search import BagEmbedder
    world = energystar
    q(world.conn, "UPDATE brands SET discovered_at = now() WHERE id = %s", (world.brand_id,))
    client = Searching()
    summary = run_auto(world.conn, fetcher=world.fetcher(), ai_client=client, embedder=BagEmbedder(),
                       link_checker=Checker(), sleep=lambda s: None)
    cov = summary["brands"][world.brand]["coverage"]
    assert (cov["families"], cov["covered"], cov["phase"]) == (3, 0, "filling_gaps")
    assert cov["gap_searches"] == len(client.gap.searches()) == 3  # one web search per uncovered family
    report = q(world.conn, "SELECT report FROM runs WHERE id = %s", (summary["run_id"],))[0][0]
    assert "coverage: 0 of 3 listed model families have a manual" in report
    shown = q(world.conn, "SELECT families, covered, coverage_pct, phase FROM control.brand_coverage WHERE brand = %s",
              (world.brand,))
    assert shown == ((3, 0, 0, "filling_gaps"),)


def test_gap_fill_stops_at_its_share_for_the_run(energystar):
    from crawler.gapfill import gap_fill
    world = energystar
    pull(world)
    client = FakeGapAI()
    assert gap_fill(world.conn, client, brand=world.brand, now=T0, limit=2)["searched"] == 2


def test_a_brand_with_families_still_to_search_is_not_in_upkeep(energystar):
    # review 2026-10-30: an over-budget brand (no searches made) read as upkeep while families waited
    from crawler.coverage import measure
    world = energystar
    pull(world)
    assert measure(world.conn, world.brand_id)["phase"] == "filling_gaps"
    for fam in ("PROG50", "RA15AZ", "RP20AZ"):
        q(world.conn, "INSERT INTO gapfill_searches (brand_id, family, searched_at) VALUES (%s, %s, now())",
          (world.brand_id, fam))
    assert measure(world.conn, world.brand_id)["phase"] == "upkeep"


def test_a_hidden_manual_does_not_cover_its_family(energystar):
    from crawler.coverage import measure
    world = energystar
    pull(world)
    a_manual_for(world)
    q(world.conn, "UPDATE documents SET hidden_at = now() WHERE id IN (SELECT document_id FROM document_models "
                  "WHERE model_key = 'RA15AZ24AJ3CA')")
    assert measure(world.conn, world.brand_id)["covered"] == 0
