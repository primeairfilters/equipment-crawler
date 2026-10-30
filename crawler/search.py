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
import re
from dataclasses import dataclass, replace
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
EF_SEARCH = 8 * DEPTH  # HNSW candidates; at 400 the meaning list matched an exact search on all 25 eval questions
MAX_DISTANCE = 0.8  # a meaning match further than this is noise, not a match
WIDEN_DISTANCE = 0.6  # to be tuned with ./crawl eval
TOP = 8
PREFER_MODEL = 2.0  # D82: the model's own manuals and its series manuals, then
PREFER_BRAND = 1.5  # the brand's (and its sister brands' shared) manuals, then everything else at 1
FOREIGN_CODE = 0.3  # another brand's section matched through an error or part code in a brand question
LANGUAGE = "en"  # Breeze's techs ask in English
EQUIPMENT_BOOST = 1.5  # a section of the equipment the question names: its merged score times this (a boost, not a
# filter: AI tags are not always right, doc 80 is a ductless manual tagged heat_pump). Ruud eval 2026-10-29: none 16/25,
# 1.25 and 1.5 19/25 (1.5 a little higher MRR), 2.0 18/25 (a SEER2 answer in a guide tagged 'other' fell out)
# The words a tech uses for each equipment type (documents.equipment_type), longest first: a phrase is taken out of the
# question once matched, so "heat pump water heater" is not also "heat pump" plus "water heater", and "ductless heat
# pump" is a mini split, not every heat pump. Breeze copies this list with search.sql.
EQUIPMENT_WORDS = [
    (r"heat pump water heaters?|hybrid water heaters?|hpwh", ["heat_pump", "water_heater"]),
    # the other Tracforce trades (D83); multi-word names first, and no bare "range", "panel" or "filter" (a pressure
    # range, an access panel and an air filter are HVAC words)
    (r"salt (water )?(chlorine )?generators?|salt (cells?|systems?)|(salt )?chlorinators?", ["pool_chlorinator"]),
    (r"pool (and spa )?heaters?|spa heaters?", ["pool_heater"]),
    (r"pool pumps?|variable speed pool pumps?", ["pool_pump"]),
    (r"pool filters?|sand filters?|cartridge filters?", ["pool_filter"]),
    (r"pool cleaners?|robotic cleaners?", ["pool_cleaner"]),
    (r"hot tubs?|spas?", ["spa_hot_tub"]),
    (r"(automatic )?transfer switch(es)?", ["transfer_switch"]),
    (r"(standby |portable |home )?generators?|gensets?", ["generator"]),
    (r"garage door openers?|door openers?", ["garage_door_opener"]),
    (r"garage doors?", ["garage_door"]),
    (r"water softeners?|softeners?", ["water_softener"]),
    (r"reverse osmosis|ro systems?", ["reverse_osmosis"]),
    (r"(whole house )?water filters?", ["water_filter"]),
    (r"ice machines?|ice makers?", ["ice_machine"]),
    (r"walk[- ]ins?( coolers?| freezers?| boxes?)?", ["walk_in"]),
    (r"reach[- ]ins?( coolers?| freezers?| refrigerators?)?", ["reach_in"]),
    (r"range hoods?|vent hoods?", ["range_hood"]),
    (r"ovens?|stoves?|(gas|electric|induction) ranges?", ["range_oven"]),
    (r"cooktops?", ["cooktop"]),
    (r"microwaves?", ["microwave"]),
    (r"dishwashers?", ["dishwasher"]),
    (r"washers?|washing machines?", ["washer"]),
    (r"dryers?", ["dryer"]),
    (r"refrigerators?|fridges?", ["refrigerator"]),
    (r"freezers?", ["freezer"]),
    (r"(electrical|main|breaker) panels?|breaker box(es)?|load centers?|(circuit )?breakers?", ["panel_breaker"]),
    (r"ev chargers?|evse|car chargers?", ["ev_charger"]),
    (r"surge protect(ors?|ion)", ["surge_protection"]),
    (r"garbage dispos(ers?|als?)", ["garbage_disposer"]),
    (r"sump pumps?|sewage pumps?|ejector pumps?", ["sump_sewage_pump"]),
    (r"well pumps?", ["well_pump"]),
    (r"toilets?", ["toilet"]),
    (r"faucets?", ["fixture_faucet"]),
    (r"backflow( preventers?)?", ["backflow_valve"]),
    (r"ductless( heat pumps?| splits?)?|mini[- ]?splits?|wall[- ]mount(ed)? (units?|splits?)", ["ductless_mini_split"]),
    (r"(tankless )?water heaters?|tankless", ["water_heater"]),
    (r"rooftop( units?)?|rtus?|packaged units?", ["packaged_unit"]),
    (r"air handlers?", ["air_handler"]),
    (r"heat pumps?", ["heat_pump"]),
    (r"air conditioners?|split ac|ac units?|a/c", ["air_conditioner"]),
    (r"furnaces?", ["furnace"]),
    (r"boilers?", ["boiler"]),
    (r"thermostats?|controllers?", ["thermostat_control"]),
]


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


