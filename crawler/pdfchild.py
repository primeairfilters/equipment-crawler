"""Reads a PDF from stdin and prints its page texts as JSON. Run only as a child process with limits.

`pdfchild.py <max_pages>` prints {"pages": [text, ...]}. `pdfchild.py <max_pages> layout` also prints
{"layout": [[item, ...] per page]}: text lines with their font size and boldness, and ruled tables as rows, in
reading order (for the sectioner, P12). Both print {"rows": [[drawn, real] per page]}: lines of text drawn as shapes
and lines of real text, so the parent can tell a text layer that holds only fragments.

Text outside its clipping path is not read: a viewer never shows it, but pdfminer ignores clips, so a hidden copy
(seen in a Ruud spec sheet: a table's text clipped away under another table) would mix into what is shown.
"""
import io
import json
import re
import statistics
import sys


MAX_PAGE_CHARS = 50_000


def _outside(boxes):
    def keep(obj):
        return not any(obj["x0"] >= x0 - 1 and obj["x1"] <= x1 + 1 and obj["top"] >= top - 1
                       and obj["bottom"] <= bottom + 1 for x0, top, x1, bottom in boxes)
    return keep


def find_tables(page):
    """Ruled tables that hold data, as (bbox, rows): 2+ rows with 2+ filled cells. Boxes drawn around text (a brochure
    panel, a one-column submittal box, an empty notes grid) are not tables; their text is read as ordinary lines, once.
    Found and read once a page, for its text and its layout: reading a table checks every cell against every character
    on the page, and dense spec sheets took 6-8 s a page reading each table four times (parse issues 2026-11-05)."""
    kept = getattr(page, "_ruled_tables", None)
    if kept is not None:
        return kept
    kept = []
    for table in page.find_tables():
        try:
            rows = table.extract()
        except Exception:
            continue
        if sum(1 for row in rows if sum(1 for cell in row if cell and cell.strip()) >= 2) >= 2:
            kept.append((table.bbox, rows))
    page._ruled_tables = kept
    return kept


GLUED_RUN = 20  # letters in a row: longer is words run together
SPACING_RATIO = 0.15  # word gap as a share of the font size, for a page whose words come out glued


def _glued(text):
    """The share of letter runs that are words run together ("Theheatexchangerhasasmallamount")."""
    runs = [r for r in re.findall(r"[A-Za-z]+", text) if len(r) >= 4]
    return sum(len(r) > GLUED_RUN for r in runs) / len(runs) if len(runs) >= 10 else 0.0


def _text(page):
    """pdfplumber's text. Words set apart by kerning rather than spaces, closer than its fixed 3-point tolerance, come
    out glued (Ruud's condensing boiler manuals: 2.2 points at 10-point type, parse issue 2026-10-30); such a page is
    read again with a tolerance that scales with the font, and kept if that reads more words. Other pages are left
    as they were."""
    text = page.extract_text() or ""
    if _glued(text) < 0.1:
        return text
    spaced = page.extract_text(x_tolerance_ratio=SPACING_RATIO) or ""
    return spaced if _glued(spaced) < _glued(text) else text


def page_text(page):
    """Text outside ruled tables, then each ruled table as tab-separated rows (empty cells kept)."""
    try:
        tables = find_tables(page)
        if not tables:
            return _text(page)
        parts = [_text(page.filter(_outside([bbox for bbox, _ in tables])))]
        for _, rows in tables:
            parts.append("\n".join("\t".join((cell or "").replace("\n", " ") for cell in row) for row in rows))
        return "\n".join(parts)
    except Exception:
        return _text(page)


def _glyph_run(shapes, gap=10):
    """The longest run of shapes along a line with at most `gap` points between neighbours."""
    shapes = sorted(shapes, key=lambda s: s["x0"])
    best, run = [], shapes[:1]
    for shape in shapes[1:]:
        if shape["x0"] - run[-1]["x1"] <= gap:
            run.append(shape)
        else:
            best, run = max(best, run, key=len), [shape]
    return max(best, run, key=len)


