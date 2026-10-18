"""The monthly ENERGY STAR pull (AC / heat pump dataset): matched systems, their ratings, and the confirmed flag.

Ratings for matched systems come from here and nowhere else. The pull goes page by page and writes as it
goes, so the whole dataset is never held in memory. Nothing here commits unless the caller does.
"""
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from crawler.store import manufacturer_for, model_key

DATASET_ID = "83eb-xbyy"
ES_HOSTS = {"data.energystar.gov"}
BASE_URL = "https://data.energystar.gov"
PAGE_SIZE = 10_000

# logical name -> the dataset's column name
FIELDS = {
    "row_id": "pd_id",
    "brand": "outdoor_unit_brand_name",
    "indoor_brand": "indoor_unit_brand_name",
    "ahri_ref": "ahri_reference_number",
    "outdoor_model": "model_number",
    "indoor_model": "indoor_unit_model_number",
    "furnace_model": "furnace_model_number",
    "seer2": "seer2_btu_wh",
    "eer2": "eer2_btu_wh",
    "hspf2": "hspf2_btu_wh",
    "cooling_btuh": "cooling_capacity_btu_h",
    "heating_btuh": "heating_capacity_at_47_f_btu_h",
}


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def _exec(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.lastrowid


def _text(row, field):
    value = row.get(FIELDS[field])
    return str(value).strip() if value is not None else ""


def _decimal(value, places):
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0 or number >= Decimal(10) ** (6 - places):
        return None
    return number.quantize(Decimal(1).scaleb(-places))


def _integer(value):
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return int(number) if number.is_finite() and 0 <= number < 4_000_000_000 else None


def _model(conn, manufacturer_id, number):
    key = model_key(number)[:190]
    if not key:
        return None
    return _exec(conn,
                 "INSERT INTO equipment_models (manufacturer_id, model_number, model_key, energy_star_confirmed) "
                 "VALUES (%s, %s, %s, 1) ON DUPLICATE KEY UPDATE energy_star_confirmed = 1, id = LAST_INSERT_ID(id)",
                 (manufacturer_id, number[:190], key))


def _apply_row(conn, row, row_id, counts):
    """Write one changed dataset row into equipment_systems / members / models."""
    ahri = _text(row, "ahri_ref")[:60]
    outdoor_brand = _text(row, "brand")
    indoor_brand = _text(row, "indoor_brand") or outdoor_brand
    models = [("outdoor", _text(row, "outdoor_model"), outdoor_brand),
              ("indoor", _text(row, "indoor_model"), indoor_brand),
              ("furnace", _text(row, "furnace_model"), indoor_brand)]
    ratings = (_decimal(_text(row, "seer2"), 2), _decimal(_text(row, "eer2"), 2), _decimal(_text(row, "hspf2"), 2),
               _integer(_text(row, "cooling_btuh")), _integer(_text(row, "heating_btuh")))
    members = [(role, model_key(number)) for role, number, _ in models if model_key(number)]
    ratings_key = hashlib.sha256(json.dumps([[str(r) for r in ratings], members]).encode("utf-8")).hexdigest()

    previous = _rows(conn, "SELECT system_id FROM energy_star_rows WHERE dataset_id = %s AND row_id = %s",
                     (DATASET_ID, row_id))
    previous = previous[0][0] if previous else None
    mine = _rows(conn, "SELECT id, superseded FROM equipment_systems WHERE ahri_ref = %s AND ratings_key = %s",
                 (ahri, ratings_key))
    if mine:
        system_id = mine[0][0]
        if mine[0][1]:
            _exec(conn, "UPDATE equipment_systems SET superseded = 0 WHERE id = %s", (system_id,))
            counts["systems_changed"] += 1
    else:
        system_id = _exec(conn,
                          "INSERT INTO equipment_systems (ahri_ref, ratings_key, seer2, eer2, hspf2, cooling_capacity_btuh, "
                          "heating_capacity_btuh) VALUES (%s, %s, %s, %s, %s, %s, %s)", (ahri, ratings_key, *ratings))
        for role, number, brand in models:
            if not model_key(number):
                continue
            model_id = _model(conn, manufacturer_for(conn, brand), number)
            _exec(conn, "INSERT INTO equipment_system_members (system_id, model_id, role) VALUES (%s, %s, %s) "
                        "ON DUPLICATE KEY UPDATE id = id", (system_id, model_id, role))
        counts["systems_changed" if previous else "systems_added"] += 1
    if previous and previous != system_id:
        # the row's old system is replaced, unless another row of the dataset still produces it
        still_used = _rows(conn, "SELECT 1 FROM energy_star_rows WHERE system_id = %s AND NOT (dataset_id = %s AND row_id = %s) "
                                 "LIMIT 1", (previous, DATASET_ID, row_id))
        if not still_used:
            _exec(conn, "UPDATE equipment_systems SET superseded = 1 WHERE id = %s", (previous,))
    return system_id


def _process_page(conn, rows, counts):
    for row in rows:
        counts["rows"] += 1
        if not isinstance(row, dict):
            continue
        ahri = _text(row, "ahri_ref")
        if not ahri:
            counts["skipped_no_ahri"] += 1
            continue
        outdoor = _text(row, "outdoor_model")
        row_id = (_text(row, "row_id") or f"{ahri}|{outdoor}|{_text(row, 'indoor_model')}")[:100]
        raw = json.dumps(row, sort_keys=True)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        stored = _rows(conn, "SELECT row_hash FROM energy_star_rows WHERE dataset_id = %s AND row_id = %s",
                       (DATASET_ID, row_id))
        if stored and stored[0][0] == digest:
            counts["rows_unchanged"] += 1
            continue
        _exec(conn, "SAVEPOINT es_row")
        try:
            system_id = _apply_row(conn, row, row_id, counts)
            _exec(conn,
                  "INSERT INTO energy_star_rows (dataset_id, row_id, ahri_ref, brand_name, model_number, model_key, row_json, "
                  "row_hash, system_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE ahri_ref = VALUES(ahri_ref), "
                  "brand_name = VALUES(brand_name), model_number = VALUES(model_number), model_key = VALUES(model_key), "
                  "row_json = VALUES(row_json), row_hash = VALUES(row_hash), system_id = VALUES(system_id)",
                  (DATASET_ID, row_id, ahri[:60], _text(row, "brand")[:190], outdoor[:190], model_key(outdoor)[:190], raw,
                   digest, system_id))
            _exec(conn, "RELEASE SAVEPOINT es_row")
        except Exception:
            # the row is undone as a whole and not recorded, so the next pull tries it again
            _exec(conn, "ROLLBACK TO SAVEPOINT es_row")
            counts["rows_bad"] += 1


def pull(conn, fetcher, *, base_url=BASE_URL, allowed_hosts=ES_HOSTS, page_size=PAGE_SIZE, now=None, after_page=None):
    """Pull the dataset unless its updated time is unchanged. Returns a summary with a status of
    pulled, unchanged, failed or refused."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    counts = {"status": "pulled", "rows": 0, "systems_added": 0, "systems_changed": 0, "skipped_no_ahri": 0,
              "rows_unchanged": 0, "rows_bad": 0}

    def get(path, params=None):
        url = base_url.rstrip("/") + path + (("?" + urlencode(params)) if params else "")
        return fetcher.get_json(url, allowed_hosts)

    meta = get(f"/api/views/{DATASET_ID}.json")
    if meta.outcome != "fetched":
        counts["status"] = "refused" if meta.outcome == "refused" else "failed"
        return counts
    try:
        updated = int(json.loads(meta.body)["rowsUpdatedAt"])
    except (ValueError, KeyError, TypeError):
        counts["status"] = "failed"
        return counts
    state = _rows(conn, "SELECT rows_updated_at FROM energy_star_state WHERE dataset_id = %s", (DATASET_ID,))
    if state and state[0][0] == updated:
        counts["status"] = "unchanged"
        return counts

    offset = 0
    while True:
        page = get(f"/resource/{DATASET_ID}.json", {"$limit": page_size, "$offset": offset, "$order": ":id"})
        if page.outcome != "fetched":
            counts["status"] = "refused" if page.outcome == "refused" else "failed"
            return counts
        try:
            rows = json.loads(page.body)
        except ValueError:
            counts["status"] = "failed"
            return counts
        if not isinstance(rows, list):
            counts["status"] = "failed"
            return counts
        _process_page(conn, rows, counts)
        if after_page:
            after_page()
        if len(rows) < page_size:
            break
        offset += page_size

    _exec(conn, "INSERT INTO energy_star_state (dataset_id, rows_updated_at, pulled_at) VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE rows_updated_at = VALUES(rows_updated_at), pulled_at = VALUES(pulled_at)",
          (DATASET_ID, updated, now))
    return counts
