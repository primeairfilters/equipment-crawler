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
import statistics
import sys


MAX_PAGE_CHARS = 50_000


def _outside(boxes):
    def keep(obj):
        return not any(obj["x0"] >= x0 - 1 and obj["x1"] <= x1 + 1 and obj["top"] >= top - 1
                       and obj["bottom"] <= bottom + 1 for x0, top, x1, bottom in boxes)
    return keep


def find_tables(page):
    """Ruled tables that hold data: 2+ rows with 2+ filled cells. Boxes drawn around text (a brochure panel, a one-column
    submittal box, an empty notes grid) are not tables; their text is read as ordinary lines, once."""
    kept = []
    for table in page.find_tables():
        try:
            rows = table.extract()
        except Exception:
            continue
        if sum(1 for row in rows if sum(1 for cell in row if cell and cell.strip()) >= 2) >= 2:
            kept.append(table)
    return kept


def page_text(page):
    """Text outside ruled tables, then each ruled table as tab-separated rows (empty cells kept)."""
    try:
        tables = find_tables(page)
        if not tables:
            return page.extract_text() or ""
        parts = [page.filter(_outside([t.bbox for t in tables])).extract_text() or ""]
        for table in tables:
            parts.append("\n".join("\t".join((cell or "").replace("\n", " ") for cell in row)
                                   for row in table.extract()))
        return "\n".join(parts)
    except Exception:
        return page.extract_text() or ""


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
    boxes = [t.bbox for t in tables]
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
    for table in sorted(tables, key=lambda t: t.bbox[1]):
        try:
            rows = [[(cell or "").replace("\n", " ") for cell in row] for row in table.extract()]
        except Exception:
            continue
        at = next((n for n, i in enumerate(items) if i["kind"] == "text" and i["top"] > table.bbox[1]), len(items))
        items.insert(at, {"kind": "table", "rows": rows, "top": round(table.bbox[1], 1)})
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


def main():
    max_pages = int(sys.argv[1])
    layout = len(sys.argv) > 2 and sys.argv[2] == "layout"
    import pdfplumber
    _clip_aware()
    next_page = _stable_ids()

    data = sys.stdin.buffer.read()
    pages, layouts, rows = [], [], []
    with pdfplumber.open(io.BytesIO(data), **({"laparams": {}} if layout else {})) as pdf:
        for page in pdf.pages[:max_pages]:
            pages.append(page_text(page)[:MAX_PAGE_CHARS])
            try:
                rows.append(drawn_rows(page))
            except Exception:
                rows.append((0, 0))
            if layout:
                layouts.append(page_layout(page))
            page.close()  # drop the page's parsed objects: a 37 MB manual peaked at 1.6 GB kept, 0.2 GB closed
            next_page()
    out = {"pages": pages, "rows": rows}
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