def brand_documents(conn, manufacturer_id):
    """Every document that belongs to the manufacturer: its own and those its addresses also serve (Q23)."""
    return [r[0] for r in _rows(conn, "SELECT document_id FROM document_brands WHERE manufacturer_id = %s UNION "
                                      "SELECT id FROM documents WHERE manufacturer_id = %s ORDER BY 1",
                                (manufacturer_id, manufacturer_id))]


def equipment_types(question):
    """The equipment types the question names (EQUIPMENT_WORDS), sorted; [] when it names none."""
    text, found = " " + re.sub(r"\s+", " ", question.lower()) + " ", set()
    for pattern, types in EQUIPMENT_WORDS:
        text, n = re.subn(rf"(?<![\w-])({pattern})(?![\w-])", " ", text)
        if n:
            found.update(types)
    return sorted(found)


def search(conn, question, vector, documents=None, manufacturer_id=None, top=TOP, language=None, model=None):
    """model: the model the documents were narrowed by. Inside its own manuals its number (or a prefix of it, or a
    longer form) names nearly every section, so it is not a code match there."""
    codes = terms(question)
    if model:
        own = model_key(model)
        codes = {c for c in codes if not (own.startswith(c) or c.startswith(own))}
    # the HNSW index hands back at most hnsw.ef_search rows (default 40) before the filters, fewer than DEPTH: widen it
    # and let it keep scanning past filtered rows, for this transaction only (same speed on the eval, 2026-10-28); one
    # transaction around both, so the settings reach the search on an autocommit or pooled connection too
    with conn.transaction():
        _rows(conn, "SELECT set_config('hnsw.ef_search', %s, true), "
                    "set_config('hnsw.iterative_scan', 'strict_order', true)", (str(EF_SEARCH),))
        rows = _rows(conn, SEARCH_SQL, {"question": question, "vector": _vector_literal(vector),
                                        "codes": sorted(codes), "documents": documents,
                                        "manufacturer": manufacturer_id, "language": language, "depth": DEPTH,
                                        "top": top, "max_distance": MAX_DISTANCE,
                                        "equipment": equipment_types(question), "boost": EQUIPMENT_BOOST})
    return [Hit(*r[:11], float(r[11]), r[12], None if r[13] is None else float(r[13]), r[14], r[15]) for r in rows]


def _relevant(hits):
    return any(h.text_rank or h.term_rank or (h.distance is not None and h.distance < WIDEN_DISTANCE) for h in hits)


def answer(conn, question, vector, model=None, manufacturer=None, top=TOP, language=LANGUAGE):
    """D82 (user, 2026-10-29): prefer, never exclude. Every search covers all live data; the model's own manuals (and
    its series manuals) rank first, then the brand's (shared sister-brand manuals included, Q23), then the rest. Each
    preferred set is also searched on its own, so its sections are never crowded out of the candidates by a large
    library. A question with a code (an error code, a part number) is brand-specific: another brand's section that
    matches only through that code is pushed down hard (an E4 means different things to different makers).
    level: the narrowest preference that applied (model, manufacturer, all); widened: a model with no manuals."""
    maker = find_manufacturer(conn, manufacturer) if manufacturer else None
    model_docs = model_documents(conn, model, maker) if model else []
    brand_docs = brand_documents(conn, maker) if maker is not None else []
    widened = ["model"] if model and not model_docs else []
    level = "model" if model_docs else "manufacturer" if maker is not None else "all"
    pools = [docs for docs in (model_docs, brand_docs) if docs] + [None]
    own_model, own_brand, coded = set(model_docs), set(brand_docs), bool(terms(question))
    hits, used = [], language
    for used in ([language, None] if language else [None]):
        best = {}
        for documents in pools:
            for hit in search(conn, question, vector, documents, None, top, used, model if model_docs else None):
                weight = (PREFER_MODEL if hit.document_id in own_model else
                          PREFER_BRAND if hit.document_id in own_brand else 1.0)
                if coded and maker is not None and hit.document_id not in own_brand | own_model and hit.term_rank:
                    weight *= FOREIGN_CODE
                scored = replace(hit, score=hit.score * weight)
                if hit.section_id not in best or scored.score > best[hit.section_id].score:
                    best[hit.section_id] = scored
        hits = sorted(best.values(), key=lambda h: (-h.score, h.section_id))[:top]
        if _relevant(hits):
            break
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
