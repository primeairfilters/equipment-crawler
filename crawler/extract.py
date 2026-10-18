"""Extraction: free filter -> AI reads (Batch API, one document per request group) -> rules verify -> store.

A pass first collects batches that have finished, then submits new ones. A document is read once
per cleaned text and extractor version, under any brand. Nothing here deletes anything.
"""
import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from crawler import ai
from crawler.brands import UnknownTarget, find_brand
from crawler.store import model_key, save_document
from crawler.verify import SPEC_KEYS, verify

EXTRACTOR_VERSION = "1"
MAX_PAGES_SENT = 20
PROMPT_TOKEN_LIMIT = 100_000
SPLIT_TARGET_TOKENS = 95_000
DEFAULT_MAX_TOKENS = 16000
DEFAULT_CAP_USD = "25"
RETRY_AFTER = timedelta(days=1)

SYSTEM = (
    "You read HVAC equipment documents (spec sheets, data tables, model decoders) and report facts exactly as printed.\n"
    "Rules:\n"
    "- Report only facts that are printed in the text you are given. Never guess, never calculate, never use outside knowledge.\n"
    "- A model is a model number printed in the text. Copy it exactly. If a table uses a partial model number, report the "
    "partial model number as printed and set is_base_model to true.\n"
    "- Each fact is one key from the fixed list below, the value exactly as printed (digits only for numbers), and the unit "
    "as printed or null.\n"
    "- If a fact is not in the text, leave it out.\n"
    "- Tables arrive as tab-separated rows, one row per line; a column belongs to the model named at the top of that column.\n"
    "- Answer with JSON only, in the requested format.\n"
    "Keys: " + ", ".join(SPEC_KEYS) + ".\n"
)

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"models": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "model_number": {"type": "string"},
            "is_base_model": {"type": "boolean"},
            "specs": {"type": "array", "items": {
                "type": "object",
                "properties": {"key": {"type": "string", "enum": list(SPEC_KEYS)},
                               "value": {"type": "string"},
                               "unit": {"type": ["string", "null"]}},
                "required": ["key", "value", "unit"], "additionalProperties": False}},
        },
        "required": ["model_number", "is_base_model", "specs"], "additionalProperties": False}}},
    "required": ["models"], "additionalProperties": False,
}

_LABELS = re.compile(
    r"\b(seer2?|eer2?|hspf2?|cop|afue|tonnage|tons?|btuh?|btu/h|refrigerant|charge|mca|mocp|fla|rla|lra|voltage|volts?|"
    r"phase|weight|cfm|airflow|capacity|dba|amps?)\b", re.I)
_MODEL_LIKE = re.compile(r"(?<![A-Z0-9-])(?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{6,}(?![A-Z0-9-])")


def select_pages(pages):
    """Indexes of the pages worth sending: none unless one page has a spec label AND a model-like string."""
    if not any(_LABELS.search(p) and _MODEL_LIKE.search(p) for p in pages):
        return []
    return [i for i, p in enumerate(pages) if _MODEL_LIKE.search(p)][:MAX_PAGES_SENT]


