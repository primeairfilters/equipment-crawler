"""P5: the monthly ENERGY STAR pull (AC / heat pump dataset 83eb-xbyy) into matched systems.

Served by a fake Socrata server in this process. pull(conn, fetcher, *, base_url, allowed_hosts, page_size, now)
never commits. The data host is a fixed allow-list, not the brand-approved hosts, but the pull still goes
through the Fetcher's politeness (10 second gap, user agent, rest after failures) and address policy.
"""
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from fakees import DATASET, FakeSocrata, ahri, es_row
from p3support import new_brand, q
from test_p2_fetch import World

ES_HOST = "data.es.test"
UA = "TracforceEquipmentBot/1.0 (+mailto:support+crawler@tracforce.ai)"


@pytest.fixture
def w(runtime_conn):
    world = World(runtime_conn)
    world.es = FakeSocrata(world.site)
    try:
        yield world
    finally:
        world.site.stop()


def pull(world, *, page_size=10, fetcher=None, **kw):
    from crawler.energystar import pull as do_pull
    kw.setdefault("allowed_hosts", {ES_HOST})
    return do_pull(world.conn, fetcher or world.fetcher(), base_url=f"http://{ES_HOST}:{world.site.port}",
                   page_size=page_size, now=world.clock.now(), **kw)


def brand_text(name):
    return f"{name} Sales Company, Fort Smith, Arkansas"


def systems(world, refs):
    return q(world.conn, "SELECT ahri_ref, seer2, eer2, hspf2, cooling_capacity_btuh, heating_capacity_btuh, superseded "
                         "FROM equipment_systems WHERE ahri_ref IN %s ORDER BY ahri_ref, id", (tuple(refs),))


def members(world, ref):
    return set(q(world.conn,
                 "SELECT m.role, em.model_key FROM equipment_system_members m JOIN equipment_systems s ON s.id = m.system_id "
                 "JOIN equipment_models em ON em.id = m.model_id WHERE s.ahri_ref = %s AND s.superseded = 0", (ref,)))


# ---------------------------------------------------------------- the constants

def test_the_data_host_is_a_fixed_allow_list():
    from crawler import energystar
    assert energystar.DATASET_ID == DATASET
    assert energystar.ES_HOSTS == {"data.energystar.gov"}
    assert {"brand", "ahri_ref", "outdoor_model", "indoor_model", "furnace_model", "seer2", "eer2", "hspf2",
            "cooling_btuh", "heating_btuh", "row_id"} <= set(energystar.FIELDS)


# ---------------------------------------------------------------- a pull creates systems

def test_a_pull_creates_systems_with_members_and_typed_ratings(w):
    name, brand_id = new_brand(w.conn)
    a1, a2 = ahri(), ahri()
    w.es.rows = [
        es_row(brand_text(name), a1, "RA15AZ24AJ3CA", "RC24AFTN"),
        es_row(brand_text(name), a2, "RA15AZ24AJ3CA", "RC30AFTN", "RGPQ10EAUER", seer2="15.0", eer2="11.5", hspf2="8.5",
               cooling="30000", heating="22000"),
    ]
    summary = pull(w)
    assert summary["status"] == "pulled" and summary["rows"] == 2
    assert summary["systems_added"] == 2 and summary["skipped_no_ahri"] == 0

    got = systems(w, [a1, a2])
    assert got == (
        tuple(sorted([(a1, Decimal("16.5"), Decimal("12.1"), None, 24000, None, 0),
                      (a2, Decimal("15.0"), Decimal("11.5"), Decimal("8.5"), 30000, 22000, 0)]))
    )
    assert members(w, a1) == {("outdoor", "RA15AZ24AJ3CA"), ("indoor", "RC24AFTN")}
    assert members(w, a2) == {("outdoor", "RA15AZ24AJ3CA"), ("indoor", "RC30AFTN"), ("furnace", "RGPQ10EAUER")}


def test_models_go_under_one_manufacturer_however_the_brand_is_written_and_are_marked_energy_star(w):
    from p3support import settle  # noqa: F401
    from crawler.store import save_document
    name, brand_id = new_brand(w.conn)
    save_document(w.conn, brand_id=brand_id, source_url="https://x.test/a", title="t", content_sha="a" * 64,
                  extracted_by="rules", models=[{"model_number": "RA15AZ24AJ3CA", "is_base_model": False,
                                                 "specs": [{"key": "seer2", "value": "15.2", "unit": None}]}])
    before = q(w.conn, "SELECT id FROM manufacturers WHERE name_key = %s", (name.upper(),))
    assert len(before) == 1

    w.es.rows = [
        es_row(f"{name} Sales Company, Fort Smith, Arkansas", ahri(), "RA15AZ24AJ3CA", "RC24AFTN"),
        es_row(f"{name.upper()} SALES COMPANY, FORT SMITH, ARKANSAS", ahri(), "RA15AZ30AJ3CA", "RC30AFTN"),
        es_row(f"{name} Sales Company", ahri(), "RA15AZ36AJ3CA", "RC36AFTN"),
    ]
    assert pull(w)["status"] == "pulled"
    assert q(w.conn, "SELECT id FROM manufacturers WHERE name_key = %s", (name.upper(),)) == before
    rows = q(w.conn, "SELECT model_key, energy_star_confirmed FROM equipment_models WHERE manufacturer_id = %s ORDER BY model_key",
             (before[0][0],))
    keys = [r[0] for r in rows]
    assert {"RA15AZ24AJ3CA", "RA15AZ30AJ3CA", "RA15AZ36AJ3CA", "RC24AFTN", "RC30AFTN", "RC36AFTN"} <= set(keys)
    assert all(r[1] == 1 for r in rows)
    # the model the crawl already knew is the same row, not a duplicate
    assert keys.count("RA15AZ24AJ3CA") == 1


