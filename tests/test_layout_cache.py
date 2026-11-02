"""Layout cache (user, 2026-10-28: rebuilds must be faster without a smaller sample). Reading a PDF's layout is the slow
part of `./crawl sections` (about 8 s a document); the sectioner itself is quick. The reader's answer is kept in the
library (DOCS_DIR/.layout/<sha>.json) under a key made from the reader's own code and library versions, so a
sectioner change rebuilds from the kept answer and a reader change reads every PDF again."""
import pytest

from p3support import make_pdf

PDF = make_pdf([["Installation Manual", "Read all instructions before installing the unit."]])


def no_reader(*args, **kwargs):
    raise AssertionError("the PDF was read again")


def test_a_pdf_layout_is_read_once_and_kept_in_the_library(tmp_path, monkeypatch):
    import crawler.textextract
    monkeypatch.setenv("DOCS_DIR", str(tmp_path))
    first = crawler.textextract.extract_layout(PDF)
    assert any(tmp_path.joinpath(".layout").iterdir())
    monkeypatch.setattr(crawler.textextract, "_run_child", no_reader)
    assert crawler.textextract.extract_layout(PDF) == first


def test_a_changed_reader_reads_the_pdf_again(tmp_path, monkeypatch):
    import crawler.textextract
    monkeypatch.setenv("DOCS_DIR", str(tmp_path))
    first = crawler.textextract.extract_layout(PDF)
    monkeypatch.setattr(crawler.textextract, "_layout_reader", lambda: "a newer reader")
    calls = []
    real = crawler.textextract._run_child
    monkeypatch.setattr(crawler.textextract, "_run_child", lambda *a, **k: calls.append(1) or real(*a, **k))
    assert crawler.textextract.extract_layout(PDF) == first and calls == [1]


def test_the_reader_key_follows_the_reader_code():
    from pathlib import Path
    import crawler.textextract
    key = crawler.textextract._layout_reader()
    assert key == crawler.textextract._layout_reader()
    assert __import__("hashlib").sha256(Path(crawler.textextract.__file__).with_name("pdfchild.py").read_bytes()
                                        ).hexdigest()[:16] in key


def test_a_failed_read_is_not_kept(tmp_path, monkeypatch):
    import crawler.textextract
    monkeypatch.setenv("DOCS_DIR", str(tmp_path))

    def too_slow(*args, **kwargs):
        raise crawler.textextract.ExtractError("the PDF took too long to read")

    monkeypatch.setattr(crawler.textextract, "_run_child", too_slow)
    with pytest.raises(crawler.textextract.ExtractError):
        crawler.textextract.extract_layout(PDF)
    monkeypatch.undo()
    monkeypatch.setenv("DOCS_DIR", str(tmp_path))
    assert any(i.get("text") == "Installation Manual" for i in crawler.textextract.extract_layout(PDF)[0])


def test_a_shorter_page_limit_is_kept_apart(tmp_path, monkeypatch):
    import crawler.textextract
    monkeypatch.setenv("DOCS_DIR", str(tmp_path))
    two = make_pdf([["Page one"], ["Page two"]])
    assert len(crawler.textextract.extract_layout(two, max_pages=1)) == 1
    assert len(crawler.textextract.extract_layout(two)) == 2


def test_a_damaged_kept_answer_is_read_again(tmp_path, monkeypatch):
    import crawler.textextract
    monkeypatch.setenv("DOCS_DIR", str(tmp_path))
    first = crawler.textextract.extract_layout(PDF)
    for path in tmp_path.joinpath(".layout").iterdir():
        path.write_text("{not json")
    assert crawler.textextract.extract_layout(PDF) == first


def test_a_pdf_read_whole_under_a_lower_page_limit_is_not_read_again_under_a_higher_one(tmp_path, monkeypatch):
    # 2026-10-29: the limit went from 200 to 300 pages; the 596 kept layouts of shorter PDFs are still whole
    import crawler.textextract
    monkeypatch.setenv("DOCS_DIR", str(tmp_path))
    two = make_pdf([["Page one of the manual"], ["Page two of the manual"]])
    first = crawler.textextract.extract_layout(two, max_pages=5)
    monkeypatch.setattr(crawler.textextract, "_run_child", no_reader)
    assert crawler.textextract.extract_layout(two, max_pages=9) == first


def test_a_pdf_cut_at_a_lower_page_limit_is_read_again_under_a_higher_one(tmp_path, monkeypatch):
    import crawler.textextract
    monkeypatch.setenv("DOCS_DIR", str(tmp_path))
    three = make_pdf([["Page one of the manual"], ["Page two of the manual"], ["Page three of the manual"]])
    assert len(crawler.textextract.extract_layout(three, max_pages=2)) == 2
    assert len(crawler.textextract.extract_layout(three, max_pages=9)) == 3


def test_a_new_pdf_is_read_once_for_its_text_and_its_sections(tmp_path, monkeypatch):
    # user 2026-11-02: the crawl read a new PDF for its text and the section build read it again for its layout. One
    # read now serves both: the crawl's read is the layout read, and it is kept.
    import crawler.textextract
    monkeypatch.setenv("DOCS_DIR", str(tmp_path))
    text = crawler.textextract.extract_pdf(PDF)
    assert "Read all instructions before installing the unit." in text.pages[0]
    monkeypatch.setattr(crawler.textextract, "_run_child", no_reader)
    assert [i["text"] for i in crawler.textextract.extract_layout(PDF)[0]] == \
        ["Installation Manual", "Read all instructions before installing the unit."]
    assert crawler.textextract.extract_pdf(PDF).pages == text.pages  # a re-read of the text needs no read either
