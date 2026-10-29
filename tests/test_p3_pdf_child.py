"""P3: the PDF child process is boxed in: no secrets, bounded page text, bounded output.

Seams (all in the code as written, read before these tests were added):
- crawler.textextract.extract_pdf starts the child with subprocess.Popen(..., env=<dict>). The first
  test wraps Popen to see that dict; it still starts the real child.
- crawler.pdfchild.main() reads argv[1] and the PDF bytes from stdin and prints JSON. The second
  test runs it in-process with a stand-in pdfplumber, so no huge PDF is needed.
- crawler.textextract.PDF_MAX_OUTPUT_BYTES is read at call time; the third test lowers it.
"""
import io
import json
import sys
import types

import pytest

from p3support import make_pdf

SECRETS = {"EQ_RUNTIME_PASSWORD": "runtime-pass-should-not-leak", "EQ_MIGRATE_PASSWORD": "migrate-pass-should-not-leak",
           "ANTHROPIC_API_KEY_CRAWLER": "sk-ant-should-not-leak"}


def test_the_pdf_child_gets_none_of_the_parents_secrets(monkeypatch):
    import subprocess

    from crawler import textextract

    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)
    seen = []
    real_popen = subprocess.Popen

    def spy(*args, **kwargs):
        seen.append(kwargs.get("env"))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(textextract.subprocess, "Popen", spy)
    out = textextract.extract_pdf(make_pdf([["Model RA15AZ24AJ3CA", "SEER2 15.2"]]))

    assert out.pages and "RA15AZ24AJ3CA" in out.pages[0]  # the real child ran, so what we saw was really passed to it
    assert seen and all(isinstance(env, dict) for env in seen)  # an explicit environment, not "inherit everything"
    for env in seen:  # the reader, and the OCR child on a page this short
        for key, value in SECRETS.items():
            assert key not in env
            assert value not in env.values()
        assert not [k for k in env if k.startswith(("EQ_", "ANTHROPIC", "OPENAI", "AWS_"))]


def run_child(monkeypatch, page_text, closed=None):
    from crawler import pdfchild

    class Page:
        def extract_text(self):
            return page_text

        def close(self):
            if closed is not None:
                closed.append(self)

    class Pdf:
        pages = [Page(), Page()]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake = types.ModuleType("pdfplumber")
    fake.open = lambda handle: Pdf()
    monkeypatch.setitem(sys.modules, "pdfplumber", fake)
    monkeypatch.setattr(pdfchild, "_clip_aware", lambda: None)  # the stand-in has no pdfminer underneath to hook
    monkeypatch.setattr(sys, "argv", ["pdfchild", "5"])
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"%PDF-1.4 stand-in")))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    pdfchild.main()
    return json.loads(out.getvalue())["pages"]


def test_one_pages_text_is_cut_to_50000_characters(monkeypatch):
    pages = run_child(monkeypatch, "x" * 60_000)
    assert len(pages) == 2
    assert len(pages[0]) == 50_000


def test_each_page_is_closed_once_read_so_a_big_manual_fits_in_memory(monkeypatch):
    closed = []
    run_child(monkeypatch, "text", closed)  # a 37 MB service manual peaked at 1.6 GB without this, 0.2 GB with it
    assert len(closed) == 2


def test_a_short_pages_text_is_left_alone(monkeypatch):
    assert run_child(monkeypatch, "short text") == ["short text", "short text"]  # control: the cut is not applied to everything


def test_child_output_over_the_parents_limit_is_an_error(monkeypatch):
    from crawler import textextract
    pdf = make_pdf([["Model RA15AZ24AJ3CA", "SEER2 15.2", "Tonnage 2.0"]])
    assert textextract.extract_pdf(pdf).pages  # control: with the normal limit it works
    monkeypatch.setattr(textextract, "PDF_MAX_OUTPUT_BYTES", 50)
    with pytest.raises(textextract.ExtractError):
        textextract.extract_pdf(pdf)
