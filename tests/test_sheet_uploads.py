"""EQUIPMENT_FILE_UPLOADS.md phase 2 (D4, D6, D8): Excel (.xlsx) and CSV files uploaded on the admin page.

A spreadsheet is read row by row with its column headings ("Tons: 2, SEER2: 15.2, Outdoor: RA14AY24BJ1NA"), one page
per sheet, so a model-number question finds its row. Title rows above the headings, merged headings and a label merged
down the side are handled; hidden sheets and columns are left out. A column whose heading has the word price, cost or
net is left out (whole words: "Cabinet" stays); a file whose name has the word price, pricing or quoter is refused as a
price sheet. The document carries its file type (pdf for every document already there), and the library, reread,
section build, `check` and parse issues follow it.

Local library in a tmp folder (DOCS_DIR, inbox/ inside it), fake AI, `_testing`, one rolled-back transaction per test."""
import hashlib
import io
import uuid

import pytest

from p11support import world  # noqa: F401  (world is a fixture)
from p3support import q
from test_auto_run import auto
from test_uploads import discovered, outcome, process, queue

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def workbook(build):
    """An .xlsx made in the test: build(openpyxl.Workbook) fills it."""
    import openpyxl
    book = openpyxl.Workbook()
    build(book)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def matchup(book):
    """The shape of the drive's matchup tables: titles above the headings, a price column per part, a blank row."""
    sheet = book.active
    sheet.title = "14 SEER2 Cased Coil"
    sheet["I1"] = "     Split AC with Constant Torque Furnace"
    sheet["I3"] = "14 SEER2  80% Furnace, Upflow/Horizontal "
    sheet.append([])
    sheet.append(["Tons", "Seer2", "Outdoor", "Outdoor Price", "Cabinet", "Coil", "Coil Price", "Net cost",
                  "System Price", "Notes"])
    sheet.append([2, 15.2, "RA14AY24BJ1NA", 1234, "B", "RCFY2414STAAM", 567, 89, 1890, None])
    sheet.append([])
    sheet.append([2.5, 14.299999999, "RA14AY30AJ1NA", 1300, None, "RCFY3617STANM", 600, 90, 1990, "2 stage"])


def coil_cross_reference(book):
    """The shape of '2026 Coil model Consolidation.xlsx': group headings merged across, sub-headings under them, and a
    category label merged down the first column."""
    sheet = book.active
    sheet.title = "Marked Table"
    sheet.append([None, "Old Coils", None, None, "M1 Coils", "Item"])
    sheet.append(["AIR HANDLER COILS", "Cu. Tin Pl. Rpl. Coil", "Cu. Rpl. Coil", "Al. Rpl. Coil", "Al. Rpl. Coil", None])
    sheet.append([None, "RCSL-HU2417TU", "RCSL-HU2417CU", "RCST-HU2417BU", "RCHZ2417STANVU/\nRCHZ2517STANVU", None])
    sheet.append([None, "N/A", "N/A", "N/A", "RCHZ2421MEANVU", "EEV"])
    sheet.merge_cells("B1:D1")
    sheet.merge_cells("A2:A4")


def read(body, file_type="xlsx"):
    from crawler.sheets import read_sheets
    return read_sheets(body, file_type).pages


# ---------------------------------------------------------------- reading (D6, D8)

def test_each_row_is_read_with_its_headings_under_the_titles_and_price_columns_are_left_out():
    (page,) = read(workbook(matchup))
    lines = page.splitlines()
    assert lines[0] == "Sheet: 14 SEER2 Cased Coil"
    assert lines[1:3] == ["Split AC with Constant Torque Furnace", "14 SEER2 80% Furnace, Upflow/Horizontal"]
    assert "Tons: 2, Seer2: 15.2, Outdoor: RA14AY24BJ1NA, Cabinet: B, Coil: RCFY2414STAAM" in lines
    assert "Tons: 2.5, Seer2: 14.3, Outdoor: RA14AY30AJ1NA, Coil: RCFY3617STANM, Notes: 2 stage" in lines
    for price in ("1234", "567", "89", "1890", "1300", "600", "1990", "Price", "cost"):
        assert price not in page


def test_merged_group_headings_join_their_sub_headings_and_a_side_label_goes_on_each_row():
    (page,) = read(workbook(coil_cross_reference))
    lines = page.splitlines()
    assert ("AIR HANDLER COILS, Old Coils Cu. Tin Pl. Rpl. Coil: RCSL-HU2417TU, Old Coils Cu. Rpl. Coil: RCSL-HU2417CU, "
            "Old Coils Al. Rpl. Coil: RCST-HU2417BU, M1 Coils Al. Rpl. Coil: RCHZ2417STANVU/ RCHZ2517STANVU") in lines
    assert ("AIR HANDLER COILS, Old Coils Cu. Tin Pl. Rpl. Coil: N/A, Old Coils Cu. Rpl. Coil: N/A, "
            "Old Coils Al. Rpl. Coil: N/A, M1 Coils Al. Rpl. Coil: RCHZ2421MEANVU, Item: EEV") in lines