def drawn_rows(page):
    """(lines of text drawn as shapes, lines of real text) on the page. A drawn line is a run of 15+ letter-sized
    filled outlines, 120+ points long, mostly of 8+ points each (a texture of simple specks is not), on a line that
    has no real characters. Some PDFs print text this way; their text layer then holds only fragments."""
    lines = {}
    for c in page.curves:
        if (2 < c["bottom"] - c["top"] < 14 and c["x1"] - c["x0"] < 14 and c["top"] >= 0 and c["bottom"] <= page.height
                and c["x0"] >= 0 and c["x1"] <= page.width):
            lines.setdefault(round(c["bottom"]), []).append(c)
    real = {round(ch["bottom"]) for ch in page.chars if ch["text"].strip()}
    drawn = 0
    for y, shapes in lines.items():
        run = _glyph_run(shapes)
        if (len(run) >= 15 and run[-1]["x1"] - run[0]["x0"] >= 120 and sum(len(s["pts"]) >= 8 for s in run) * 2 >= len(run)
                and not any(abs(y - r) < 3 for r in real)):
            drawn += 1
    return drawn, len(real)


def _box_lines(page, boxes):
    """Text lines in pdfminer's reading order: text boxes follow columns, so two columns are not read across.
    Each item: (top of its box, line text, characters). Lines inside a table's area are left to the table."""
    from pdfminer.layout import LTChar, LTTextBox, LTTextLine
    out = []
    for box in page.layout:
        if not isinstance(box, LTTextBox):
            continue
        box_top = page.height - box.y1
        for line in box:
            if not isinstance(line, LTTextLine):
                continue
            top, bottom = page.height - line.y1, page.height - line.y0
            if any(line.x0 >= x0 - 1 and line.x1 <= x1 + 1 and top >= t - 1 and bottom <= b + 1 for x0, t, x1, b in boxes):
                continue
            chars = [c for c in line if isinstance(c, LTChar) and c.get_text().strip()]
            if sum(not c.upright for c in chars) * 2 > len(chars):
                continue  # turned text: pdfminer reads it one letter per line, so _rotated_lines reads it
            text = " ".join(_line_text(line).split())
            if chars and text:
                out.append((box_top, text, [{"size": c.size, "fontname": c.fontname} for c in chars]))
    return out


TRACKING_EM = 0.3  # letters set further apart than this (in font sizes) are a word gap, not tracking


def _line_text(line):
    """A line's text. pdfminer adds a space wherever letters stand apart, so a display heading set with wide letter
    spacing reads "Avai l able Access ories". On a line that has real space characters, the words are already marked:
    there a space pdfminer added between two letters closer than TRACKING_EM is dropped."""
    from pdfminer.layout import LTAnno, LTChar
    objs = list(line)
    if not any(isinstance(o, LTChar) and o.get_text() == " " for o in objs):
        return line.get_text()
    out = []
    for n, obj in enumerate(objs):
        if isinstance(obj, LTAnno) and obj.get_text() == " " and 0 < n < len(objs) - 1:
            before, after = objs[n - 1], objs[n + 1]
            if (isinstance(before, LTChar) and isinstance(after, LTChar) and before.get_text().strip()
                    and after.get_text().strip() and after.x0 - before.x1 < TRACKING_EM * before.size):
                continue
        out.append(obj.get_text())
    return "".join(out)