def test_rows_without_an_ahri_reference_are_skipped_and_counted(w):
    name, brand_id = new_brand(w.conn)
    good = ahri()
    w.es.rows = [
        es_row(brand_text(name), "", "RA15AZ24AJ3CA", "RC24AFTN"),
        es_row(brand_text(name), None, "RA15AZ30AJ3CA", "RC30AFTN"),      # the column is missing altogether
        es_row(brand_text(name), good, "RA15AZ36AJ3CA", "RC36AFTN"),
    ]
    summary = pull(w)
    assert summary["rows"] == 3 and summary["skipped_no_ahri"] == 2 and summary["systems_added"] == 1
    assert systems(w, [good])[0][0] == good  # the good row went in, so the skipping above was selective
    skipped = q(w.conn, "SELECT COUNT(*) FROM equipment_models WHERE model_key IN ('RA15AZ24AJ3CA', 'RA15AZ30AJ3CA')")[0][0]
    assert skipped == 0


# ---------------------------------------------------------------- when nothing changed

def test_an_unchanged_rows_updated_time_means_only_the_check_is_made(w):
    name, brand_id = new_brand(w.conn)
    w.es.rows = [es_row(brand_text(name), ahri(), "RA15AZ24AJ3CA", "RC24AFTN")]
    assert pull(w)["status"] == "pulled"
    data_before, meta_before = len(w.es.data_requests()), len(w.es.meta_requests())
    assert data_before >= 1  # the first pull really fetched data

    again = pull(w)
    assert again["status"] == "unchanged"
    assert len(w.es.data_requests()) == data_before
    assert len(w.es.meta_requests()) == meta_before + 1


def test_a_new_rows_updated_time_with_identical_rows_changes_nothing(w):
    name, brand_id = new_brand(w.conn)
    refs = [ahri(), ahri()]
    w.es.rows = [es_row(brand_text(name), refs[0], "RA15AZ24AJ3CA", "RC24AFTN"),
                 es_row(brand_text(name), refs[1], "RA15AZ30AJ3CA", "RC30AFTN")]
    pull(w)
    before = systems(w, refs)
    models_before = q(w.conn, "SELECT COUNT(*) FROM equipment_models")[0][0]
    w.es.updated_at += 3600
    again = pull(w)
    assert again["status"] == "pulled" and again["rows"] == 2
    assert again["systems_added"] == 0 and again["systems_changed"] == 0 and again["rows_unchanged"] == 2
    assert systems(w, refs) == before
    assert q(w.conn, "SELECT COUNT(*) FROM equipment_models")[0][0] == models_before


def test_a_changed_rating_adds_a_new_system_and_supersedes_the_old_one_and_going_back_brings_it_back(w):
    name, brand_id = new_brand(w.conn)
    ref, rid = ahri(), uuid.uuid4().hex[:12]

    def rows(seer2):
        return [es_row(brand_text(name), ref, "RA15AZ24AJ3CA", "RC24AFTN", seer2=seer2, row_id=rid)]

    w.es.rows = rows("16.5")
    pull(w)
    assert [(r[1], r[6]) for r in systems(w, [ref])] == [(Decimal("16.5"), 0)]

    w.es.rows, w.es.updated_at = rows("17.0"), w.es.updated_at + 1
    summary = pull(w)
    assert summary["systems_changed"] == 1
    assert sorted((r[1], r[6]) for r in systems(w, [ref])) == [(Decimal("16.5"), 1), (Decimal("17.0"), 0)]
    assert members(w, ref) == {("outdoor", "RA15AZ24AJ3CA"), ("indoor", "RC24AFTN")}  # the current system has its members

    w.es.rows, w.es.updated_at = rows("16.5"), w.es.updated_at + 1
    pull(w)
    assert sorted((r[1], r[6]) for r in systems(w, [ref])) == [(Decimal("16.5"), 0), (Decimal("17.0"), 1)]  # no third row