def test_hidden_sheets_and_hidden_columns_are_left_out():
    def build(book):
        shown = book.active
        shown.title = "Models"
        shown.append(["Model", "Secret", "Tons"])
        shown.append(["RA14AY24BJ1NA", "HIDDENVALUE", 2])
        shown.column_dimensions["B"].hidden = True
        hidden = book.create_sheet("Lookups")
        hidden.append(["Key", "Value"])
        hidden.append(["LOOKUPROW", 1])
        hidden.sheet_state = "hidden"
    pages = read(workbook(build))
    assert len(pages) == 1
    assert "Model: RA14AY24BJ1NA, Tons: 2" in pages[0].splitlines()
    assert "HIDDENVALUE" not in pages[0] and "LOOKUPROW" not in pages[0]


def test_a_csv_is_read_row_by_row_with_its_headings():
    body = "﻿Motor,Replacement,List price\nK55HXJGS-8901,51-104181-01,$310\n".encode("utf-8")
    (page,) = read(body, "csv")
    assert page.splitlines() == ["Motor: K55HXJGS-8901, Replacement: 51-104181-01"]


@pytest.mark.parametrize("name, refused", [
    ("R454B Ruud 316L Pricing 6.25.xlsx", True),
    ("Fujitsu Quoter CVR 06-05-2023 v05082023 Platinum Transact Pricing.xlsx", True),
    ("Armbridge R32 Price Sheet Tempelate.xlsx", True),
    ("prices_2026.csv", True),
    ("2026 Coil model Consolidation.xlsx", False),
    ("Priceless cabinet sizes.xlsx", False),
])
def test_a_file_named_as_a_price_sheet_is_spotted_by_whole_words(name, refused):
    from crawler.sheets import price_sheet
    assert price_sheet(name) is refused


# ---------------------------------------------------------------- uploads

def test_a_queued_workbook_becomes_a_document_of_its_own_type(world):
    from crawler import library
    body = workbook(matchup)
    upload = queue(world.conn, body, name="Copy of 2026 Ruud Template.xlsx", brand=world.brand)
    assert process(world.conn)["added"] == 1
    state, reason, document_id, _ = outcome(world.conn, upload)
    assert state == "added" and reason is None
    sha = hashlib.sha256(body).hexdigest()
    assert q(world.conn, "SELECT file_type, needs_ocr, page_count FROM documents WHERE id = %s",
             (document_id,))[0] == ("xlsx", False, 1)
    assert library.has_file(sha, "xlsx") and not library.has_file(sha, "pdf")
    assert q(world.conn, "SELECT p.is_pdf, p.content_type FROM pages p JOIN control.uploads u ON u.page_id = p.id "
                         "WHERE u.id = %s", (upload,))[0] == (False, XLSX)
    body_text = q(world.conn, "SELECT t.body FROM page_text t JOIN control.uploads u ON u.page_id = t.page_id "
                              "WHERE u.id = %s", (upload,))[0][0]
    assert "Outdoor: RA14AY24BJ1NA" in body_text and "1234" not in body_text


def test_a_csv_upload_is_stored_as_csv(world):
    from crawler import library
    body = f"Motor,Replacement\nK55HXJGS-{uuid.uuid4().hex[:4]},51-104181-01\n".encode()
    upload = queue(world.conn, body, name="Alt. Motors_Sheet1.csv")
    process(world.conn)
    document_id = outcome(world.conn, upload)[2]
    assert q(world.conn, "SELECT file_type FROM documents WHERE id = %s", (document_id,))[0][0] == "csv"
    assert library.has_file(hashlib.sha256(body).hexdigest(), "csv")


def test_a_price_sheet_is_refused_and_nothing_is_stored(world):
    body = workbook(matchup)
    upload = queue(world.conn, body, name="R454B Ruud 316L Pricing 6.25.xlsx")
    assert process(world.conn)["rejected"] == 1
    state, reason, document_id, _ = outcome(world.conn, upload)
    assert (state, reason, document_id) == ("rejected", "a price sheet", None)
    assert not q(world.conn, "SELECT 1 FROM documents WHERE content_sha = %s", (hashlib.sha256(body).hexdigest(),))


def test_a_file_of_a_type_it_does_not_read_is_refused(world):
    upload = queue(world.conn, b"PK\x03\x04 a zip of manuals", name="Service notes.zip")
    process(world.conn)
    assert outcome(world.conn, upload)[:2] == ("rejected", "not a file Tracforce reads")


def test_a_broken_workbook_is_rejected_and_its_parse_issue_names_an_xlsx_original(world):
    body = b"PK\x03\x04" + uuid.uuid4().bytes + b" not really a workbook"
    upload = queue(world.conn, body, name="Broken.xlsx")
    process(world.conn)
    state, reason, _, _ = outcome(world.conn, upload)
    assert state == "rejected" and "could not be read" in reason
    sha = hashlib.sha256(body).hexdigest()
    assert q(world.conn, "SELECT original FROM control.parse_issues WHERE content_sha = %s", (sha,))[0][0] == f"{sha}.xlsx"


