"""P5: several dataset rows with the same AHRI reference. A row only replaces the system it produced itself."""
from decimal import Decimal

from fakees import ahri, es_row
from p3support import new_brand, q
from test_p5_energystar import brand_text, pull, w  # noqa: F401  (w is the fake-server fixture)


def current_and_old(world, ref):
    """(seer2, superseded, indoor model) for every system with this AHRI reference, oldest first."""
    return [(r[0], r[1], r[2]) for r in q(
        world.conn,
        "SELECT s.seer2, s.superseded, em.model_key FROM es_systems s "
        "JOIN es_system_members m ON m.system_id = s.id AND m.role = 'indoor' "
        "JOIN models em ON em.id = m.model_id WHERE s.ahri_ref = %s ORDER BY s.id", (ref,))]


def test_changing_one_row_does_not_supersede_the_system_of_another_row_with_the_same_ref(w):
    name, _ = new_brand(w.conn)
    ref = ahri()

    def rows(seer2_first):
        return [es_row(brand_text(name), ref, "RA15AZ24AJ3CA", "RC24AFTN", seer2=seer2_first, row_id="row-one"),
                es_row(brand_text(name), ref, "RA15AZ24AJ3CA", "RC30AFTN", seer2="16.5", row_id="row-two")]

    w.es.rows = rows("16.5")
    first = pull(w)
    assert first["systems_added"] == 2
    assert current_and_old(w, ref) == [(Decimal("16.50"), 0, "RC24AFTN"), (Decimal("16.50"), 0, "RC30AFTN")]

    w.es.rows, w.es.updated_at = rows("17.0"), w.es.updated_at + 1
    second = pull(w)
    assert second["systems_changed"] == 1 and second["systems_added"] == 0
    assert current_and_old(w, ref) == [
        (Decimal("16.50"), 1, "RC24AFTN"),   # row one's old system
        (Decimal("16.50"), 0, "RC30AFTN"),   # row two's system is untouched
        (Decimal("17.00"), 0, "RC24AFTN"),   # row one's new system
    ]
    row_two_members = q(w.conn, "SELECT m.role, em.model_key FROM es_system_members m "
                                "JOIN es_systems s ON s.id = m.system_id JOIN models em ON em.id = m.model_id "
                                "WHERE s.ahri_ref = %s AND NOT s.superseded AND s.seer2 = 16.5", (ref,))
    assert set(row_two_members) == {("outdoor", "RA15AZ24AJ3CA"), ("indoor", "RC30AFTN")}  # and it still has its members


def test_identical_rows_with_the_same_ref_share_one_system_until_none_of_them_produces_it(w):
    name, _ = new_brand(w.conn)
    ref = ahri()

    def rows(first, second):
        return [es_row(brand_text(name), ref, "RA15AZ24AJ3CA", "RC24AFTN", seer2=first, row_id="row-one"),
                es_row(brand_text(name), ref, "RA15AZ24AJ3CA", "RC24AFTN", seer2=second, row_id="row-two")]

    def seers():
        return sorted((x[0], x[1]) for x in current_and_old(w, ref))

    w.es.rows = rows("16.5", "16.5")
    pull(w)
    assert q(w.conn, "SELECT COUNT(*) FROM es_systems WHERE ahri_ref = %s", (ref,))[0][0] == 1  # shared

    w.es.rows, w.es.updated_at = rows("17.0", "16.5"), w.es.updated_at + 1
    pull(w)
    assert seers() == [(Decimal("16.50"), 0), (Decimal("17.00"), 0)]  # row two still produces the old one, so it stays current

    w.es.rows, w.es.updated_at = rows("17.0", "17.0"), w.es.updated_at + 1
    pull(w)
    assert seers() == [(Decimal("16.50"), 1), (Decimal("17.00"), 0)]  # nobody produces the old one now, so it is superseded
    assert q(w.conn, "SELECT COUNT(*) FROM es_systems WHERE ahri_ref = %s", (ref,))[0][0] == 2  # and nothing was added or deleted
