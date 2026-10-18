"""P5: whose models they are, when an ENERGY STAR row names the indoor unit's brand separately, and rows with no brand."""
from fakees import ahri, es_row
from p3support import new_brand, q
from test_p5_energystar import brand_text, pull, systems, w  # noqa: F401  (w is the fake-server fixture)


def maker_by_role(world, ref):
    return dict(q(world.conn,
                  "SELECT m.role, f.name_key FROM equipment_system_members m JOIN equipment_systems s ON s.id = m.system_id "
                  "JOIN equipment_models em ON em.id = m.model_id JOIN manufacturers f ON f.id = em.manufacturer_id "
                  "WHERE s.ahri_ref = %s AND s.superseded = 0", (ref,)))


def test_the_indoor_unit_and_furnace_use_the_indoor_brand_when_the_row_gives_one(w):
    outdoor_name, _ = new_brand(w.conn)
    indoor_name, _ = new_brand(w.conn)
    ref = ahri()
    w.es.rows = [es_row(brand_text(outdoor_name), ref, "RA15AZ24AJ3CA", "RC24AFTN", "RGPQ10EAUER",
                        indoor_brand=brand_text(indoor_name))]
    assert pull(w)["systems_added"] == 1
    assert maker_by_role(w, ref) == {"outdoor": outdoor_name.upper(), "indoor": indoor_name.upper(),
                                     "furnace": indoor_name.upper()}


def test_without_an_indoor_brand_everything_is_the_outdoor_brand(w):
    name, _ = new_brand(w.conn)
    ref = ahri()
    w.es.rows = [es_row(brand_text(name), ref, "RA15AZ24AJ3CA", "RC24AFTN", "RGPQ10EAUER")]
    pull(w)
    assert maker_by_role(w, ref) == {"outdoor": name.upper(), "indoor": name.upper(), "furnace": name.upper()}


def test_an_empty_indoor_brand_falls_back_to_the_outdoor_brand(w):
    name, _ = new_brand(w.conn)
    ref = ahri()
    w.es.rows = [es_row(brand_text(name), ref, "RA15AZ24AJ3CA", "RC24AFTN", indoor_brand="  ")]
    pull(w)
    assert maker_by_role(w, ref) == {"outdoor": name.upper(), "indoor": name.upper()}


def test_rows_with_no_usable_brand_are_skipped_and_counted_and_leave_nothing_behind(w):
    name, _ = new_brand(w.conn)
    good, bad1, bad2, bad3 = ahri(), ahri(), ahri(), ahri()
    w.es.rows = [
        es_row(brand_text(name), good, "RA15AZ24AJ3CA", "RC24AFTN"),
        es_row("", bad1, "XB11CD24AJ3CA", "XB24AFTN"),
        es_row("   ", bad2, "XB12CD24AJ3CA", "XB30AFTN"),
        es_row(", Fort Smith", bad3, "XB13CD24AJ3CA", "XB36AFTN"),   # nothing before the first comma
    ]
    summary = pull(w)
    assert summary["status"] == "pulled" and summary["rows"] == 4
    assert summary["rows_bad"] == 3 and summary["systems_added"] == 1
    assert [r[0] for r in systems(w, [good])] == [good]  # the good row went in, so the skipping was selective
    # nothing half-written: no system, and no model, for the rows that could not be placed
    assert systems(w, [bad1, bad2, bad3]) == ()
    assert q(w.conn, "SELECT COUNT(*) FROM equipment_models WHERE model_key LIKE %s", ("XB1_CD%",))[0][0] == 0