def _rotated_lines(page):
    """Text turned a quarter turn (a landscape wiring diagram or clearance drawing on a portrait page), as lines in
    reading order: (line text, characters). pdfplumber's own grouping chains neighbouring labels a point or two apart
    into one line and interleaves their letters, so a line here is the characters on one baseline (within a point of
    its first), read along it; a gap over two letter sizes starts a new line, so labels along one edge stay apart.
    Characters drawn twice (fake bold) count once."""
    if all(c["upright"] for c in page.chars):
        return []  # most pages: no turned text, and dedupe_chars is slow on a full page
    out = []
    chars = page.filter(lambda o: o.get("object_type") != "char" or not o["upright"]).dedupe_chars().chars
    for up in (True, False):
        columns = []
        for char in sorted((c for c in chars if (c["matrix"][1] > 0) == up), key=lambda c: c["x0"]):
            if columns and char["x0"] - columns[-1][0]["x0"] <= 1:
                columns[-1].append(char)
            else:
                columns.append([char])
        for column in (columns if up else columns[::-1]):
            run, end = [], None
            for char in sorted(column, key=lambda c: -c["bottom"] if up else c["top"]):
                size = _turned_size(char)
                start = -char["bottom"] if up else char["top"]
                if end is not None and start - end > 2 * size:
                    out.append(run)
                    run = []
                elif run and (char["text"].isspace() or start - end > size / 4):
                    run.append(None)
                if not char["text"].isspace():
                    run.append(char)
                end = -char["top"] if up else char["bottom"]
            out.append(run)
    lines = []
    for run in out:
        chars = [c for c in run if c is not None]
        text = " ".join("".join(" " if c is None else c["text"] for c in run).split())
        if chars and text:
            lines.append((text, [{"size": _turned_size(c), "fontname": c["fontname"]} for c in chars]))
    return lines


def _turned_size(char):
    return round(char["x1"] - char["x0"], 1)  # a turned letter's height runs across the page


def page_layout(page):
    """Text lines (size = median character size, bold = most characters bold) in reading order, with each ruled table
    placed before the first text box that starts below it."""
    try:
        tables = find_tables(page)
    except Exception:
        tables = []
    boxes = [bbox for bbox, _ in tables]
    try:
        lines = _box_lines(page, boxes)
    except Exception:
        lines = []
    try:
        lines += [(page.height, text, chars) for text, chars in _rotated_lines(page)]
    except Exception:
        pass
    items, used = [], 0
    for box_top, text, chars in lines:
        if used > MAX_PAGE_CHARS:
            break
        used += len(text)
        bold = sum("bold" in (c["fontname"] or "").lower() for c in chars) * 2 > len(chars)
        items.append({"kind": "text", "text": text, "size": round(statistics.median(c["size"] for c in chars), 1),
                      "bold": bold, "top": round(box_top, 1)})
    for bbox, cells in sorted(tables, key=lambda t: t[0][1]):
        rows = [[(cell or "").replace("\n", " ") for cell in row] for row in cells]
        at = next((n for n, i in enumerate(items) if i["kind"] == "text" and i["top"] > bbox[1]), len(items))
        items.insert(at, {"kind": "table", "rows": rows, "top": round(bbox[1], 1)})
    return items


def _clip_aware():
    """Make pdfplumber read pages through an interpreter that follows rectangular clipping (re W n, saved and restored
    by q/Q, carried into forms) and a device that drops each character whose centre is outside the current clip.
    A clip that is not a rectangle counts as its bounding box, so it can only keep too much, never drop shown text."""
    import pdfplumber.page
    from pdfminer.pdfinterp import PDFPageInterpreter

    Device = pdfplumber.page.PDFPageAggregatorWithMarkedContent

    class ClipDevice(Device):
        clip = None

        def render_char(self, *args, **kwargs):
            adv = super().render_char(*args, **kwargs)
            if self.clip is not None and self.cur_item._objs:
                ch = self.cur_item._objs[-1]
                x, y = (ch.x0 + ch.x1) / 2, (ch.y0 + ch.y1) / 2
                x0, y0, x1, y1 = self.clip
                if not (x0 - 1 <= x <= x1 + 1 and y0 - 1 <= y <= y1 + 1):
                    self.cur_item._objs.pop()
            return adv

    class ClipInterpreter(PDFPageInterpreter):
        def init_state(self, ctm):
            super().init_state(ctm)
            self.clip = getattr(self, "_start_clip", None)
            self.clip_stack, self.pending_clip = [], False
            self.device.clip = self.clip

        def dup(self):
            other = super().dup()
            other._start_clip = self.clip
            return other

        def do_q(self):
            super().do_q()
            self.clip_stack.append(self.clip)

        def do_Q(self):
            super().do_Q()
            if self.clip_stack:
                self.clip = self.clip_stack.pop()
            self.device.clip = self.clip

        def do_Do(self, xobjid):
            super().do_Do(xobjid)
            self.device.clip = self.clip  # a form ran with its own interpreter

        def do_W(self):
            self.pending_clip = True

        do_W_a = do_W

        def _end_path(self):
            if not self.pending_clip:
                return
            self.pending_clip = False
            a, b, c, d, e, f = self.ctm
            points = []
            for segment in self.curpath:
                numbers = [v for v in segment[1:] if isinstance(v, (int, float))]
                points += [(a * x + c * y + e, b * x + d * y + f) for x, y in zip(numbers[::2], numbers[1::2])]
            if not points:
                return
            box = (min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points), max(y for _, y in points))
            if self.clip is not None:
                box = (max(box[0], self.clip[0]), max(box[1], self.clip[1]), min(box[2], self.clip[2]), min(box[3], self.clip[3]))
            self.clip = box
            self.device.clip = box

    def ends_path(base):  # pdfminer passes operands by argument count, so no extra (even default) arguments here
        def painter(self):
            self._end_path()
            base(self)
        return painter

    for name in ("do_n", "do_S", "do_f", "do_F", "do_f_a", "do_B", "do_B_a"):
        setattr(ClipInterpreter, name, ends_path(getattr(PDFPageInterpreter, name)))

    pdfplumber.page.PDFPageAggregatorWithMarkedContent = ClipDevice
    pdfplumber.page.PDFPageInterpreter = ClipInterpreter