def test_a_row_missing_from_a_later_pull_is_not_hidden(w):
    name, brand_id = new_brand(w.conn)
    keep, gone = ahri(), ahri()
    w.es.rows = [es_row(brand_text(name), keep, "RA15AZ24AJ3CA", "RC24AFTN"),
                 es_row(brand_text(name), gone, "RA15AZ30AJ3CA", "RC30AFTN")]
    pull(w)
    w.es.rows, w.es.updated_at = w.es.rows[:1], w.es.updated_at + 1
    assert pull(w)["rows"] == 1
    assert [r[6] for r in systems(w, [gone])] == [0]
    assert members(w, gone) == {("outdoor", "RA15AZ30AJ3CA"), ("indoor", "RC30AFTN")}
    assert q(w.conn, "SELECT hidden_at FROM equipment_models WHERE model_key = 'RA15AZ30AJ3CA'") == ((None,),)


# ---------------------------------------------------------------- paging

@pytest.mark.parametrize("count,expected", [(25, [0, 10, 20]), (20, [0, 10, 20]), (5, [0]), (0, [0])])
def test_paging_asks_until_a_short_or_empty_page_and_then_stops(w, count, expected):
    name, brand_id = new_brand(w.conn)
    w.es.rows = [es_row(brand_text(name), ahri(), f"RA15AZ{n:02d}AJ3CA", f"RC{n:02d}AFTN") for n in range(count)]
    summary = pull(w, page_size=10)
    assert summary["status"] == "pulled" and summary["rows"] == count
    assert w.es.offsets() == expected
    assert all("%24order" in r["path"] or "$order" in r["path"] for r in w.es.data_requests())  # a stable order between pages


def test_a_failed_page_fails_the_pull_and_the_next_pull_tries_again(w):
    name, brand_id = new_brand(w.conn)
    w.es.rows = [es_row(brand_text(name), ahri(), f"RA15AZ{n:02d}AJ3CA", f"RC{n:02d}AFTN") for n in range(15)]
    w.es.fail_offsets = {10}
    assert pull(w)["status"] == "failed"
    w.es.fail_offsets = set()
    w.clock.advance(3 * 24 * 3600)  # past any rest
    second = pull(w)
    assert second["status"] == "pulled" and second["rows"] == 15  # not "unchanged": the failed pull was not recorded as done


def test_a_failed_check_fails_the_pull_without_asking_for_data(w):
    w.es.fail_meta = True
    assert pull(w)["status"] == "failed"
    assert w.es.data_requests() == []
    assert w.es.meta_requests()


# ---------------------------------------------------------------- where it may fetch, and how politely

def test_a_host_outside_the_allow_list_is_refused_without_a_request(w):
    summary = pull(w, allowed_hosts={"data.energystar.gov"})
    assert summary["status"] == "refused"
    assert w.site.requests == []


def test_the_pull_does_not_need_any_brand_host_to_be_approved(w):
    assert q(w.conn, "SELECT COUNT(*) FROM brand_domains WHERE host = %s", (ES_HOST,))[0][0] == 0
    assert pull(w)["status"] == "pulled"


def test_the_real_address_policy_still_applies(w):
    summary = pull(w, fetcher=w.fetcher(allow_loopback=False))
    assert summary["status"] == "refused"
    assert w.site.requests == []


def test_requests_carry_the_user_agent_and_keep_the_ten_second_gap(w):
    name, brand_id = new_brand(w.conn)
    w.es.rows = [es_row(brand_text(name), ahri(), f"RA15AZ{n:02d}AJ3CA", f"RC{n:02d}AFTN") for n in range(25)]
    pull(w, page_size=10)
    seen = [r for r in w.site.requests if r["host"] == ES_HOST]
    assert len(seen) >= 4  # the check and three pages
    assert {r["headers"].get("user-agent") for r in seen} == {UA}
    for a, b in zip(seen, seen[1:]):
        assert (b["t"] - a["t"]).total_seconds() >= 10


# ---------------------------------------------------------------- status

def test_status_counts_energy_star_models_with_no_specs_yet(w):
    from crawler.brands import status
    from crawler.store import save_document
    name, brand_id = new_brand(w.conn)
    save_document(w.conn, brand_id=brand_id, source_url="https://x.test/a", title="t", content_sha="a" * 64, extracted_by="rules",
                  models=[{"model_number": "RA15AZ24AJ3CA", "is_base_model": False,
                           "specs": [{"key": "seer2", "value": "15.2", "unit": None}]}])
    w.es.rows = [es_row(brand_text(name), ahri(), "RA15AZ24AJ3CA", "RC24AFTN"),
                 es_row(brand_text(name), ahri(), "RA15AZ30AJ3CA", "RC30AFTN")]
    pull(w)
    mine = [r for r in status(w.conn) if r["brand"] == name]
    assert len(mine) == 1
    # confirmed models: RA15AZ24AJ3CA (has a spec), RA15AZ30AJ3CA, RC24AFTN, RC30AFTN (none)
    assert mine[0]["es_models_without_specs"] == 3
