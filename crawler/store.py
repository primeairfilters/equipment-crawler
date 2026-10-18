"""Writes checked facts into the equipment tables. One document is all or nothing. Nothing commits."""
import hashlib
import re
from decimal import Decimal, InvalidOperation

# An ENERGY STAR model "has specs" when its manufacturer has a model with current specs whose key is a prefix
# of its own (or equal): a table for the base model RA1424AJ covers RA1424AJ1NA. Used as `AND NOT EXISTS (...)`.
HAS_NO_SPECS_SQL = (
    "NOT EXISTS (SELECT 1 FROM equipment_models b JOIN equipment_specs s ON s.model_id = b.id "
    "WHERE b.manufacturer_id = em.manufacturer_id AND LENGTH(b.model_key) >= 5 "
    "AND em.model_key LIKE CONCAT(b.model_key, '%%') AND s.superseded = 0 AND s.hidden_at IS NULL)"
)

ALLOWED_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5")
_DROP_WORDS = {"SALES", "COMPANY", "CO", "INC", "LLC", "CORP", "CORPORATION", "MFG", "MANUFACTURING"}


class BadExtractedBy(ValueError):
    pass


def model_key(raw):
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


def alias_key(raw):
    """Uppercase, cut at the first comma, no punctuation, without SALES/COMPANY/CO/INC/LLC/..."""
    first = (raw or "").split(",")[0].upper()
    words = re.sub(r"[^\w\s]", "", first).split()
    return " ".join(w for w in words if w not in _DROP_WORDS)


def check_extracted_by(value):
    if value in ("energy_star", "rules"):
        return
    if isinstance(value, str) and value.startswith("ai:") and value[3:] in ALLOWED_MODELS:
        return
    raise BadExtractedBy(f"extracted_by {value!r} is not allowed")


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def _upsert_id(conn, sql, args):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.lastrowid


def _number(value):
    if not re.fullmatch(r"-?\d{1,13}(\.\d{1,4})?", value):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def value_key(value):
    return hashlib.sha256(" ".join(str(value).split()).lower().encode("utf-8")).hexdigest()


def manufacturer_for(conn, name):
    """The manufacturer a brand text belongs to, however it is written; made if new."""
    key = alias_key(name)
    if not key:
        raise ValueError("no manufacturer name")
    found = _rows(conn, "SELECT manufacturer_id FROM manufacturer_aliases WHERE alias_key = %s", (key,))
    if found:
        return found[0][0]
    short = (name or "").split(",")[0].strip()[:190]
    manufacturer_id = _upsert_id(
        conn, "INSERT INTO manufacturers (name, name_key) VALUES (%s, %s) ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)",
        (short, key))
    _upsert_id(conn, "INSERT INTO manufacturer_aliases (manufacturer_id, alias, alias_key) VALUES (%s, %s, %s) "
                     "ON DUPLICATE KEY UPDATE id = id", (manufacturer_id, short, key))
    return manufacturer_id


def _manufacturer(conn, brand_id):
    name = _rows(conn, "SELECT name FROM brands WHERE id = %s", (brand_id,))[0][0]
    key = alias_key(name)
    found = _rows(conn, "SELECT manufacturer_id FROM manufacturer_aliases WHERE alias_key = %s", (key,))
    if found:
        return found[0][0]
    manufacturer_id = _upsert_id(
        conn, "INSERT INTO manufacturers (name, name_key) VALUES (%s, %s) ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)",
        (name, key))
    _upsert_id(conn, "INSERT INTO manufacturer_aliases (manufacturer_id, alias, alias_key) VALUES (%s, %s, %s) "
                     "ON DUPLICATE KEY UPDATE id = id", (manufacturer_id, name, key))
    return manufacturer_id


def _save_spec(conn, model_id, source_id, spec, extracted_by):
    key = spec["key"]
    value = str(spec["value"])
    if len(value) > 500 or len(str(spec.get("unit") or "")) > 30 or len(key) > 100:
        raise ValueError("a value is too long to store")
    vkey = value_key(value)
    existing = _rows(conn, "SELECT id, value_key, superseded FROM equipment_specs "
                           "WHERE model_id = %s AND spec_key = %s AND source_id = %s", (model_id, key, source_id))
    current = [r for r in existing if r[1] == vkey]
    if current:
        spec_id, _, superseded = current[0]
        if not superseded:
            return
        conn_exec(conn, "UPDATE equipment_specs SET superseded = 0 WHERE id = %s", (spec_id,))
    else:
        spec_id = _upsert_id(
            conn,
            "INSERT INTO equipment_specs (model_id, source_id, spec_key, value_text, value_num, unit, value_key, extracted_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (model_id, source_id, key, value, _number(value), spec.get("unit"), vkey, extracted_by))
    conn_exec(conn, "UPDATE equipment_specs SET superseded = 1 WHERE model_id = %s AND spec_key = %s "
                    "AND source_id = %s AND id <> %s AND superseded = 0", (model_id, key, source_id, spec_id))


def conn_exec(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)


def save_document(conn, *, brand_id, source_url, title, content_sha, models, extracted_by):
    check_extracted_by(extracted_by)
    conn_exec(conn, "SAVEPOINT save_document")
    try:
        manufacturer_id = _manufacturer(conn, brand_id)
        source_id = _upsert_id(
            conn,
            "INSERT INTO equipment_sources (url, url_sha, title, content_sha) VALUES (%s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE title = VALUES(title), content_sha = VALUES(content_sha), id = LAST_INSERT_ID(id)",
            (source_url, hashlib.sha256(source_url.encode("utf-8")).hexdigest(), title, content_sha))
        for entry in models:
            model_id = _upsert_id(
                conn,
                "INSERT INTO equipment_models (manufacturer_id, model_number, model_key, is_base_model) "
                "VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)",
                (manufacturer_id, entry["model_number"], model_key(entry["model_number"]),
                 1 if entry.get("is_base_model") else 0))
            conn_exec(conn, "INSERT INTO equipment_model_sources (model_id, source_id) VALUES (%s, %s) "
                            "ON DUPLICATE KEY UPDATE id = id", (model_id, source_id))
            for spec in entry["specs"]:
                _save_spec(conn, model_id, source_id, spec, extracted_by)
    except Exception:
        conn_exec(conn, "ROLLBACK TO SAVEPOINT save_document")
        raise
    conn_exec(conn, "RELEASE SAVEPOINT save_document")