def _capped_grouping():
    """pdfminer puts text boxes in reading order by comparing every box with every other, so its time grows with the
    square of the boxes: a Daikin spec sheet's pages of 1,400-1,700 small labels took 10-18 s each and the sheet ran
    past the time limit (parse issues 2026-11-05). A page of more than MAX_GROUPED_BOXES is read top to bottom instead,
    as a crowded page is; ordinary pages (under about 350 boxes) keep their columns."""
    import pdfminer.layout as layout
    group = layout.LTLayoutContainer.group_textboxes

    def capped(self, laparams, boxes):
        if len(boxes) > MAX_GROUPED_BOXES:
            return [layout.LTTextGroupLRTB(boxes)]
        return group(self, laparams, boxes)

    layout.LTLayoutContainer.group_textboxes = capped


def _stable_ids():
    """pdfminer's layout analysis breaks ties between equally distant text boxes by id(), a memory address, so lines at
    one height came out in a different order between reads (8 Ruud documents, 2026-10-28). Its module gets an id that
    numbers objects in the order first seen, which repeats from read to read."""
    import pdfminer.layout
    numbers, keep = {}, []

    def stable_id(obj):
        key = id(obj)
        if key not in numbers:
            numbers[key] = len(numbers)
            keep.append(obj)  # held, so a freed object's address is never reused for another
        return numbers[key]

    def next_page():
        """Let the page's objects go: the tie-break is within a page, and holding them undid page.close() (a 41 MB
        manual peaked at 719 MB instead of 548 MB)."""
        numbers.clear()
        keep.clear()

    pdfminer.layout.id = stable_id
    return next_page


MAX_GROUPED_BOXES = 500  # text boxes on a page put in reading order: 330 took 0.3 s, 1,400 took 10 s
MAX_DRAWN_PIECES = 100_000  # line pieces on a page; pdfplumber reads about 20,000 a second and holds them all


def _drawn_pieces(objects, depth=0):
    """The line pieces drawn on a page (objects: (count, get)), counted by pdfium in C, stopping once over the limit.
    Wiring diagrams in AAON, Addison and Mitsubishi manuals hold 100,000 to 1,170,000 (prod parse issues 53-66,
    2026-11-02); pdfplumber took up to 51 s and 1.6 GB on one such page."""
    import pypdfium2.raw as pdfium
    count, get = objects
    pieces = 0
    for n in range(count):
        obj = get(n)
        kind = pdfium.FPDFPageObj_GetType(obj)
        if kind == pdfium.FPDF_PAGEOBJ_PATH:
            pieces += max(pdfium.FPDFPath_CountSegments(obj), 0)
        elif kind == pdfium.FPDF_PAGEOBJ_FORM and depth < 5:
            pieces += _drawn_pieces((pdfium.FPDFFormObj_CountObjects(obj),
                                     lambda k, obj=obj: pdfium.FPDFFormObj_GetObject(obj, k)), depth + 1)
        if pieces > MAX_DRAWN_PIECES:
            break
    return pieces


