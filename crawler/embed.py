"""P13 (D42, D52): vectors for live sections, for Breeze's meaning search.

- One table per embedding model and size: text-embedding-3-small at 1536 dimensions (Breeze's), as halfvec.
- The text embedded is a one-line header and the section: "Ruud · Install manual · <title> · <path> · p.23", so
  "Ruud subcooling" finds a section that never says "Ruud" (like Breeze's EmbedHelpDocs).
- Never paid twice: a section whose exact text already has a vector (a rebuild that changed nothing) copies it.
- Direct calls to the embeddings API (user, 2026-10-27), up to MAX_INPUTS_PER_REQUEST sections or
  MAX_TOKENS_PER_REQUEST tokens a request; each request is logged in ai_calls and counted in the same monthly cap as
  the Anthropic spend, checked before it is sent.

The key is read from the environment only (OPENAI_API_KEY_CRAWLER) and is never printed or logged. Callers commit;
after_request runs after each request's rows are written.
"""
import hashlib
import os
from datetime import datetime, timezone
from decimal import Decimal

from crawler import ai

MODEL = "text-embedding-3-small"
DIMS = 1536
TABLE = "section_vectors_te3s_1536"
MAX_INPUTS_PER_REQUEST = 100
MAX_TOKENS_PER_REQUEST = 60_000
MAX_INPUT_CHARS = 20_000  # the model reads at most 8,191 tokens; a dense table runs about 2.5 characters a token
DEFAULT_CAP_USD = "25"


class EmbedError(Exception):
    pass


class OpenAIEmbedder:
    """embed(texts) -> (vectors in the same order, billed input tokens)."""
    model = MODEL
    dims = DIMS
    URL = "https://api.openai.com/v1/embeddings"

    def __init__(self, api_key=None):
        self._key = api_key or os.environ.get("OPENAI_API_KEY_CRAWLER")
        if not self._key:
            raise RuntimeError("OPENAI_API_KEY_CRAWLER is not set.")

    def embed(self, texts):
        import httpx
        try:
            reply = httpx.post(self.URL, timeout=120,
                               headers={"Authorization": f"Bearer {self._key}"},
                               json={"model": self.model, "input": list(texts), "dimensions": self.dims,
                                     "encoding_format": "float"})
        except httpx.HTTPError as exc:
            raise EmbedError(f"the embeddings API could not be reached ({type(exc).__name__})") from exc
        if reply.status_code != 200:
            raise EmbedError(f"the embeddings API answered {reply.status_code}")
        body = reply.json()
        data = sorted(body["data"], key=lambda d: d["index"])
        if len(data) != len(texts) or any(len(d["embedding"]) != self.dims for d in data):
            raise EmbedError("the embeddings API gave an unusable answer")
        return [d["embedding"] for d in data], int(body["usage"]["prompt_tokens"])


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []


def _label(doc_type):
    return (doc_type or "document").replace("_", " ").capitalize()


def embed_text(manufacturer, doc_type, title, path, page_from, page_to, body):
    pages = f"p.{page_from}" if page_from == page_to else f"p.{page_from}–{page_to}"
    header = " · ".join(part for part in (manufacturer, _label(doc_type), title, path, pages) if part)
    return (header + "\n" + body)[:MAX_INPUT_CHARS]


def _estimate(text):
    return len(text) // 2 + 1  # on the high side: the cap is checked against this before a request is sent


def _vector_literal(vector):
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


def embed_pass(conn, client, *, brand_id=None, cap_usd=None, now=None, after_request=None):
    """Give every live section without a vector one. Returns {"embedded", "reused", "requests", "tokens", "cost",
    "capped", "error"}."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cap = Decimal(str(cap_usd if cap_usd is not None else os.environ.get("AI_MONTHLY_CAP_USD", DEFAULT_CAP_USD)))
    todo = _rows(conn,
                 "SELECT s.id, m.name, s.doc_type, d.title, s.heading_path, s.page_from, s.page_to, s.body "
                 "FROM sections s JOIN documents d ON d.id = s.document_id "
                 "LEFT JOIN manufacturers m ON m.id = s.manufacturer_id "
                 f"WHERE s.is_live AND NOT EXISTS (SELECT 1 FROM {TABLE} v WHERE v.section_id = s.id) "
                 + ("AND EXISTS (SELECT 1 FROM document_urls du JOIN pages p ON p.id = du.page_id "
                    "WHERE du.document_id = s.document_id AND p.brand_id = %s) " if brand_id else "")
                 + "ORDER BY s.id", (brand_id,) if brand_id else ())
    summary = {"embedded": 0, "reused": 0, "requests": 0, "tokens": 0, "cost": Decimal(0), "capped": False,
               "error": None}
    fresh = []  # (section id, text, sha) still needing a call
    for section_id, maker, doc_type, title, path, page_from, page_to, body in todo:
        text = embed_text(maker, doc_type, title, path, page_from, page_to, body)
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        copied = _rows(conn, f"INSERT INTO {TABLE} (section_id, embed_sha, embedding, is_live, manufacturer_id, trade, "
                             "equipment_type, doc_type, language) SELECT s.id, v.embed_sha, v.embedding, s.is_live, "
                             "s.manufacturer_id, s.trade, s.equipment_type, s.doc_type, s.language "
                             f"FROM sections s, (SELECT embed_sha, embedding FROM {TABLE} WHERE embed_sha = %s LIMIT 1) v "
                             "WHERE s.id = %s RETURNING 1", (sha, section_id))
        if copied:
            summary["reused"] += 1
        else:
            fresh.append((section_id, text, sha))

    groups, group, tokens = [], [], 0
    for item in fresh:
        size = _estimate(item[1])
        if group and (len(group) >= MAX_INPUTS_PER_REQUEST or tokens + size > MAX_TOKENS_PER_REQUEST):
            groups.append(group)
            group, tokens = [], 0
        group.append(item)
        tokens += size
    if group:
        groups.append(group)

    for group in groups:
        worst = ai.cost(MODEL, sum(_estimate(text) for _, text, _ in group))
        if ai.monthly_spend(conn, now) + worst > cap:
            summary["capped"] = True
            break
        try:
            found, billed = client.embed([text for _, text, _ in group])
        except EmbedError as exc:
            summary["error"] = str(exc)
            break
        spent = ai.cost(MODEL, billed)
        _rows(conn, "INSERT INTO ai_calls (purpose, model, input_tokens, cost_usd) VALUES ('embed', %s, %s, %s)",
              (MODEL, billed, spent))
        for (section_id, _, sha), vector in zip(group, found):
            _rows(conn, f"INSERT INTO {TABLE} (section_id, embed_sha, embedding, is_live, manufacturer_id, trade, "
                        "equipment_type, doc_type, language) SELECT id, %s, %s::halfvec, is_live, manufacturer_id, "
                        "trade, equipment_type, doc_type, language FROM sections WHERE id = %s",
                  (sha, _vector_literal(vector), section_id))
        summary["embedded"] += len(group)
        summary["requests"] += 1
        summary["tokens"] += billed
        summary["cost"] += spent
        if after_request:
            after_request()
    return summary
