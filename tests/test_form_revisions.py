"""Form-number revisions (user, 2026-10-29): `check` listed Ruud pairs that are one form at two revisions ("M44-2230" /
"M44-2230 Rev. 1", "AP21265" / "AP21265 Rev 04"), both live, because the form rule (D55) matched form numbers exactly.
A form's base (without its "Rev" mark) groups the versions; the highest revision is the one live, whichever was found
first (doc 840, found after doc 50, is the older revision). Only an explicit "Rev" counts: a trailing "-4" / "-3" can be
another variant (AP11828-4 is Rheem Canada's warranty, AP11828-3 the US one)."""
import pytest

from p3support import q
from test_p12_build import document
from test_p13_embed import PAGES, live_document


@pytest.mark.parametrize("form,base,rev", [
    ("M44-2230 Rev. 1", "M44-2230", 1), ("M44-2230", "M44-2230", 0), ("AP21265 Rev 04", "AP21265", 4),
    ("G22-546 REV B", "G22-546", 2), ("AP11828-4 (05/14)", "AP11828-4", 0), ("92-26150-10-42", "92-26150-10-42", 0),
    ("ap21265 rev. 3 (01/20)", "AP21265", 3), ("PREVENT-12", "PREVENT-12", 0),
])
def test_a_forms_base_and_revision_are_read_from_its_number(runtime_conn, form, base, rev):
    doc, _, _ = document(runtime_conn, form=form)
    assert q(runtime_conn, "SELECT form_base, form_rev FROM documents WHERE id = %s", (doc,)) == ((base, rev),)


def state(conn, doc):
    return q(conn, "SELECT state FROM documents WHERE id = %s", (doc,))[0][0]


def test_a_higher_revision_going_live_retires_the_lower_one(runtime_conn, monkeypatch):
    old, _, name = live_document(runtime_conn, monkeypatch, form="M44-2230")
    new, _, _ = live_document(runtime_conn, monkeypatch, brand_name=name, form="M44-2230 Rev. 1")
    assert (state(runtime_conn, old), state(runtime_conn, new)) == ("retired", "live")
    assert q(runtime_conn, "SELECT COUNT(*) FROM sections WHERE document_id = %s AND is_live", (old,))[0][0] == 0


def test_a_lower_revision_found_later_never_goes_live(runtime_conn, monkeypatch):
    import crawler.kb
    from crawler.kb import build, publish
    new, _, name = live_document(runtime_conn, monkeypatch, form="M44-3123 Rev. 1")
    old, _, _ = document(runtime_conn, name, form="M44-3123")
    monkeypatch.setattr(crawler.kb, "document_pages", lambda c, d, **k: PAGES)
    assert publish(runtime_conn, old, build(runtime_conn, old)) == (False, "a newer revision of its form is live")
    assert state(runtime_conn, new) == "live"


def test_a_trailing_number_is_another_variant_not_a_revision(runtime_conn, monkeypatch):
    canada, _, name = live_document(runtime_conn, monkeypatch, form="AP11828-4 (05/14)")
    us, _, _ = live_document(runtime_conn, monkeypatch, brand_name=name, form="AP11828-3")
    assert (state(runtime_conn, canada), state(runtime_conn, us)) == ("live", "live")


def test_another_language_of_the_same_form_stays_live(runtime_conn, monkeypatch):
    english, _, name = live_document(runtime_conn, monkeypatch, form="AP21265")
    french, _, _ = live_document(runtime_conn, monkeypatch, brand_name=name, form="AP21265 Rev 04", language="fr")
    assert (state(runtime_conn, english), state(runtime_conn, french)) == ("live", "live")


def test_the_tagged_versions_of_a_form_point_at_the_revision_each_replaces(runtime_conn):
    from test_small_fixes import replaces, tag_with_form, twin_documents
    _, (one, two) = twin_documents(runtime_conn)
    tag_with_form(runtime_conn, two, form="M44-3123")  # the older revision, found and tagged second
    tag_with_form(runtime_conn, one, form="M44-3123 Rev. 1")
    assert replaces(runtime_conn, one, two) == [two, None]


# ---------------------------------------------------------------- check: duplicates are judged by their text too

def manual_text(seed, n=400):
    from test_p12_build import t
    return [[t("Installation", 14), t(" ".join(f"{seed}{i} unit{i % 13}" for i in range(n)) + ".")]]


def titled(conn, monkeypatch, name, pages, title="USE AND INSTALLATION INSTRUCTIONS"):
    doc, _, name = live_document(conn, monkeypatch, pages=pages, brand_name=name)
    q(conn, "UPDATE documents SET title = %s WHERE id = %s", (title, doc))
    return doc, name


def test_check_lists_two_manuals_with_one_title_and_mostly_the_same_text_as_likely_duplicates(runtime_conn, monkeypatch):
    from crawler.kb import check
    one, name = titled(runtime_conn, monkeypatch, None, manual_text("step"))
    two, _ = titled(runtime_conn, monkeypatch, name, manual_text("step", 380))  # a newer edition, a little shorter
    assert (one, two) in check(runtime_conn)["likely_duplicates"]


def test_check_leaves_out_two_manuals_that_only_share_a_generic_title(runtime_conn, monkeypatch):
    # docs 78 and 80: the R-454B cassette and ducted IOMs, both "USE AND INSTALLATION INSTRUCTIONS", half their text shared
    from crawler.kb import check
    one, name = titled(runtime_conn, monkeypatch, None, manual_text("cassette"))
    two, _ = titled(runtime_conn, monkeypatch, name, manual_text("ducted"))
    assert (one, two) not in check(runtime_conn)["likely_duplicates"]


def test_a_pair_marked_as_different_manuals_leaves_the_duplicate_list(runtime_conn, monkeypatch):
    # 2026-10-29: 6 Ruud pairs compared and found to be different products (317/318: Canada vs US warranty) kept showing
    from crawler.kb import check, not_duplicates
    one, name = titled(runtime_conn, monkeypatch, None, manual_text("step"))
    two, _ = titled(runtime_conn, monkeypatch, name, manual_text("step", 380))
    not_duplicates(runtime_conn, two, one, reason="Canada vs US warranty")  # either order
    report = check(runtime_conn)
    assert (one, two) not in report["likely_duplicates"] and report["marked_different"] >= 1


def test_the_not_duplicates_command_marks_a_pair(runtime_conn, monkeypatch):
    from test_p11_cli import cli
    one, name = titled(runtime_conn, monkeypatch, None, manual_text("step"))
    two, _ = titled(runtime_conn, monkeypatch, name, manual_text("step", 380))
    code, out, err = cli(runtime_conn, "not-duplicates", str(one), str(two), "--reason", "gas vs electric")
    assert code == 0, err
    assert f"{one} and {two}" in out
    code, out, err = cli(runtime_conn, "not-duplicates", str(one), str(one), "--reason", "same")
    assert "two different manuals" in out + err