def test_a_pdf_document_is_still_a_pdf(world):
    from test_uploads import a_pdf
    upload = queue(world.conn, a_pdf("Model RA15AZ24AJ3CA"), brand=world.brand)
    process(world.conn)
    assert q(world.conn, "SELECT file_type FROM documents WHERE id = %s", (outcome(world.conn, upload)[2],))[0][0] == "pdf"


# ---------------------------------------------------------------- tagging, sections, search (phase 2 done check)

def test_the_coil_cross_reference_is_tagged_live_and_an_old_coil_number_finds_its_replacement_row(world):
    from crawler.search import search
    from test_p14_search import BagEmbedder
    discovered(world)
    upload = queue(world.conn, workbook(coil_cross_reference), name="2026 Coil model Consolidation.xlsx",
                   brand=world.brand)
    auto(world)
    document_id = outcome(world.conn, upload)[2]
    assert q(world.conn, "SELECT state FROM documents WHERE id = %s", (document_id,))[0][0] == "live"
    question = "What replaces coil RCSL-HU2417CU?"
    hits = search(world.conn, question, BagEmbedder().embed([question])[0][0])
    ours = [h for h in hits if h.document_id == document_id]
    assert ours and "M1 Coils Al. Rpl. Coil: RCHZ2417STANVU" in ours[0].body
    assert ours[0].heading_path == "Marked Table"


def test_a_long_sheet_is_split_between_rows_and_every_piece_keeps_the_sheet_name(world):
    from crawler import kb

    def build(book):
        sheet = book.active
        sheet.title = "Zoning"
        sheet.append(["Model", "Minimum airflow CFM", "Zones"])
        for n in range(400):
            sheet.append([f"ZN{n:04d}AB{uuid.uuid4().hex[:4].upper()}", 400 + n, 2 + n % 3])
    upload = queue(world.conn, workbook(build), name="Zoning - Minimum Airflows.xlsx", brand=world.brand)
    process(world.conn)
    document_id = outcome(world.conn, upload)[2]
    q(world.conn, "UPDATE documents SET state = 'tagged', doc_type = 'spec_sheet' WHERE id = %s", (document_id,))
    assert kb.build(world.conn, document_id) == 1
    pieces = q(world.conn, "SELECT heading_path, body FROM sections WHERE document_id = %s ORDER BY section_no",
               (document_id,))
    assert len(pieces) > 1
    assert {path for path, _ in pieces} == {"Zoning"}
    rows = [line for _, body in pieces for line in body.splitlines()]
    assert len(rows) == 400 and all(line.startswith("Model: ZN") for line in rows)


def test_the_section_build_reads_the_workbook_not_a_pdf_and_check_finds_its_original(world, monkeypatch):
    from crawler import kb, library
    upload = queue(world.conn, workbook(matchup), name="Ruud matchups.xlsx", brand=world.brand)
    process(world.conn)
    document_id = outcome(world.conn, upload)[2]
    sha = q(world.conn, "SELECT content_sha FROM documents WHERE id = %s", (document_id,))[0][0]
    # the stored text is changed, so a section that holds the workbook's own row proves the original was read
    q(world.conn, "UPDATE page_text SET body = 'Sheet: Stale\nstale text' WHERE page_id = "
                  "(SELECT page_id FROM control.uploads WHERE id = %s)", (upload,))
    problems = []
    pages = kb.document_pages(world.conn, document_id, problems=problems)
    assert problems == []
    assert any("Outdoor: RA14AY24BJ1NA" in item["text"] for page in pages for item in page)
    q(world.conn, "UPDATE documents SET state = 'live' WHERE id = %s", (document_id,))
    assert document_id not in kb._missing_originals(world.conn)
    assert f"{sha}.xlsx" in library.stored_keys()


def test_reread_reads_a_workbook_as_a_workbook(world):
    from crawler.documents import reread
    upload = queue(world.conn, workbook(matchup), name="Ruud matchups.xlsx", brand=world.brand)
    process(world.conn)
    document_id = outcome(world.conn, upload)[2]
    q(world.conn, "UPDATE page_text SET body = 'stale' WHERE page_id = (SELECT page_id FROM control.uploads WHERE id = %s)",
      (upload,))
    result = reread(world.conn, documents=[document_id])
    assert (result["read"], result["unreadable"]) == (1, 0)
    body = q(world.conn, "SELECT t.body FROM page_text t JOIN control.uploads u ON u.page_id = t.page_id "
                         "WHERE u.id = %s", (upload,))[0][0]
    assert "Outdoor: RA14AY24BJ1NA" in body
    assert q(world.conn, "SELECT needs_ocr FROM documents WHERE id = %s", (document_id,))[0][0] is False
