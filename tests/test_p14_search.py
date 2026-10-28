"""P14 (D43, D44): `./crawl ask` and `./crawl eval`. Narrow by model (or a 5+ character prefix of it), then
manufacturer, then everything; search by meaning (vectors), by words (full text) and by exact codes (section_terms),
merged by reciprocal rank; widen one level when the narrowed search finds too little. A fake embedder, no network."""
import json
import math
import re
from decimal import Decimal

import pytest

from p3support import q
from test_p12_build import document, t, words
from test_p13_embed import live_document


class BagEmbedder:
    """Texts that share words get close vectors (hashed bag of words), so meaning search behaves like a weak real model."""
    model = "text-embedding-3-small"
    dims = 1536

    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        out = []
        for text in texts:
            v = [0.0] * self.dims
            for word in re.findall(r"[a-z]{4,}", text.lower()):
                v[hash(word) % self.dims] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out, sum(len(x) // 4 for x in texts)


def topic(heading, body_word, extra=""):
    return [[t(heading, 14), t(words(320, body_word) + " " + extra)]]


def manual(conn, monkeypatch, pages, brand_name=None, models=(), doc_type="install_manual"):
    doc, mid, name = live_document(conn, monkeypatch, pages=pages, brand_name=brand_name, doc_type=doc_type)
    for key in models:
        model_id = q(conn, "INSERT INTO models (manufacturer_id, model_number, model_key) VALUES (%s, %s, %s) "
                           "ON CONFLICT (manufacturer_id, model_key) DO UPDATE SET model_number = EXCLUDED.model_number "
                           "RETURNING id", (mid, key, key))[0][0]
        q(conn, "INSERT INTO document_models (document_id, model_id, model_key, manufacturer_id) VALUES (%s, %s, %s, %s)",
          (doc, model_id, key, mid))
    return doc, name


@pytest.fixture
def library(runtime_conn, monkeypatch):
    """Two makers. Ruudtest has a series manual (RA15AZ) and a model manual (RA15AZ24AJ3CA); Othertest has one manual."""
    from crawler.embed import embed_pass
    series, maker = manual(runtime_conn, monkeypatch,
                           topic("Subcooling Charging", "subcooling", "Measure liquid line temperature for subcooling."),
                           brand_name="Ruudtest", models=["RA15AZ"])
    model, _ = manual(runtime_conn, monkeypatch,
                      topic("Error Codes", "diagnostic", "Code E4 means the LO PS pressure switch opened."),
                      brand_name="Ruudtest", models=["RA15AZ24AJ3CA"])
    other, _ = manual(runtime_conn, monkeypatch,
                      topic("Subcooling Charging", "subcooling", "Measure liquid line temperature for subcooling."),
                      brand_name="Othertest", models=["XB13"])
    embed_pass(runtime_conn, BagEmbedder(), cap_usd=5)
    return {"series": series, "model": model, "other": other, "maker": maker}


def docs(result):
    return [h.document_id for h in result["results"]]


def test_a_model_narrows_to_its_own_manuals_and_its_series_manual(runtime_conn, library):
    from crawler.search import ask, model_documents
    assert model_documents(runtime_conn, "RA15AZ24AJ3CA") == sorted([library["series"], library["model"]])
    result = ask(runtime_conn, BagEmbedder(), "how do I check subcooling", model="RA15AZ24AJ3CA", cap_usd=5)
    assert result["level"] == "model" and docs(result) == [library["series"]]  # the other maker's twin is left out


def test_a_partial_model_number_finds_the_manuals_of_the_models_it_starts(runtime_conn, library):
    from crawler.search import ask
    result = ask(runtime_conn, BagEmbedder(), "error code list", model="RA15AZ24", cap_usd=5)
    assert result["level"] == "model" and library["model"] in docs(result) and library["other"] not in docs(result)


def test_a_manufacturer_narrows_to_its_manuals(runtime_conn, library):
    from crawler.search import ask
    result = ask(runtime_conn, BagEmbedder(), "how do I check subcooling", manufacturer="Ruudtest", cap_usd=5)
    assert result["level"] == "manufacturer" and library["other"] not in docs(result)


def test_with_neither_everything_is_searched(runtime_conn, library):
    from crawler.search import ask
    result = ask(runtime_conn, BagEmbedder(), "how do I check subcooling", cap_usd=5)
    assert result["level"] == "all" and {library["series"], library["other"]} <= set(docs(result))


def test_an_unknown_model_widens_to_the_manufacturer(runtime_conn, library):
    from crawler.search import ask
    result = ask(runtime_conn, BagEmbedder(), "how do I check subcooling", model="ZZ99999", manufacturer="Ruudtest",
                 cap_usd=5)
    assert result["level"] == "manufacturer" and result["widened"] == ["model"]
    assert library["series"] in docs(result)


def test_full_text_and_codes_find_an_error_code_that_meaning_search_alone_misses(runtime_conn, library):
    from crawler.search import ask
    result = ask(runtime_conn, BagEmbedder(), "E4", cap_usd=5)  # no word of 4+ letters: the fake vector is empty
    top = result["results"][0]
    assert top.document_id == library["model"] and top.term_rank == 1 and top.vector_rank is None


def test_a_section_found_every_way_ranks_first(runtime_conn, library):
    from crawler.search import ask
    result = ask(runtime_conn, BagEmbedder(), "what does code E4 diagnostic pressure switch mean", cap_usd=5)
    top = result["results"][0]
    assert top.document_id == library["model"] and top.vector_rank and top.text_rank and top.term_rank


def test_a_hidden_manual_is_never_returned(runtime_conn, library):
    from crawler.kb import hide
    from crawler.search import ask
    hide(runtime_conn, "document", str(library["model"]), reason="bad scan", by_whom="tester")
    result = ask(runtime_conn, BagEmbedder(), "E4 diagnostic", cap_usd=5)
    assert library["model"] not in docs(result)


def test_a_hit_carries_what_breeze_cites(runtime_conn, library):
    from crawler.search import ask
    hit = ask(runtime_conn, BagEmbedder(), "E4", cap_usd=5)["results"][0]
    assert (hit.title, hit.doc_type, hit.page_from, hit.heading_path) == ("Install manual", "install_manual", 1,
                                                                           "Error Codes")
    assert "LO PS" in hit.body


def test_asking_logs_the_question_embedding_and_the_cap_holds(runtime_conn, library):
    from crawler.search import AskCapped, ask
    ask(runtime_conn, BagEmbedder(), "how do I check subcooling", cap_usd=5)
    assert q(runtime_conn, "SELECT purpose, model FROM ai_calls WHERE purpose = 'ask'") == (
        ("ask", "text-embedding-3-small"),)
    q(runtime_conn, "INSERT INTO ai_calls (purpose, model, cost_usd) VALUES ('tag', 'claude-haiku-5-5', 5)")
    client = BagEmbedder()
    with pytest.raises(AskCapped):
        ask(runtime_conn, client, "how do I check subcooling", cap_usd=5)
    assert client.calls == []


def test_the_search_sql_lives_in_one_file_breeze_can_copy():
    from pathlib import Path
    import crawler.search
    sql = (Path(crawler.search.__file__).with_name("search.sql")).read_text()
    assert "section_vectors_te3s_1536" in sql and "section_terms" in sql and "tsv" in sql
    assert crawler.search.SEARCH_SQL == sql


# ---------------------------------------------------------------- eval

def test_eval_scores_how_often_the_expected_page_is_in_the_top_five(runtime_conn, library, tmp_path):
    from crawler.search import evaluate
    sha = q(runtime_conn, "SELECT content_sha FROM documents WHERE id = %s", (library["model"],))[0][0]
    cases = [{"question": "what does E4 mean", "expect": [{"document": sha[:12], "pages": [1, 1]}]},
             {"question": "what does E4 mean", "expect": [{"document": sha[:12], "pages": [9, 9]}]}]
    client = BagEmbedder()
    report = evaluate(runtime_conn, client, cases, cap_usd=5)
    assert [c["rank"] for c in report["cases"]] == [1, None]
    assert report["hit_at_5"] == Decimal("0.5") and report["mrr"] == Decimal("0.5")
    assert len(client.calls) == 1  # all questions embedded in one request


def test_the_ask_and_eval_commands_print_their_results(runtime_conn, library, tmp_path, monkeypatch):
    import crawler.embed
    from test_p11_cli import cli
    monkeypatch.setattr(crawler.embed, "OpenAIEmbedder", BagEmbedder)
    code, out, err = cli(runtime_conn, "ask", "what does E4 mean", "--manufacturer", "Ruudtest")
    assert code == 0, err
    assert "Searched: manufacturer" in out and "Error Codes" in out and "p.1" in out
    sha = q(runtime_conn, "SELECT content_sha FROM documents WHERE id = %s", (library["model"],))[0][0]
    path = tmp_path / "eval.json"
    path.write_text(json.dumps([{"question": "what does E4 mean", "expect": [{"document": sha[:12], "pages": [1, 1]}]}]))
    code, out, err = cli(runtime_conn, "eval", str(path))
    assert code == 0, err
    assert "top 5: 1 of 1" in out


# ---------------------------------------------------------------- tuning: language preference

FRENCH = ("Mesurer la température de la ligne liquide pour le sous-refroidissement avant de charger le système. "
          "Le technicien doit vérifier la pression et la température avec les outils pour le réfrigérant. ") * 6
ENGLISH = ("Measure the liquid line temperature for subcooling before you charge the system. "
           "The technician must check the pressure and the temperature with the tools for the refrigerant. ") * 6


@pytest.fixture
def twins(runtime_conn, monkeypatch):
    """One maker's manual in English and its French twin, both about subcooling (a Ruud IOM and its French copy);
    and a French-only manual about a topic nothing English covers."""
    from crawler.embed import embed_pass
    en, _ = manual(runtime_conn, monkeypatch, [[t("Subcooling Charging", 14), t(ENGLISH)]], brand_name="Twintest")
    fr, _ = manual(runtime_conn, monkeypatch, [[t("Charge Sous-refroidissement", 14), t(FRENCH)]],
                   brand_name="Twintest", doc_type="install_manual")
    only, _ = manual(runtime_conn, monkeypatch, [[t("Thermopompe Piscine", 14),
                                                  t(("La thermopompe de piscine chauffe l'eau de la piscine pour "
                                                     "le confort des baigneurs et des nageurs. ") * 8)]],
                     brand_name="Twintest")
    embed_pass(runtime_conn, BagEmbedder(), cap_usd=5)
    assert q(runtime_conn, "SELECT DISTINCT language FROM sections WHERE document_id = %s", (fr,)) == (("fr",),)
    return {"en": en, "fr": fr, "only": only}


def test_sections_in_the_questions_language_come_first_and_its_twin_in_another_language_is_left_out(runtime_conn, twins):
    from crawler.search import ask
    result = ask(runtime_conn, BagEmbedder(), "check liquid line temperature subcooling pressure refrigerant",
                 manufacturer="Twintest", cap_usd=5)
    assert docs(result) == [twins["en"]] and result["language"] == "en"


def test_when_nothing_relevant_is_in_that_language_the_same_level_is_searched_in_any_language(runtime_conn, twins):
    from crawler.search import ask
    result = ask(runtime_conn, BagEmbedder(), "thermopompe piscine baigneurs nageurs", manufacturer="Twintest",
                 cap_usd=5)
    assert result["level"] == "manufacturer" and result["language"] is None and docs(result)[0] == twins["only"]


def test_without_a_language_every_language_is_searched(runtime_conn, twins):
    from crawler.search import ask
    result = ask(runtime_conn, BagEmbedder(), "check liquid line temperature subcooling pressure refrigerant",
                 manufacturer="Twintest", language=None, cap_usd=5)
    assert {twins["en"], twins["fr"]} <= set(docs(result))


def test_an_eval_case_can_ask_in_another_language(runtime_conn, twins):
    from crawler.search import evaluate
    sha = q(runtime_conn, "SELECT content_sha FROM documents WHERE id = %s", (twins["fr"],))[0][0]
    cases = [{"question": "mesurer la température de la ligne liquide sous-refroidissement", "manufacturer": "Twintest",
              "language": "fr", "expect": [{"document": sha[:12], "pages": [1, 1]}]}]
    assert evaluate(runtime_conn, BagEmbedder(), cases, cap_usd=5)["cases"][0]["rank"] == 1


# ---------------------------------------------------------------- tuning: codes stay exact
# (tried 2026-10-27: a 5+ character code also matching longer codes it starts, RMHPHDA068 → RMHPHDA068V000; it
# made the eval worse, 20 → 19 of 25, because every section of a model's manual lists the model with some suffix)

def test_a_short_code_matches_only_itself(runtime_conn, monkeypatch):
    from crawler.embed import embed_pass
    from crawler.search import ask
    pages = [[t("Error Codes", 14), t(words(320, "fault") + " Code E45 means the outdoor fan stalled.")]]
    manual(runtime_conn, monkeypatch, pages, brand_name="Shorttest")
    embed_pass(runtime_conn, BagEmbedder(), cap_usd=5)
    hits = ask(runtime_conn, BagEmbedder(), "E4", manufacturer="Shorttest", cap_usd=5)["results"]
    assert not any(h.term_rank for h in hits)


# ---------------------------------------------------------------- tuning: tables lost in the merge

def test_inside_a_models_manuals_its_own_model_number_is_not_a_code_match(runtime_conn, monkeypatch):
    # the monobloc manual names RMHPHDA068 on most pages; matching it there boosted every section but the table
    from crawler.embed import embed_pass
    from crawler.search import ask
    pages = [[t("Unit Dimensions", 14), {"kind": "table", "rows": [["Model", "Height", "Width"],
                                                                    ["RMHPHDA068", '35"', '36"']]}],
             [t("Water Quality", 14), t(words(320, "water") + " The RMHPHDA068 needs water pH 7 to 8.")]]
    manual(runtime_conn, monkeypatch, pages, brand_name="Owncodetest", models=["RMHPHDA068"])
    embed_pass(runtime_conn, BagEmbedder(), cap_usd=5)
    found = ask(runtime_conn, BagEmbedder(), "what is the height of the RMHPHDA068", model="RMHPHDA068", cap_usd=5)
    assert found["level"] == "model" and not any(h.term_rank for h in found["results"])
    wide = ask(runtime_conn, BagEmbedder(), "what is the height of the RMHPHDA068", manufacturer="Owncodetest",
               cap_usd=5)
    assert any(h.term_rank for h in wide["results"])  # without the model narrowing, the code still counts


def test_a_word_in_a_sections_heading_counts_more_than_the_same_word_in_a_long_body(runtime_conn, monkeypatch):
    # the thermistor chart is a long numeric table under "Thermistor Resistance Values": words ranked it #229
    from crawler.embed import embed_pass
    from crawler.search import ask
    rows = [["Temp F", "Resistance kOhm"]] + [[str(n), f"{100 - n / 3:.3f}"] for n in range(120)]
    pages = [[t("Thermistor Resistance Values", 14), {"kind": "table", "rows": rows}],
             [t("Sensor Checks", 14), t(("Check the air sensor wiring and check each sensor plug before checking the "
                                          "board. ") * 30)]]
    manual(runtime_conn, monkeypatch, pages, brand_name="Headingtest")
    for n in range(8):  # most manuals say "check the air sensor" somewhere; few have a thermistor chart
        manual(runtime_conn, monkeypatch, [[t(f"Startup {n}", 14), t("Check the air sensor before startup. "
                                                                        + words(300, f"start{n}x"))]],
               brand_name="Headingtest")
    embed_pass(runtime_conn, BagEmbedder(), cap_usd=5)
    hits = ask(runtime_conn, BagEmbedder(), "thermistor resistance chart for checking air sensors",
                manufacturer="Headingtest", cap_usd=5)["results"]
    best = min((h for h in hits if h.text_rank), key=lambda h: h.text_rank)
    assert best.heading_path == "Thermistor Resistance Values"


def test_question_words_side_by_side_in_a_section_count_more_than_the_same_words_apart(runtime_conn, monkeypatch):
    # guard (passed before and after the rarity weights): words together in the text are not lost to a table of numbers
    from crawler.embed import embed_pass
    from crawler.search import ask
    pages = [[t("Troubleshooting", 14), t(words(300, "fault") + " Error code 33 means the indoor temperature sensor failed.")],
             [t("Capacity Data", 14), {"kind": "table", "rows": [["Model", "Code", "Error margin"],
                                                                  ["FAHFW24A3D", "33", "2%"], ["FAHFW18A3D", "33", "3%"]]}]]
    manual(runtime_conn, monkeypatch, pages, brand_name="Phrasetest")
    for n in range(6):
        manual(runtime_conn, monkeypatch, [[t(f"Startup {n}", 14), t("The error light and the code display. "
                                                                        + words(300, f"boot{n}x"))]],
               brand_name="Phrasetest")
    embed_pass(runtime_conn, BagEmbedder(), cap_usd=5)
    hits = ask(runtime_conn, BagEmbedder(), "error code 33", manufacturer="Phrasetest", cap_usd=5)["results"]
    best = min((h for h in hits if h.text_rank), key=lambda h: h.text_rank)
    assert best.heading_path == "Troubleshooting"

