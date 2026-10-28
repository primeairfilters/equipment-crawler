"""P14 (D43, D44): the search Breeze will run, in Python, so it can be measured before Breeze has it.

- Narrow: with a model, the manuals of that model, of any 5+ character prefix of it (a series manual) and of the
  models it starts (a tech typed part of it); with a manufacturer, its manuals; with neither, everything.
- Search the narrowed live sections three ways (crawler/search.sql) and merge them by reciprocal rank.
- Prefer the question's language (LANGUAGE unless asked otherwise): Ruud prints French and Spanish twins of its
  manuals, which otherwise crowd out the English one. When nothing relevant is in that language, the same level is
  searched in any language before widening, so a manual that exists only in French is still found.
- Widen one level (model → manufacturer → everything) when nothing relevant comes back: no word or code match and no
  section closer in meaning than WIDEN_DISTANCE.

The question is embedded as Breeze will embed it: the plain question, same model and size. Each embedding request is
logged in ai_calls (purpose 'ask') and checked against the monthly cap first. Read only otherwise; callers commit.
"""
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from crawler import ai
from crawler.embed import DEFAULT_CAP_USD, MODEL, _vector_literal
from crawler.sections import terms
from crawler.store import alias_key, model_key

SEARCH_SQL = Path(__file__).with_name("search.sql").read_text()
MIN_PREFIX = 5
DEPTH = 50  # rows each way, before merging
MAX_DISTANCE = 0.8  # a meaning match further than this is noise, not a match
WIDEN_DISTANCE = 0.6  # to be tuned with ./crawl eval
TOP = 8
LANGUAGE = "en"  # Breeze's techs ask in English


class AskCapped(Exception):
    pass


@dataclass
class Hit:
    section_id: int
    document_id: int
    title: str
    doc_type: str
    page_from: int
    page_to: int
    url: str
    heading_path: str
    kind: str
    body: str
    content_sha: str
    score: float
    vector_rank: int
    distance: float
    text_rank: int
    term_rank: int


def _rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else []


def find_manufacturer(conn, name):
    found = _rows(conn, "SELECT manufacturer_id FROM manufacturer_aliases WHERE alias_key = %s", (alias_key(name),))
    return found[0][0] if found else None


def model_documents(conn, model, manufacturer_id=None):
    """Live-or-not documents linked to the model, a 5+ character prefix of it, or a model it starts."""
    key = model_key(model)
    if len(key) < MIN_PREFIX:
        return []
    prefixes = [key[:n] for n in range(len(key), MIN_PREFIX - 1, -1)]
    return [r[0] for r in _rows(
        conn, "SELECT DISTINCT document_id FROM document_models WHERE (model_key = ANY (%s) "
              "OR (model_key >= %s AND model_key < %s)) AND (%s::integer IS NULL OR manufacturer_id = %s::integer) "
              "ORDER BY 1", (prefixes, key, key + "\x7f", manufacturer_id, manufacturer_id))]


def levels(conn, model=None, manufacturer=None):
    """[(level name, documents or None, manufacturer id or None)], narrowest first."""
    maker = find_manufacturer(conn, manufacturer) if manufacturer else None
    out = []
    if model:
        out.append(("model", model_documents(conn, model, maker), None))
    if maker is not None:
        out.append(("manufacturer", None, maker))
    out.append(("all", None, None))
    return out


def search(conn, question, vector, documents=None, manufacturer_id=None, top=TOP, language=None, model=None):
    """model: the model the documents were narrowed by. Inside its own manuals its number (or a prefix of it, or a
    longer form) names nearly every section, so it is not a code match there."""
    codes = terms(question)
    if model:
        own = model_key(model)
        codes = {c for c in codes if not (own.startswith(c) or c.startswith(own))}
    rows = _rows(conn, SEARCH_SQL, {"question": question, "vector": _vector_literal(vector),
                                    "codes": sorted(codes), "documents": documents,
                                    "manufacturer": manufacturer_id, "language": language, "depth": DEPTH, "top": top,
                                    "max_distance": MAX_DISTANCE})
    return [Hit(*r[:11], float(r[11]), r[12], None if r[13] is None else float(r[13]), r[14], r[15]) for r in rows]


def _relevant(hits):
    return any(h.text_rank or h.term_rank or (h.distance is not None and h.distance < WIDEN_DISTANCE) for h in hits)


def answer(conn, question, vector, model=None, manufacturer=None, top=TOP, language=LANGUAGE):
    widened = []
    hits, level, used = [], "all", language
    for level, documents, maker in levels(conn, model, manufacturer):
        if documents == []:
            widened.append(level)
            continue
        for used in ([language, None] if language else [None]):
            hits = search(conn, question, vector, documents, maker, top, used, model if level == "model" else None)
            if _relevant(hits):
                break
        if _relevant(hits) or level == "all":
            break
        widened.append(level)
    return {"level": level, "language": used, "widened": widened, "results": hits}


def _embed(conn, client, texts, purpose, cap_usd, now):
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cap = Decimal(str(cap_usd if cap_usd is not None else os.environ.get("AI_MONTHLY_CAP_USD", DEFAULT_CAP_USD)))
    worst = ai.cost(MODEL, sum(len(t) // 2 + 1 for t in texts))
    if ai.monthly_spend(conn, now) + worst > cap:
        raise AskCapped("the monthly AI cap (AI_MONTHLY_CAP_USD) is reached")
    vectors, billed = client.embed(list(texts))
    _rows(conn, "INSERT INTO ai_calls (purpose, model, input_tokens, cost_usd) VALUES (%s, %s, %s, %s)",
          (purpose, MODEL, billed, ai.cost(MODEL, billed)))
    return vectors


def ask(conn, client, question, *, model=None, manufacturer=None, top=TOP, language=LANGUAGE, cap_usd=None, now=None):
    vector = _embed(conn, client, [question], "ask", cap_usd, now)[0]
    return answer(conn, question, vector, model, manufacturer, top, language)


def _matches(hit, expect):
    pages = expect["pages"]
    return (hit.content_sha.startswith(expect["document"]) and hit.page_from <= pages[-1]
            and hit.page_to >= pages[0])


def evaluate(conn, client, cases, *, top=5, cap_usd=None, now=None):
    """cases: [{"question", "model"?, "manufacturer"?, "language"? (default LANGUAGE), "expect": [{"document": sha prefix, "pages": [from, to]}]}].
    A case's rank is the first result (1-based) from an expected document whose pages overlap; None if not in the top.
    All questions are embedded in one request."""
    vectors = _embed(conn, client, [c["question"] for c in cases], "eval", cap_usd, now)
    out = []
    for case, vector in zip(cases, vectors):
        found = answer(conn, case["question"], vector, case.get("model"), case.get("manufacturer"), top,
                       case.get("language", LANGUAGE))
        rank = next((n for n, hit in enumerate(found["results"], start=1)
                     if any(_matches(hit, e) for e in case["expect"])), None)
        out.append({"question": case["question"], "rank": rank, "level": found["level"], "results": found["results"]})
    total = Decimal(len(out)) or Decimal(1)
    return {"cases": out, "top": top,
            "hit_at_5": Decimal(sum(1 for c in out if c["rank"] and c["rank"] <= 5)) / total,
            "hit_at_1": Decimal(sum(1 for c in out if c["rank"] == 1)) / total,
            "mrr": sum((Decimal(1) / c["rank"] for c in out if c["rank"]), Decimal(0)) / total}