def crowded(page):
    """True when a pdfium page has more line pieces than pdfplumber can read in time."""
    import pypdfium2.raw as pdfium
    objects = (pdfium.FPDFPage_CountObjects(page.raw), lambda k: pdfium.FPDFPage_GetObject(page.raw, k))
    return _drawn_pieces(objects) > MAX_DRAWN_PIECES


def plain_lines(page):
    """A crowded page's text lines read by pdfium: no tables and no clip check, but each line's size (median character
    size) and boldness (most characters 700 or heavier), as page_layout gives them. The size is the font's times the
    text's scale: Mitsubishi's manuals set a 1-point font scaled up to 12."""
    import math
    import pypdfium2.raw as pdfium
    text = page.get_textpage()
    lines, chars, scale = [], [], pdfium.FS_MATRIX()
    for n in range(text.count_chars()):
        code = pdfium.FPDFText_GetUnicode(text.raw, n)
        if code in (10, 13):
            if chars:
                lines.append(chars)
            chars = []
        elif code:
            pdfium.FPDFText_GetMatrix(text.raw, n, scale)
            chars.append((chr(code), pdfium.FPDFText_GetFontSize(text.raw, n) * math.hypot(scale.c, scale.d),
                          pdfium.FPDFText_GetFontWeight(text.raw, n) >= 700))
    if chars:
        lines.append(chars)
    out, used = [], 0
    for line in lines:
        words = "".join(c for c, _, _ in line).strip()
        if not words:
            continue
        if used > MAX_PAGE_CHARS:
            break
        used += len(words)
        out.append({"kind": "text", "text": words, "size": round(statistics.median(s for c, s, _ in line if not c.isspace()), 1),
                    "bold": sum(b for _, _, b in line) * 2 > len(line)})
    text.close()
    return out


def main():
    max_pages = int(sys.argv[1])
    layout = len(sys.argv) > 2 and sys.argv[2] == "layout"
    import pdfplumber
    import pypdfium2
    _clip_aware()
    _capped_grouping()
    next_page = _stable_ids()

    data = sys.stdin.buffer.read()
    pages, layouts, rows = [], [], []
    fast = pypdfium2.PdfDocument(data)
    with pdfplumber.open(io.BytesIO(data), **({"laparams": {}} if layout else {})) as pdf:
        total = len(pdf.pages)  # more than max_pages: the document was cut (a parse issue)
        for number, page in enumerate(pdf.pages[:max_pages]):
            quick = fast[number]
            if crowded(quick):
                items = plain_lines(quick)
                pages.append("\n".join(i["text"] for i in items)[:MAX_PAGE_CHARS])
                rows.append((0, len(items)))
                if layout:
                    layouts.append(items)
                quick.close()
                continue
            try:
                text = page_text(page)[:MAX_PAGE_CHARS]
                try:
                    row = drawn_rows(page)
                except Exception:
                    row = (0, 0)
                items = page_layout(page) if layout else None
            except Exception:
                # pdfminer cannot read this page at all (parse issues 101-105, 2026-11-05): pdfium reads its lines, so
                # one bad page no longer loses the whole PDF.
                items = plain_lines(quick)
                text = "\n".join(i["text"] for i in items)[:MAX_PAGE_CHARS]
                row = (0, len(items))
            quick.close()
            pages.append(text)
            rows.append(row)
            if layout:
                layouts.append(items)
            page.close()  # drop the page's parsed objects: a 37 MB manual peaked at 1.6 GB kept, 0.2 GB closed
            next_page()
    fast.close()
    out = {"pages": pages, "rows": rows, "page_total": total}
    if layout:
        out["layout"] = layouts
    sys.stdout.write(json.dumps(out))


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
