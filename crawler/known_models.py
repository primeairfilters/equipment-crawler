"""Free model links: an install or service manual with no model numbers gets its brand's known model numbers that are
printed in its text. No AI.

Prod 2026-11-21: 840 of 2,638 manuals over 20 pages had no model linked; in a sample of 40 with a text layer, 30
printed a known model number of their own brand (25 in the first 20 pages, the text the AI was sent). A manual with no
model cannot be tied to a unit, and the missing-manual search pays to look for it again.

A number counts only when its key (letters and digits) is MIN_KEY_LENGTH or longer and has both letters and digits,
so phone numbers, part counts and short series codes are never matched. Each manual is read once, then again after
RECHECK_AFTER (new known models arrive from ENERGY STAR and other documents). Nothing here commits.
"""
import re
from datetime import datetime, timedelta, timezone

from crawler.store import conn_exec, model_key

DOC_TYPES = ("install_manual", "service_manual")
MIN_KEY_LENGTH = 6
RECHECK_AFTER = timedelta(days=30)
MAX_LINKS = 200  # a manual naming more is a catalog; the first 200 found are plenty to tie it to its units
_LETTER, _DIGIT = re.compile(r"[A-Z]"), re.compile(r"[0-9]")


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def _alnum(text):
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def link_known_models(conn, *, now=None):
    """Link known models to the manuals that print them. Returns how many links were made."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    due = _rows(conn, "SELECT d.id, d.manufacturer_id FROM documents d WHERE d.doc_type = ANY (%s) "
                      "AND d.hidden_at IS NULL AND d.manufacturer_id IS NOT NULL "
                      "AND (d.models_checked_at IS NULL OR d.models_checked_at <= %s) "
                      "AND NOT EXISTS (SELECT 1 FROM document_models dm WHERE dm.document_id = d.id) ORDER BY d.id",
                (list(DOC_TYPES), now - RECHECK_AFTER))
    keys_by_maker, made = {}, 0
    for doc_id, maker in due:
        if maker not in keys_by_maker:
            keys_by_maker[maker] = [(model_id, key) for model_id, key in _rows(
                conn, "SELECT id, model_key FROM models WHERE manufacturer_id = %s AND hidden_at IS NULL "
                      "AND length(model_key) >= %s", (maker, MIN_KEY_LENGTH))
                if _LETTER.search(key) and _DIGIT.search(key)]
        text = _alnum(" ".join(r[0] for r in _rows(
            conn, "SELECT t.body FROM page_text t WHERE t.page_id = (SELECT du.page_id FROM document_urls du "
                  "WHERE du.document_id = %s ORDER BY du.page_id LIMIT 1) ORDER BY t.page_number", (doc_id,))))
        found = [(model_id, key) for model_id, key in keys_by_maker[maker] if key in text][:MAX_LINKS]
        for model_id, key in found:
            conn_exec(conn, "INSERT INTO document_models (document_id, model_id, model_key, manufacturer_id) "
                            "VALUES (%s, %s, %s, %s) ON CONFLICT (document_id, model_id) DO NOTHING",
                      (doc_id, model_id, model_key(key)[:190], maker))
        made += len(found)
        conn_exec(conn, "UPDATE documents SET models_checked_at = %s WHERE id = %s", (now, doc_id))
    return made


# Sister brands sell the same unit under the same model number (Rheem/Ruud, Carrier/Bryant/Payne, Trane/American
# Standard). 8+ characters, as on prod every pair that long was a real family; shorter ones can be two unrelated
# brands sharing a number by chance (2026-11-21).
SISTER_MIN_KEY_LENGTH = 8
# A 6-7 character match counts only between two brands already proven a family by FAMILY_PROOF long matches: on prod
# those were Ruud/Rheem, Carrier/Bryant, Trane/American Standard, Daikin/Goodman, and the ones outside were chance
# pairs like Bosch/Bryant (2026-11-21).
SISTER_SHORT_KEY_LENGTH = 6
FAMILY_PROOF = 5


def link_sister_brand_models(conn):
    """A manual's model that a sister brand also sells (the same model key under another manufacturer) is linked under
    that brand too, and the brand becomes one of the manual's (document_brands), so Breeze's model and brand lookups
    for a Payne unit find the Bryant manual that prints its number, and gap-fill stops counting it uncovered. One
    statement, safe to run every night. Returns how many model links were made."""
    made = _rows(conn, "INSERT INTO document_models (document_id, model_id, model_key, manufacturer_id) "
                       "SELECT DISTINCT dm.document_id, m.id, m.model_key, m.manufacturer_id FROM document_models dm "
                       "JOIN models m ON m.model_key = dm.model_key AND m.manufacturer_id <> dm.manufacturer_id "
                       "AND m.hidden_at IS NULL WHERE length(dm.model_key) >= %s AND dm.model_key ~ '[A-Z]' "
                       "AND dm.model_key ~ '[0-9]' ON CONFLICT (document_id, model_id) DO NOTHING RETURNING 1",
                 (SISTER_MIN_KEY_LENGTH,))
    made += _rows(conn, "WITH family AS (SELECT d.manufacturer_id AS a, dm.manufacturer_id AS b FROM document_models dm "
                        "JOIN documents d ON d.id = dm.document_id WHERE dm.manufacturer_id <> d.manufacturer_id "
                        "AND length(dm.model_key) >= %s GROUP BY 1, 2 HAVING COUNT(*) >= %s) "
                        "INSERT INTO document_models (document_id, model_id, model_key, manufacturer_id) "
                        "SELECT DISTINCT dm.document_id, m.id, m.model_key, m.manufacturer_id FROM document_models dm "
                        "JOIN models m ON m.model_key = dm.model_key AND m.manufacturer_id <> dm.manufacturer_id "
                        "AND m.hidden_at IS NULL JOIN family f ON f.a = dm.manufacturer_id AND f.b = m.manufacturer_id "
                        "WHERE length(dm.model_key) >= %s AND length(dm.model_key) < %s AND dm.model_key ~ '[A-Z]' "
                        "AND dm.model_key ~ '[0-9]' ON CONFLICT (document_id, model_id) DO NOTHING RETURNING 1",
                  (SISTER_MIN_KEY_LENGTH, FAMILY_PROOF, SISTER_SHORT_KEY_LENGTH, SISTER_MIN_KEY_LENGTH))
    conn_exec(conn, "INSERT INTO document_brands (document_id, manufacturer_id) SELECT DISTINCT dm.document_id, "
                    "dm.manufacturer_id FROM document_models dm JOIN documents d ON d.id = dm.document_id "
                    "WHERE dm.manufacturer_id IS DISTINCT FROM d.manufacturer_id ON CONFLICT DO NOTHING")
    return len(made)
