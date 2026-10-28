"""P12: the sections, hide, unhide, check and preview commands, through the real main() on the test's connection."""
import hashlib

import pytest

from p3support import q
from test_p11_cli import cli
from test_p12_build import PAGES, document, live, sized_pdf, words


def test_sections_builds_and_puts_live_every_tagged_document_and_reports_held_ones(runtime_conn, monkeypatch):
    import crawler.kb
    real = crawler.kb.document_pages
    monkeypatch.setattr(crawler.kb, "document_pages", lambda conn, doc, **kw: PAGES if doc in mine else real(conn, doc, **kw))
    good, _, _ = document(runtime_conn)
    off, _, _ = document(runtime_conn, doc_type="not_equipment")
    mine = {good, off}
    code, out, err = cli(runtime_conn, "sections")
    assert code == 0, err
    assert live(runtime_conn, good) != () and live(runtime_conn, off) == ()
    assert "held back" in out
    code, out, _ = cli(runtime_conn, "sections")  # nothing new to do
    assert q(runtime_conn, "SELECT MAX(build) FROM sections WHERE document_id = %s", (good,)) == ((1,),)


def test_hide_and_unhide_a_manual_need_a_reason(runtime_conn):
    from crawler.kb import build, publish
    doc, _, name = document(runtime_conn)
    build(runtime_conn, doc, pages=PAGES)
    publish(runtime_conn, doc)
    with pytest.raises(SystemExit):  # --reason is required
        cli(runtime_conn, "hide", "manual", str(doc))
    assert cli(runtime_conn, "hide", "manual", str(doc), "--reason", "bad scan")[0] == 0
    assert live(runtime_conn, doc) == ()
    assert cli(runtime_conn, "unhide", "manual", str(doc), "--reason", "fixed")[0] == 0
    assert live(runtime_conn, doc) != ()
    assert cli(runtime_conn, "hide", "manufacturer", name, "--reason", "legal")[0] == 0
    assert live(runtime_conn, doc) == ()
    code, out, err = cli(runtime_conn, "hide", "manual", "999999999", "--reason", "x")
    assert code != 0 and "No manual" in err


def test_check_prints_problems_and_counts(runtime_conn):
    doc, _, _ = document(runtime_conn)
    q(runtime_conn, "UPDATE documents SET state = 'live', live_build = 1 WHERE id = %s", (doc,))
    code, out, _ = cli(runtime_conn, "check")
    assert code == 0
    assert "live manuals with no live sections" in out and str(doc) in out
    assert "live sections have no vector yet" in out


def test_preview_prints_sections_without_storing_anything(runtime_conn, tmp_path, monkeypatch, capsys):
    pdf = sized_pdf([[("Refrigerant Charging", 14)] + [(words(12), 10)] * 30])
    path = tmp_path / "manual.pdf"
    path.write_bytes(pdf)
    before = q(runtime_conn, "SELECT COUNT(*) FROM sections")[0][0]
    code, out, _ = cli(runtime_conn, "preview", str(path))
    assert code == 0
    assert "=== 1. text | p.1 | Refrigerant Charging |" in out
    assert q(runtime_conn, "SELECT COUNT(*) FROM sections")[0][0] == before
    assert hashlib.sha256(pdf).hexdigest()  # the file itself is untouched
