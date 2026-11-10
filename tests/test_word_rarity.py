"""kb.word_rarity: how rare each word is among live sections, stored once instead of counted on every Breeze search
(the search's slowest part, ~450 of ~750 ms on 17k sections, 2026-11-10). It must equal what search.sql counted live,
and be refreshed by everything that turns sections on or off: the nightly run and the sections, hide, unhide, tag and
extract commands."""
from p3support import q
from test_auto_run import a_brand_with_a_manual, auto
from test_p11_cli import cli
from test_p12_build import PAGES, document, t, words
from p11support import world  # noqa: F401  (world is a fixture)

UNSEEN = "zzqxunseenword"


def counted(conn, lexeme):
    """What search.sql used to count on every question: ln(live sections / live sections with the word, at least 1)."""
    return q(conn, "SELECT ln((SELECT COUNT(*) FROM sections WHERE is_live)::numeric / GREATEST((SELECT COUNT(*) "
                   "FROM sections s JOIN documents d ON d.id = s.document_id WHERE s.is_live AND (s.tsv_weighted @@ "
                   "quote_literal(%s)::tsquery OR d.title_tsv @@ quote_literal(%s)::tsquery)), 1))",
             (lexeme, lexeme))[0][0]


def stored(conn, lexeme):
    return q(conn, "SELECT COALESCE((SELECT idf FROM word_rarity WHERE lexeme = %s), "
                   "(SELECT idf FROM word_rarity WHERE lexeme = ''))", (lexeme,))[0][0]


def live_lexemes(conn):
    return [r[0] for r in q(conn, "SELECT DISTINCT unnest(tsvector_to_array(s.tsv_weighted)) FROM sections s "
                                  "WHERE s.is_live UNION SELECT DISTINCT unnest(tsvector_to_array(d.title_tsv)) FROM "
                                  "sections s JOIN documents d ON d.id = s.document_id WHERE s.is_live ORDER BY 1")]


def lexeme(conn, word):
    return q(conn, "SELECT (tsvector_to_array(to_tsvector('english', %s)))[1]", (word,))[0][0]


def assert_matches_the_library(conn, extra=()):
    checked = live_lexemes(conn) + list(extra) + [UNSEEN]
    wrong = [w for w in checked if stored(conn, w) != counted(conn, w)]
    assert wrong == [], f"{len(wrong)} of {len(checked)} words differ, e.g. {wrong[:5]}"


RARE_PAGES = [[t("Thermistor Wiring", 14), t(words(200, "thermistor") + " glorfindelwidget sensor")]] + PAGES


def two_manuals(conn):
    """Two live manuals, so hiding one changes the live total and with it every word's rarity."""
    from crawler.kb import build, publish, refresh_word_rarity
    made = []
    for pages in (RARE_PAGES, PAGES):
        doc, _, _ = document(conn)
        build(conn, doc, pages=pages)
        publish(conn, doc)
        made.append(doc)
    refresh_word_rarity(conn)
    return made


def test_the_stored_rarity_is_what_counting_every_live_section_gives(runtime_conn):
    two_manuals(runtime_conn)
    assert_matches_the_library(runtime_conn)


def test_the_hide_unhide_and_tag_commands_refresh_the_rarity(runtime_conn):
    doc, _ = two_manuals(runtime_conn)
    rare = lexeme(runtime_conn, "glorfindelwidget")
    before = stored(runtime_conn, UNSEEN)

    assert cli(runtime_conn, "hide", "manual", str(doc), "--reason", "bad scan")[0] == 0
    assert stored(runtime_conn, UNSEEN) != before  # fewer live sections
    assert_matches_the_library(runtime_conn, [rare])

    assert cli(runtime_conn, "unhide", "manual", str(doc), "--reason", "fixed")[0] == 0
    assert stored(runtime_conn, UNSEEN) == before
    assert_matches_the_library(runtime_conn, [rare])

    assert cli(runtime_conn, "tag", str(doc), "--doc-type", "not_equipment")[0] == 0
    assert stored(runtime_conn, UNSEEN) != before
    assert_matches_the_library(runtime_conn, [rare])


def test_the_sections_command_refreshes_the_rarity(runtime_conn, monkeypatch):
    import crawler.kb
    two_manuals(runtime_conn)
    before = stored(runtime_conn, UNSEEN)
    real = crawler.kb.document_pages
    doc, _, _ = document(runtime_conn)
    monkeypatch.setattr(crawler.kb, "document_pages",
                        lambda conn, d, **kw: RARE_PAGES if d == doc else real(conn, d, **kw))
    assert cli(runtime_conn, "sections")[0] == 0
    assert stored(runtime_conn, UNSEEN) != before  # more live sections
    assert_matches_the_library(runtime_conn, [lexeme(runtime_conn, "glorfindelwidget")])


def test_the_nightly_run_leaves_the_rarity_matching_the_library(world):  # noqa: F811
    from crawler.kb import refresh_word_rarity
    refresh_word_rarity(world.conn)
    a_brand_with_a_manual(world)
    assert auto(world)["state"] == "done"
    assert q(world.conn, "SELECT COUNT(*) FROM sections WHERE is_live")[0][0] > 0
    assert_matches_the_library(world.conn)
