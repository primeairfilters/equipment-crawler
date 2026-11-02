"""Reads a spreadsheet from stdin and prints its text as JSON. Run only as a child process with limits (crawler/sheets.py).

`sheetchild.py xlsx|csv` prints {"pages": [text, ...]}, one page per sheet (EQUIPMENT_FILE_UPLOADS.md D6). A page is
the line "Sheet: <name>" (not for a CSV, which has none), then the title lines above the column headings, then one line
per row with its headings: "Tons: 2, SEER2: 15.2, Outdoor: RA14AY24BJ1NA, Coil: RCFY2414STAAM".

- The headings are the first row with two or more different values; the rows above it are titles. A heading row with a
  heading merged across columns ("Old Coils" over three columns) is a group: the row under it joins it ("Old Coils
  Cu. Rpl. Coil"), and so on down to MAX_HEADING_ROWS while each row has a heading merged across.
- A label merged down the side ("AIR HANDLER COILS" beside 37 rows) is not a heading; it goes on each of its rows.
- A row that repeats the headings starts them again (a sheet of several tables).
- A column whose heading has the word price, cost or net is left out (D8: whole words, so "Cabinet" stays).
- Hidden sheets, rows and columns are left out: that is where a workbook keeps its lookups and prices.
- A formula gives its last saved value; an error value (#N/A, #REF!) gives nothing.
"""
import csv
import io
import itertools
import json
import re
import sys
from datetime import date, datetime, time, timedelta

SHEET = "Sheet: "
MAX_SHEETS = 100
MAX_ROWS = 5000
MAX_COLUMNS = 200
MAX_HEADING_ROWS = 3
PRICE = re.compile(r"(?<![A-Za-z])(prices?|costs?|net)(?![A-Za-z])", re.IGNORECASE)
ERROR = re.compile(r"#(N/A|REF!|VALUE!|DIV/0!|NAME\?|NUM!|NULL!|SPILL!|CALC!|GETTING_DATA)", re.IGNORECASE)


def value(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(round(v, 6))
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time() == time() else v.isoformat(" ", "minutes")
    if isinstance(v, (date, time)):
        return v.isoformat()
    if isinstance(v, timedelta):
        return str(v)
    text = " ".join(str(v).split())
    return "" if ERROR.fullmatch(text) else text


def _labels(cells):
    """The values that can be headings: not a label merged down the side."""
    return [v for v, down in cells if v and not down]


def _names(cells):
    return [v if not down else "" for v, down in cells]


def page(name, table):
    """table: rows of ([(value, merged down), ...], has a heading merged across). The page's text, or "" for nothing."""
    width = max((len(cells) for cells, _ in table), default=0)
    rows = [(cells + [("", False)] * (width - len(cells)), across) for cells, across in table]
    rows = [(cells, across) for cells, across in rows if any(v for v, _ in cells)]
    used = [c for c in range(width) if any(cells[c][0] for cells, _ in rows)]
    rows = [([cells[c] for c in used], across) for cells, across in rows]
    lines, names, n = [], None, 0
    while n < len(rows):  # the titles, then the headings
        cells, across = rows[n]
        n += 1
        if len(set(_labels(cells))) >= 2:
            names = _names(cells)
            depth = 1
            # a group row: the next row joins it (and the one after, when that row is a group too)
            while across and depth < MAX_HEADING_ROWS and n < len(rows) and len(set(_labels(rows[n][0]))) >= 2:
                under, across = _names(rows[n][0]), rows[n][1]
                names = [a if a == b else " ".join(x for x in (a, b) if x) for a, b in zip(names, under)]
                n += 1
                depth += 1
            break
        title = " ".join(dict.fromkeys(_labels(cells) or [v for v, _ in cells if v]))
        if title and (not lines or lines[-1] != title):
            lines.append(title)
    priced = [bool(PRICE.search(h)) for h in names or []]
    for cells, _ in rows[n:]:
        labels = _labels(cells)
        known = {h.casefold() for h in names if h}
        if len(set(labels)) >= 2 and sum(v.casefold() in known for v in labels) * 2 >= len(labels):
            if any(v.casefold() not in known for v in labels):  # headings again, a little different: they take over
                names = _names(cells)
                priced = [bool(PRICE.search(h)) for h in names]
            continue
        filled = [(h, v) for h, (v, _), p in zip(names, cells, priced) if v and not p]
        if len([v for v, _ in cells if v]) == 1:  # a lone value mid-sheet is a sub-title
            filled = [("", v) for _, v in filled]
        line = ", ".join(v if not h or h == v else f"{h}: {v}" for h, v in filled)
        if line:
            lines.append(line)
    if not lines:
        return ""
    return "\n".join(([f"{SHEET}{' '.join(name.split())}"] if name else []) + lines)


def read_xlsx(data):
    import openpyxl
    from openpyxl.utils import column_index_from_string

    book = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    pages = []
    for sheet in book.worksheets[:MAX_SHEETS]:
        if sheet.sheet_state != "visible":
            continue
        last_row, last_col = min(sheet.max_row or 0, MAX_ROWS), min(sheet.max_column or 0, MAX_COLUMNS)
        grid = [[value(v) for v in row] for row in sheet.iter_rows(max_row=last_row, max_col=last_col, values_only=True)]
        down, across = set(), set()
        for merged in sheet.merged_cells.ranges:
            top, left = merged.min_row - 1, merged.min_col - 1
            if top >= len(grid) or left >= last_col:
                continue
            filler = grid[top][left]
            for r in range(top, min(merged.max_row, len(grid))):
                for c in range(left, min(merged.max_col, last_col)):
                    grid[r][c] = filler
                    if merged.max_row > merged.min_row and merged.max_col == merged.min_col:
                        down.add((r, c))
            if merged.max_col > merged.min_col:
                across.add(top)
        hidden_cols = set()
        for key, dim in sheet.column_dimensions.items():
            if dim.hidden:
                low = dim.min or column_index_from_string(key)
                hidden_cols.update(range(low - 1, dim.max or low))
        hidden_rows = {r - 1 for r, dim in sheet.row_dimensions.items() if dim.hidden}
        table = [([(v, (r, c) in down) for c, v in enumerate(row) if c not in hidden_cols], r in across)
                 for r, row in enumerate(grid) if r not in hidden_rows]
        text = page(sheet.title, table)
        if text:
            pages.append(text)
    return pages


def read_csv(data):
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = itertools.islice(csv.reader(io.StringIO(text), dialect), MAX_ROWS)
    text = page(None, [([(value(v), False) for v in row[:MAX_COLUMNS]], False) for row in rows])
    return [text] if text else []


def main():
    data = sys.stdin.buffer.read()
    pages = read_csv(data) if sys.argv[1] == "csv" else read_xlsx(data)
    sys.stdout.write(json.dumps({"pages": pages}))


def _limit_memory():
    """The memory limit the parent asks for (CHILD_MEMORY_BYTES), set before anything big is loaded."""
    import os
    import resource
    limit = int(os.environ.get("CHILD_MEMORY_BYTES") or 0)
    if limit:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


if __name__ == "__main__":
    _limit_memory()
    main()
