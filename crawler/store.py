"""Writes a tagged document into kb: the document (one per unique PDF), where it was found, and its models.
One document is all or nothing. Nothing commits.
"""
import re

# An ENERGY STAR model "has a document" when a document with a doc_type (other than not_equipment) is linked to a model
# of its manufacturer whose key (5+ characters) is a prefix of its own or equal: a document on RA1424AJ covers RA1424AJ1NA.
# Used as `AND NOT EXISTS (...)` with the ENERGY STAR model aliased `em`.
HAS_NO_DOCUMENT_SQL = (
    "NOT EXISTS (SELECT 1 FROM document_models dm JOIN documents d ON d.id = dm.document_id "
    "WHERE dm.manufacturer_id = em.manufacturer_id AND length(dm.model_key) >= 5 "
    "AND left(em.model_key, length(dm.model_key)) = dm.model_key "
    "AND d.doc_type IS NOT NULL AND d.doc_type <> 'not_equipment')"
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


def _one(conn, sql, args):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()[0]


def conn_exec(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)


def _new_manufacturer(conn, name, key):
    manufacturer_id = _one(
        conn, "INSERT INTO manufacturers (name, name_key) VALUES (%s, %s) "
              "ON CONFLICT (name_key) DO UPDATE SET name_key = EXCLUDED.name_key RETURNING id", (name, key))
    conn_exec(conn, "INSERT INTO manufacturer_aliases (manufacturer_id, alias, alias_key) VALUES (%s, %s, %s) "
                    "ON CONFLICT (alias_key) DO NOTHING", (manufacturer_id, name, key))
    return manufacturer_id


def manufacturer_for(conn, name):
    """The manufacturer a brand text belongs to, however it is written; made if new."""
    key = alias_key(name)
    if not key:
        raise ValueError("no manufacturer name")
    found = _rows(conn, "SELECT manufacturer_id FROM manufacturer_aliases WHERE alias_key = %s", (key,))
    if found:
        return found[0][0]
    return _new_manufacturer(conn, (name or "").split(",")[0].strip()[:190], key)


def brand_manufacturer(conn, brand_id):
    name = _rows(conn, "SELECT name FROM brands WHERE id = %s", (brand_id,))[0][0]
    key = alias_key(name)
    found = _rows(conn, "SELECT manufacturer_id FROM manufacturer_aliases WHERE alias_key = %s", (key,))
    if found:
        return found[0][0]
    return _new_manufacturer(conn, name, key)


def page_manufacturer(conn, brand_id, page_id):
    """The manufacturer of the document a page holds: its brand's, or for a file uploaded on the admin page the one
    the user picked or its text named (None: no brand found, D84)."""
    upload = _rows(conn, "SELECT manufacturer_id FROM control.uploads WHERE page_id = %s", (page_id,))
    return upload[0][0] if upload else brand_manufacturer(conn, brand_id)


def save_model(conn, manufacturer_id, number, *, is_base_model=False, energy_star=False):
    """The model's id; made if new. energy_star=True marks it confirmed by ENERGY STAR."""
    return _one(
        conn,
        "INSERT INTO models (manufacturer_id, model_number, model_key, is_base_model, energy_star_confirmed) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (manufacturer_id, model_key) DO UPDATE SET "
        "energy_star_confirmed = models.energy_star_confirmed OR EXCLUDED.energy_star_confirmed RETURNING id",
        (manufacturer_id, number[:190], model_key(number)[:190], is_base_model, energy_star))


TAG_COLUMNS = ("trade", "equipment_type", "language", "form_number")


def save_document(conn, *, brand_id, page_id, source_url, title, content_sha, models, extracted_by, doc_type=None,
                  tags=None, tagger_version=None):
    """Upsert the document for this content, record the URL it was found at, and link its models.

    tags: trade / equipment_type / language / form_number (None values keep what is stored). A tagged document is
    state 'tagged' unless it is already further along. Same manufacturer + form number links it to the document it
    replaces (D55)."""
    check_extracted_by(extracted_by)
    tags = {k: v for k, v in (tags or {}).items() if k in TAG_COLUMNS}
    conn_exec(conn, "SAVEPOINT save_document")
    try:
        manufacturer_id = page_manufacturer(conn, brand_id, page_id)
        document_id = _one(
            conn,
            "INSERT INTO documents (content_sha, manufacturer_id, title, canonical_url, doc_type, tagged_by, tagger_version, "
            "trade, equipment_type, language, form_number, state) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'tagged') ON CONFLICT (content_sha) DO UPDATE SET "
            "title = COALESCE(EXCLUDED.title, documents.title), "
            # a tag the user set by hand (kb.set_tags) is never overwritten
            "doc_type = CASE WHEN 'doc_type' = ANY (documents.user_tags) THEN documents.doc_type "
            "ELSE COALESCE(EXCLUDED.doc_type, documents.doc_type) END, "
            "manufacturer_id = COALESCE(documents.manufacturer_id, EXCLUDED.manufacturer_id), "
            "trade = CASE WHEN 'trade' = ANY (documents.user_tags) THEN documents.trade "
            "ELSE COALESCE(EXCLUDED.trade, documents.trade) END, "
            "equipment_type = CASE WHEN 'equipment_type' = ANY (documents.user_tags) THEN documents.equipment_type "
            "ELSE COALESCE(EXCLUDED.equipment_type, documents.equipment_type) END, "
            "language = COALESCE(EXCLUDED.language, documents.language), "
            "form_number = COALESCE(EXCLUDED.form_number, documents.form_number), "
            "tagged_by = EXCLUDED.tagged_by, tagger_version = COALESCE(EXCLUDED.tagger_version, documents.tagger_version), "
            "state = CASE WHEN documents.state = 'new' THEN 'tagged' ELSE documents.state END, "
            "updated_at = (now() AT TIME ZONE 'UTC') RETURNING id",
            (content_sha, manufacturer_id, title, source_url, doc_type, extracted_by, tagger_version,
             tags.get("trade"), tags.get("equipment_type"), tags.get("language"), tags.get("form_number")))
        if tags.get("form_number"):
            # each version of this form (same base without its "Rev" mark, migration 0008) points at the one before it
            # by revision, then by id: tagged in any order (59 <- 1074 <- 1079; doc 840, the older revision of 50, was
            # found after it). A link to a document outside the form (the same address's earlier PDF) is kept.
            conn_exec(conn, "UPDATE documents d SET replaces_id = g.prev FROM (SELECT o.id, lag(o.id) OVER "
                            "(ORDER BY o.form_rev, o.id) AS prev FROM documents o JOIN documents t ON t.id = %s "
                            "AND o.manufacturer_id = t.manufacturer_id AND o.form_base = t.form_base "
                            "AND o.language IS NOT DISTINCT FROM t.language) g WHERE d.id = g.id "
                            "AND d.replaces_id IS DISTINCT FROM g.prev AND (d.replaces_id IS NULL OR EXISTS "
                            "(SELECT 1 FROM documents r WHERE r.id = d.replaces_id AND r.manufacturer_id = d.manufacturer_id "
                            "AND r.form_base = d.form_base AND r.language IS NOT DISTINCT FROM d.language))",
                      (document_id,))
        # sections already built keep true copies of the tags; a manual retagged as not equipment leaves Breeze at once
        conn_exec(conn, "UPDATE sections s SET manufacturer_id = d.manufacturer_id, trade = d.trade, "
                        "equipment_type = d.equipment_type, doc_type = d.doc_type, language = d.language, "
                        "is_live = s.is_live AND d.doc_type IS DISTINCT FROM 'not_equipment' FROM documents d "
                        "WHERE d.id = %s AND s.document_id = d.id AND ((s.manufacturer_id, s.trade, s.equipment_type, "
                        "s.doc_type, s.language) IS DISTINCT FROM (d.manufacturer_id, d.trade, d.equipment_type, d.doc_type, "
                        "d.language) OR (s.is_live AND d.doc_type = 'not_equipment'))", (document_id,))
        conn_exec(conn, "INSERT INTO document_urls (document_id, page_id, url) VALUES (%s, %s, %s) "
                        "ON CONFLICT (document_id, page_id) DO UPDATE SET last_seen_at = (now() AT TIME ZONE 'UTC')",
                  (document_id, page_id, source_url))
        for entry in models if manufacturer_id is not None else ():  # a model belongs to a manufacturer
            model_id = save_model(conn, manufacturer_id, entry["model_number"], is_base_model=bool(entry.get("is_base_model")))
            if tags.get("trade") or tags.get("equipment_type"):
                conn_exec(conn, "UPDATE models SET trade = COALESCE(trade, %s), equipment_type = COALESCE(equipment_type, %s) "
                                "WHERE id = %s", (tags.get("trade"), tags.get("equipment_type"), model_id))
            conn_exec(conn, "INSERT INTO document_models (document_id, model_id, model_key, manufacturer_id) "
                            "VALUES (%s, %s, %s, %s) ON CONFLICT (document_id, model_id) DO NOTHING",
                      (document_id, model_id, model_key(entry["model_number"])[:190], manufacturer_id))
    except Exception:
        conn_exec(conn, "ROLLBACK TO SAVEPOINT save_document")
        raise
    conn_exec(conn, "RELEASE SAVEPOINT save_document")
    return document_id
