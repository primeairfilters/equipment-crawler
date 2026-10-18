"""P5: when does an ENERGY STAR model count as "has specs"?

It does when the same manufacturer has a model with a current spec (not superseded, not hidden) whose model key
is equal to it or is a prefix of it that is at least 5 characters long. This decides both the "models with no
specs yet" count in `status` and which families gap-fill searches for.
"""
import pytest

from fakeai import FakeGapAI
from p3support import new_brand, q
from test_p5_gapfill import T0, add_es_models, fam, run, tracked


def with_spec(conn, brand_id, model_number):
    from crawler.store import save_document
    save_document(conn, brand_id=brand_id, source_url=f"https://ruud.test/{model_number}", title="t", content_sha="a" * 64,
                  extracted_by="rules", models=[{"model_number": model_number, "is_base_model": True,
                                                 "specs": [{"key": "seer2", "value": "15.2", "unit": None}]}])


def count_without_specs(conn, name):
    from crawler.brands import status
    mine = [r for r in status(conn) if r["brand"] == name]
    assert len(mine) == 1
    return mine[0]["es_models_without_specs"]


def test_a_model_that_extends_a_base_model_with_specs_has_specs(runtime_conn):
    family = fam("K")
    base, full = family + "AJ", family + "AJ1NA"
    name, brand_id = tracked(runtime_conn, [full])
    with_spec(runtime_conn, brand_id, base)
    assert count_without_specs(runtime_conn, name) == 0
    fake = FakeGapAI()
    run(runtime_conn, fake, name, now=T0)
    assert fake.searches() == []


def test_a_model_with_its_own_specs_has_specs(runtime_conn):
    family = fam("K")
    name, brand_id = tracked(runtime_conn, [family + "AJ1NA"])
    with_spec(runtime_conn, brand_id, family + "AJ1NA")
    assert count_without_specs(runtime_conn, name) == 0


def never_counts_as_having_specs(kind, family):
    """(spec-bearing base model, how to spoil its spec afterwards) for each way the rule must NOT apply."""
    if kind == "prefix_too_short":
        return family[:4], None
    if kind == "not_a_prefix":
        return family[:5] + ("Y" if family[4] == "Z" else "Z") + "AJ", None
    if kind == "superseded":
        return family + "AJ", "UPDATE equipment_specs SET superseded = 1"
    if kind == "hidden":
        return family + "AJ", "UPDATE equipment_specs SET hidden_at = NOW()"


@pytest.mark.parametrize("kind", ["prefix_too_short", "not_a_prefix", "superseded", "hidden"])
def test_these_do_not_count_as_having_specs_so_the_model_is_counted_and_searched(runtime_conn, kind):
    family = fam("K")
    full = family + "AJ1NA"
    base, spoil = never_counts_as_having_specs(kind, family)
    name, brand_id = tracked(runtime_conn, [full])
    with_spec(runtime_conn, brand_id, base)
    if spoil:
        with runtime_conn.cursor() as cur:
            cur.execute(spoil + " WHERE model_id IN (SELECT id FROM equipment_models WHERE model_key = %s)", (base,))
    assert count_without_specs(runtime_conn, name) == 1
    fake = FakeGapAI()
    run(runtime_conn, fake, name, now=T0)
    assert len(fake.searches()) == 1 and family in fake.searches()[0][3]


def test_specs_of_another_manufacturers_model_do_not_count(runtime_conn):
    family = fam("K")
    name, brand_id = tracked(runtime_conn, [family + "AJ1NA"])
    other_name, other_id = new_brand(runtime_conn)
    with_spec(runtime_conn, other_id, family + "AJ")  # same letters, different manufacturer
    assert count_without_specs(runtime_conn, name) == 1
    fake = FakeGapAI()
    run(runtime_conn, fake, name, now=T0)
    assert len(fake.searches()) == 1