def doc_sha(pages, chosen):
    """The hash of the cleaned text that would be sent."""
    return hashlib.sha256("\n\f\n".join(pages[i] for i in chosen).encode("utf-8")).hexdigest()


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def _exec(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _texts(conn, page_id, page_count):
    sql = "SELECT page_number, body, title FROM page_text WHERE page_id = %s"
    args = [page_id]
    if page_count is not None:
        sql += " AND page_number <= %s"
        args.append(page_count)
    rows = _rows(conn, sql + " ORDER BY page_number", args)
    return [r[1] for r in rows], (rows[0][2] if rows else None)


def _marked(number, text):
    return f"=== page {number} ===\n{text}"


def _clear_batch(conn, page_id, status, retry_at=None, keep_pending=False, **extra):
    sets = ["extraction_status = %s", "ai_batch_id = NULL", "ai_parts = NULL", "ai_reserved_usd = 0", "ai_retry_at = %s"]
    if not keep_pending:  # a paid failure keeps the hash it failed on, so an unchanged page stays failed
        sets.append("ai_pending_sha = NULL")
    args = [status, retry_at]
    for name, value in extra.items():
        sets.append(f"{name} = %s")
        args.append(value)
    _exec(conn, f"UPDATE pages SET {', '.join(sets)} WHERE id = %s", (*args, page_id))


# ---------------------------------------------------------------- collecting finished batches

def _log_call(conn, brand_id, page_id, model, result, now):
    rejected = result.output is None
    _exec(conn,
          "INSERT INTO ai_calls (purpose, brand_id, page_id, model, input_tokens, output_tokens, cache_read_tokens, "
          "cache_write_tokens, searches, is_batch, rejected, cost_usd, created_at) "
          "VALUES ('extract', %s, %s, %s, %s, %s, %s, %s, 0, 1, %s, %s, %s)",
          (brand_id, page_id, model, result.input_tokens, result.output_tokens, result.cache_read_tokens,
           result.cache_write_tokens, 1 if rejected else 0,
           ai.cost(model, result.input_tokens, result.output_tokens, batch=True, check_long=True), now))
    return rejected


def _write_found(conn, page_id, brand_id, checks, extracted_by):
    models = {}
    for c in checks:
        key = model_key(c.model_number)[:190]
        with conn.cursor() as cur:
            cur.execute("INSERT INTO found_models (page_id, brand_id, model_number, model_key) VALUES (%s, %s, %s, %s) "
                        "ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)",
                        (page_id, brand_id, c.model_number[:190], key))
            found_id = cur.lastrowid
        models[key] = found_id
        value = (c.value or "")[:500]
        vkey = hashlib.sha256(" ".join(value.split()).lower().encode("utf-8")).hexdigest()
        number = value if re.fullmatch(r"-?\d{1,13}(\.\d{1,4})?", value) else None
        _exec(conn,
              "INSERT INTO found_specs (found_model_id, spec_key, value_text, value_num, unit, value_key, check_status, "
              "fail_reason, extracted_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
              "ON DUPLICATE KEY UPDATE check_status = VALUES(check_status), fail_reason = VALUES(fail_reason), "
              "extracted_by = VALUES(extracted_by)",
              (found_id, c.key[:100], value, number, (c.unit or None) and str(c.unit)[:30], vkey,
               "passed" if c.ok else "failed", c.reason, extracted_by))


def _finish_page(conn, page, results, now):
    page_id, brand_id, url, content_sha, page_count, pending, parts, model = page
    extracted_by = f"ai:{model}"
    retry = now + RETRY_AFTER
    rejected = [_log_call(conn, brand_id, page_id, model, r, now) for r in results if not r.errored]
    if len(results) < (parts or 0) or any(r.errored for r in results):
        _clear_batch(conn, page_id, "waiting")  # a failed or expired request is not billed; send it again
        return False
    if any(rejected):
        # paid for and unusable: read again only when the text changes or the extractor version does
        _clear_batch(conn, page_id, "failed", None, True, ai_extractor_version=EXTRACTOR_VERSION)
        return False
    texts, title = _texts(conn, page_id, page_count)
    chosen = select_pages(texts)
    if not chosen or doc_sha(texts, chosen) != pending:
        _clear_batch(conn, page_id, "waiting")  # the text changed while the batch ran; read it again
        return False
    sent = [texts[i] for i in chosen]

    merged, order = {}, []
    for r in results:
        for entry in r.output.get("models", []):
            key = model_key(str(entry.get("model_number") or ""))
            if key not in merged:
                merged[key] = {"model_number": str(entry.get("model_number") or ""),
                               "is_base_model": bool(entry.get("is_base_model")), "specs": []}
                order.append(key)
            merged[key]["specs"].extend(entry.get("specs", []))
    answer = {"models": [merged[k] for k in order]}
    checks = verify(answer, sent)
    _write_found(conn, page_id, brand_id, checks, extracted_by)

    good = {}
    for c in checks:
        if c.ok:
            good.setdefault(model_key(c.model_number), {"model_number": c.model_number[:190],
                                                         "is_base_model": merged[model_key(c.model_number)]["is_base_model"],
                                                         "specs": []})["specs"].append(
                {"key": c.key, "value": c.value, "unit": c.unit})
    if good:
        try:
            save_document(conn, brand_id=brand_id, source_url=url, title=title, content_sha=content_sha,
                          models=list(good.values()), extracted_by=extracted_by)
        except Exception:  # the document's write was rolled back as a whole; try it again tomorrow
            _clear_batch(conn, page_id, "failed", None, True, ai_extractor_version=EXTRACTOR_VERSION)
            return False
    _clear_batch(conn, page_id, "extracted", ai_extracted_sha=pending, ai_extractor_version=EXTRACTOR_VERSION)
    return True


def _collect(conn, client, brand_id, now, after_document):
    done = 0
    batches = _rows(conn, "SELECT DISTINCT ai_batch_id FROM pages WHERE ai_batch_id IS NOT NULL AND ai_batch_id <> 'submitting'"
                          + (" AND brand_id = %s" if brand_id else ""), (brand_id,) if brand_id else ())
    for (batch_id,) in batches:
        try:
            results = client.batch_results(batch_id)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:  # the batch is gone; those documents are tried again later
                for (page_id,) in _rows(conn, "SELECT id FROM pages WHERE ai_batch_id = %s", (batch_id,)):
                    _clear_batch(conn, page_id, "failed", now + RETRY_AFTER)
            continue
        if results is None:
            continue
        by_page = {}
        for r in results:
            match = re.fullmatch(r"p(\d+)-\d+", r.custom_id)
            if match:
                by_page.setdefault(int(match.group(1)), []).append(r)
        pages = _rows(conn, "SELECT id, brand_id, url, content_sha, page_count, ai_pending_sha, ai_parts, ai_model "
                            "FROM pages WHERE ai_batch_id = %s", (batch_id,))
        for page in pages:
            if _finish_page(conn, page, by_page.get(page[0], []), now):
                done += 1
            if after_document:
                after_document()
    return done


# ---------------------------------------------------------------- submitting new work

def _plan(client, model, page_texts, chosen, max_tokens):
    """Request texts and token counts for one document: one request, or several if it is over the limit."""
    marked = [_marked(i + 1, page_texts[i]) for i in chosen]
    whole = "\n\n".join(marked)
    tokens = client.count_tokens(model, SYSTEM, whole)
    if tokens <= SPLIT_TARGET_TOKENS:
        return [(whole, tokens)]
    counts = [client.count_tokens(model, SYSTEM, m) for m in marked]
    if any(c > SPLIT_TARGET_TOKENS for c in counts):
        return None
    groups, current, total = [], [], 0
    for text, c in zip(marked, counts):
        if current and total + c > SPLIT_TARGET_TOKENS:
            groups.append(("\n\n".join(current), total))
            current, total = [], 0
        current.append(text)
        total += c
    groups.append(("\n\n".join(current), total))
    return groups


def extract_pass(conn, client, *, brand=None, model=ai.HAIKU, cap_usd=None, now=None, max_tokens=DEFAULT_MAX_TOKENS,
                 after_document=None):
    ai.cost(model, input_tokens=1)  # an unknown model is an error before anything else happens
    now = now or _utcnow()
    cap = Decimal(str(cap_usd if cap_usd is not None else os.environ.get("AI_MONTHLY_CAP_USD", DEFAULT_CAP_USD)))
    brand_id = None
    if brand is not None:
        brand_id = find_brand(conn, brand)
        if brand_id is None:
            raise UnknownTarget(f"No brand called {brand}.")
    summary = {"collected": 0, "submitted": 0, "held": 0, "already_read": 0, "skipped": 0}
    if not _rows(conn, "SELECT GET_LOCK('crawler-extract', 0)")[0][0]:
        raise RuntimeError("Another extract is already running.")
    try:
        return _extract(conn, client, brand_id, model, cap, now, max_tokens, after_document, summary)
    finally:
        _rows(conn, "SELECT RELEASE_LOCK('crawler-extract')")


def _extract(conn, client, brand_id, model, cap, now, max_tokens, after_document, summary):
    summary["collected"] = _collect(conn, client, brand_id, now, after_document)

    eligible = _rows(
        conn,
        "SELECT p.id, p.brand_id, p.page_count FROM pages p WHERE p.ai_batch_id IS NULL "
        "AND (p.extraction_status = 'waiting' "
        "  OR (p.extraction_status = 'failed' AND ((p.ai_retry_at IS NOT NULL AND p.ai_retry_at <= %s) "
        "    OR (p.ai_retry_at IS NULL AND p.ai_extractor_version IS NOT NULL AND p.ai_extractor_version <> %s))) "
        "  OR (p.extraction_status = 'extracted' AND (p.ai_extractor_version IS NULL OR p.ai_extractor_version <> %s))) "
        "AND EXISTS (SELECT 1 FROM page_text t WHERE t.page_id = p.id)"
        + (" AND p.brand_id = %s" if brand_id else "") + " ORDER BY p.id",
        (now, EXTRACTOR_VERSION, EXTRACTOR_VERSION, *((brand_id,) if brand_id else ())))

    spent = ai.monthly_spend(conn, now)
    in_flight = Decimal(_rows(conn, "SELECT COALESCE(SUM(ai_reserved_usd), 0) FROM pages WHERE ai_batch_id IS NOT NULL")[0][0])
    room = cap - spent - in_flight
    requests, admitted, taken = [], [], set()

    for page_id, page_brand, page_count in eligible:
        texts, _ = _texts(conn, page_id, page_count)
        chosen = select_pages(texts)
        if not chosen:
            _exec(conn, "UPDATE pages SET extraction_status = 'skipped' WHERE id = %s", (page_id,))
            summary["skipped"] += 1
            continue
        sha = doc_sha(texts, chosen)
        if _rows(conn, "SELECT 1 FROM pages WHERE ai_extracted_sha = %s AND ai_extractor_version = %s LIMIT 1",
                 (sha, EXTRACTOR_VERSION)):
            _exec(conn, "UPDATE pages SET extraction_status = 'extracted', ai_extracted_sha = %s, "
                        "ai_extractor_version = %s, ai_retry_at = NULL WHERE id = %s", (sha, EXTRACTOR_VERSION, page_id))
            summary["already_read"] += 1
            continue
        if sha in taken or _rows(
                conn, "SELECT 1 FROM pages WHERE ai_pending_sha = %s AND (ai_batch_id IS NOT NULL OR "
                      "(extraction_status = 'failed' AND ai_retry_at IS NULL AND ai_extractor_version = %s)) LIMIT 1",
                (sha, EXTRACTOR_VERSION)):
            summary["held"] += 1  # the same text is already on its way; the next pass finds it read
            continue
        plan = _plan(client, model, texts, chosen, max_tokens)
        if plan is None:
            _exec(conn, "UPDATE pages SET extraction_status = 'skipped' WHERE id = %s", (page_id,))
            summary["skipped"] += 1
            continue
        reserve = sum(ai.cost(model, input_tokens=tokens, output_tokens=max_tokens, check_long=True) for _, tokens in plan)
        if reserve > room:
            summary["held"] += 1
            continue
        room -= reserve
        taken.add(sha)
        for part, (text, _) in enumerate(plan):
            requests.append(ai.Request(f"p{page_id}-{part}", model, SYSTEM, text, max_tokens))
        admitted.append((page_id, sha, len(plan), reserve))

    if requests:
        # mark the documents before sending, so a crash right after the send cannot make a second send
        for page_id, sha, parts, reserve in admitted:
            _exec(conn, "UPDATE pages SET ai_batch_id = 'submitting', ai_reserved_usd = %s WHERE id = %s",
                  (reserve, page_id))
        if after_document:
            after_document()
        try:
            batch_id = client.submit_batch(requests)
        except Exception:
            for page_id, sha, parts, reserve in admitted:
                _exec(conn, "UPDATE pages SET ai_batch_id = NULL, ai_reserved_usd = 0 WHERE id = %s", (page_id,))
            raise
        for page_id, sha, parts, reserve in admitted:
            _exec(conn, "UPDATE pages SET ai_batch_id = %s, ai_pending_sha = %s, ai_parts = %s, ai_model = %s, "
                        "ai_reserved_usd = %s, ai_retry_at = NULL WHERE id = %s",
                  (batch_id, sha, parts, model, reserve, page_id))
            if after_document:
                after_document()
        summary["submitted"] = len(admitted)
    return summary
