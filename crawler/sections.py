"""Sections: a document cut by rules into pieces Breeze can quote, each with its pages and heading path (D41, P12).

Free and deterministic: the same lines always give the same sections.
- Running headers and footers are dropped first: a line among the first or last two of a page that repeats there
  on at least two pages and half of all pages (digits and web addresses ignored when comparing). So is a page number
  there: a bare number ("7", "Page 7", "7 of 16") that is the page's own number, or is off from it by the same amount
  on 2+ pages (numbering that starts after a cover). A page left holding only "Notes" or "intentionally left blank"
  gives nothing.
- A heading is a short line in a larger font than the body (or a short bold capitals line), never a form label
  ending in ':', never garbled glyphs (no pronounceable letter run, or an unknown-glyph mark), never a spec value ('Nominal Sizes: 2 to 5 Ton'), never the end of a sentence, never a bullet. Back-to-back heading lines of one size are one heading
  wrapped over up to three lines; joined, they must still be short, so a big-font paragraph or a run of labels is text.
  A heading already on the path (a chapter title reprinted on each page) is not a new topic. A warning box's caption
  (WARNING, CAUTION, NOTICE, NOTE, DANGER…, alone or as "WARNING: Freeze Hazard") stays with its warning as text, and a
  title has 3+ letters, and its first word is not all lowercase (a wrapped line's middle; "iM Air" is fine). Hazard
  icon labels (HOT, BURN, CHAUDE) count as captions too.
  Heading sizes rank into levels, which build the path (Refrigerant Charging › Subcooling).
- A cover title (a page-1 heading larger than every heading on later pages) stays in the text but out of the path:
  the document's tagged title already says what it is, and a title in every path only repeats the same words.
- A table is its own section in markdown, split only between rows, repeating its header rows (all rows before the first
  row of numbers, for a chart with a title, labels and column names over its data). A short note right under
  it (no heading between, under NOTE_WORDS) goes with it: "*Crankcase heater recommended", "[ ] Designates Metric";
  else a short block right before it opens it (a column of labels outside the ruling).
- Text aims at MIN_WORDS..MAX_WORDS: a short section merges into the next, a long one splits at a sentence end. It
  merges into its own subsections and into its next sibling (then under their parent's path, empty at the top level),
  never across a higher heading: one topic, a true path.
- With no headings at all, text falls back to overlapping windows.
- A section (text or table) whose common words clearly say en, es or fr is flagged lang:xx (a manual's Spanish and French halves),
  and a short block never merges into one in another language.
- Text that reads like instructions to an AI is flagged; Breeze treats every body as quoted data anyway (D53).
"""
import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field

SECTIONER_VERSION = "17"
MIN_WORDS = 300
MAX_WORDS = 800
WINDOW_WORDS = 500
OVERLAP_WORDS = 50
NOTE_WORDS = 60
HEADING_RATIO = 1.15
MAX_HEADING_WORDS = 12
MAX_HEADING_CHARS = 90
# a contents page's title is often bigger than the chapter titles; it heads the contents page only, never a chapter
_CONTENTS = re.compile(r"(table of )?contents|table des mati[eè]res|sommaire|[íi]ndice|(tabla de )?contenidos?",
                       re.IGNORECASE)
MAX_HEADING_LINES = 3  # a heading wraps over at most this many lines; a longer run of short lines is labels or a list

_STOPWORDS_BY_LANGUAGE = {
    "en": {"the", "and", "of", "to", "is", "be", "for", "with", "are", "this", "that", "from", "or", "must", "before"},
    "es": {"el", "los", "las", "del", "que", "y", "para", "con", "por", "una", "unidad", "debe", "antes", "se", "lea"},
    "fr": {"le", "les", "des", "du", "et", "pour", "avec", "une", "est", "pas", "dans", "sur", "doit", "avant", "au"},
}
LANGUAGE_MIN_WORDS = 30


def _language(text):
    """'en', 'es' or 'fr' when common words clearly say so (30+ words, the winner at least twice the next and 8% of
    the words), else None. Ruud IOMs put Spanish and French halves after the English one."""
    tokens = re.findall(r"[a-zà-ÿ]+", text.lower())
    if len(tokens) < LANGUAGE_MIN_WORDS:
        return None
    counts = sorted(((sum(t in words for t in tokens), lang) for lang, words in _STOPWORDS_BY_LANGUAGE.items()),
                    reverse=True)
    (best, lang), (second, _) = counts[0], counts[1]
    return lang if best >= 2 * second and best >= 0.08 * len(tokens) else None


_INSTRUCTIONS = re.compile(
    r"\b(ignore|disregard|forget)\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)\s+instructions\b|"
    r"\byou are (now )?(an? )?(ai|assistant|chatbot|language model)\b|\bsystem prompt\b|\bnew instructions\s*:", re.I)
_SENTENCE_END = re.compile(r"[.!?:;][\"')\]]*$")
_STOPWORDS = {"A", "AN", "THE", "AND", "OR", "OF", "TO", "IN", "ON", "IS", "IT", "IF", "AT", "BY", "BE", "DO", "NO",
              "NOT", "FOR", "ARE", "AS", "SO", "UP", "WE", "US", "MAY", "ALL", "ANY", "PER", "SEE", "USE", "ONE",
              "TWO", "OUT", "OFF", "ON", "YES", "MIN", "MAX", "QTY", "REF", "NEW", "OLD"}


@dataclass
class Section:
    page_from: int
    page_to: int
    heading_path: str
    kind: str  # text | table
    body: str
    flags: list = field(default_factory=list)

    @property
    def body_sha(self):
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()

    @property
    def token_count(self):
        return -(-len(self.body) // 4)  # about four characters a token; exact counts come with the embedding model


def _words(text):
    return len(text.split())


_ADDRESS = re.compile(r"(https?://|www\.)\S+", re.I)
_CALLOUT = re.compile(r"^(?:(?:warning|caution|notice|note|notes|danger|important|attention|avertissement|advertencia|"
                      r"precauci[oó]n|aviso|mise en garde|hot|burn|chaude|br[uû]lure|caliente)[\s!:.]*)+$", re.I)  # a warning box's caption: text, not a topic
_CALLOUT_TITLE = re.compile(r"^(?:warning|caution|notice|note|danger|important|attention|avertissement|advertencia|"
                            r"precauci[oó]n|aviso)\s*[:!\-–—]", re.I)  # "WARNING: Refrigeration System under High pressure"
_WORDLIKE = re.compile(r"(?=[A-Za-z]*[AEIOUYaeiouy])[A-Za-z]{2,}")  # a pronounceable run: "HVAC" yes, "WTT" no
_SPEC_VALUE = re.compile(r":\s*(.*\d|yes\b|no\b)", re.I)  # "Nominal Sizes: 2 to 5 Ton", "EcoNet Enabled: Yes"
_BULLET = re.compile(r"^(o|[•·▪■◦●○–—\-*])\s")
EDGE_LINES = 2
_PAGE_NUMBER = re.compile(r"^[\s\-–—]*(?:page\s*)?(\d{1,4})(?:\s*(?:of|/)\s*\d{1,4})?[\s\-–—]*$", re.I)
_BLANK_PAGE = re.compile(r"^(notes?|memo|(this page )?(is )?intentionally left blank)[\s:.]*$", re.I)


def _norm(text):
    return re.sub(r"[\d\s]+", " ", _ADDRESS.sub(" ", text.lower())).strip()


# ---------------------------------------------------------------- page furniture and headings

def _edges(page):
    """Indexes of the first and last EDGE_LINES text lines of a page: where running headers and footers sit."""
    texts = [n for n, i in enumerate(page) if i["kind"] == "text"]
    return set(texts[:EDGE_LINES] + texts[-EDGE_LINES:])


def _page_numbers(pages):
    """(page index, item index) of each page number at a page edge: the page's own number, numbering that starts after
    a cover (the same offset on 2+ pages), or numbering that counts down (a flip-book's back half: number plus page the
    same on 3+ pages)."""
    found = []
    for p, page in enumerate(pages):
        for n in _edges(page):
            match = _PAGE_NUMBER.match(page[n]["text"])
            if match:
                found.append((p, n, int(match.group(1)) - (p + 1), int(match.group(1)) + (p + 1)))
    offsets = Counter(off for _, _, off, _ in found)
    sums = Counter(total for _, _, _, total in found)
    return {(p, n) for p, n, off, total in found if off == 0 or offsets[off] >= 2 or sums[total] >= 3}


def _blank(page):
    texts = [i for i in page if i["kind"] == "text"]
    return bool(texts) and len(texts) == len(page) and all(_BLANK_PAGE.match(" ".join(i["text"].split())) for i in texts)


def _drop_furniture(pages):
    seen = Counter()
    for page in pages:
        seen.update({_norm(page[n]["text"]) for n in _edges(page) if _norm(page[n]["text"])})
    furniture = {k for k, n in seen.items() if n >= 2 and n * 2 >= len(pages)} if len(pages) >= 2 else set()
    numbers = _page_numbers(pages)
    out = [[i for n, i in enumerate(page)
            if not (n in _edges(page) and (_norm(i["text"]) in furniture or (p, n) in numbers))]
           for p, page in enumerate(pages)]
    return [[] if _blank(page) else page for page in out]


def _body_size(pages):
    sizes = Counter()
    for page in pages:
        for i in page:
            if i["kind"] == "text":
                sizes[round(float(i.get("size") or 0), 1)] += len(i["text"])
    return sizes.most_common(1)[0][0] if sizes else 0


def _heading_size(item, body, continuation=False):
    """The size a heading counts as, or None for a body line. continuation: the line follows a heading line of its size,
    so it may start in lowercase (a wrapped title or paragraph goes on)."""
    text = " ".join(item["text"].split())
    if not text or len(text) > MAX_HEADING_CHARS or _words(text) > MAX_HEADING_WORDS:
        return None
    if (item.get("body") or _BULLET.match(text) or _SPEC_VALUE.search(text) or _CALLOUT.match(text)
            or _CALLOUT_TITLE.match(text)):
        return None
    first = re.match(r"[^\W\d_]+", text)
    if first and first.group().islower() and not continuation:
        return None  # the middle of a wrapped sentence ("and condensate neutralizer…"), not a title
    if sum(c.isalpha() for c in text) < 3:  # icon and numeral debris ("Ii", "II") is no title
        return None
    if text.endswith((":", ",", ";", ".")) or not _WORDLIKE.search(text) or "\ufffd" in text or _ADDRESS.search(text):
        return None
    if sum(c.isdigit() for c in text) > len(text) / 2:
        return None
    size = round(float(item.get("size") or 0), 1)
    if body and size >= body * HEADING_RATIO:
        return size
    if item.get("bold") and size >= body and text.upper() == text and _words(text) <= 8:
        return body  # a bold capitals line at body size: the lowest heading level
    return None


def _join_wrapped(pages, body):
    """Back-to-back heading lines of one size become one heading line; if the joined text is too long to be a heading,
    every line of the run is marked as body text (a big-font paragraph, a run of form labels)."""
    out = []
    for page in pages:
        items, run = [], []

        def close():
            if len(run) > 1:
                joined = dict(run[0], text=" ".join(" ".join(i["text"].split()) for i in run),
                              bold=any(i.get("bold") for i in run))
                if len(run) <= MAX_HEADING_LINES and _heading_size(joined, body) is not None:
                    items.append(joined)
                else:
                    items.extend(dict(i, body=True) for i in run)
            else:
                items.extend(run)
            run.clear()

        for item in page:
            size = _heading_size(item, body) if item["kind"] == "text" else None
            if size is None and run and item["kind"] == "text":
                size = _heading_size(item, body, continuation=True)
            if size is None or (run and _heading_size(run[0], body) != size):
                close()
            if size is None:
                items.append(item)
            else:
                run.append(item)
        close()
        out.append(items)
    return out


def _drop_cover_title(pages, body):
    """Page-1 headings larger than every heading on later pages become body text (they need later headings to compare)."""
    later = [s for page in pages[1:] for i in page if i["kind"] == "text" for s in [_heading_size(i, body)] if s is not None]
    if not pages or not later:
        return pages
    top = max(later)
    first = [dict(i, body=True) if i["kind"] == "text" and (_heading_size(i, body) or 0) > top else i for i in pages[0]]
    return [first] + pages[1:]


# ---------------------------------------------------------------- tables

def _cell(value):
    return " ".join(str(value or "").split()).replace("|", "\\|")


_NUMBER = re.compile(r"^[\d.,%°+\-–/ ]*\d[\d.,%°+\-–/ ]*$")
MAX_HEADER_ROWS = 6


def _header_rows(rows, width):
    """How many leading rows are the header: every row before the first data row (half the columns filled, half of
    those numbers), at most MAX_HEADER_ROWS and leaving a data row; else just the first row. A capacity chart's header is
    a title, a label, a row of outdoor temperatures and rows of column names before its numbers start."""
    for n, row in enumerate(rows[:MAX_HEADER_ROWS + 1]):
        filled = [c for c in row if c]
        if n and len(filled) * 2 >= width and sum(bool(_NUMBER.match(c)) for c in filled) * 2 >= len(filled):
            return n
    return 1


def _table_sections(rows, path, page):
    rows = [[_cell(c) for c in r] for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    top = _header_rows(rows, width)
    head = (["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
            + ["| " + " | ".join(r) + " |" for r in rows[1:top]])
    budget = MAX_WORDS - _words("\n".join(head))
    out, chunk, used = [], [], 0
    for r in rows[top:]:
        line = "| " + " | ".join(r) + " |"
        n = _words(line)
        if chunk and used + n > budget:
            out.append(chunk)
            chunk, used = [], 0
        chunk.append(line)
        used += n
    if chunk or not out:
        out.append(chunk)
    return [Section(page, page, path, "table", "\n".join(head + c)) for c in out]


# ---------------------------------------------------------------- text blocks

def _split_long(block):
    """Split (lines with pages) into pieces of at most MAX_WORDS, preferring a sentence end."""
    pieces, current, count = [], [], 0
    for text, page in block:
        n = _words(text)
        if current and count + n > MAX_WORDS:
            cut = max((i for i, (t, _) in enumerate(current) if _SENTENCE_END.search(t)), default=len(current) - 1)
            pieces.append(current[:cut + 1])
            current = current[cut + 1:]
            count = sum(_words(t) for t, _ in current)
        current.append((text, page))
        count += n
    if current:
        pieces.append(current)
    return pieces


def _text_section(lines, path):
    return Section(lines[0][1], lines[-1][1], path, "text", "\n".join(t for t, _ in lines))


def _windows(lines):
    """Overlapping windows of about WINDOW_WORDS, cut between lines, for documents with no headings."""
    out, i = [], 0
    while i < len(lines):
        j, count = i, 0
        while j < len(lines) and (count < WINDOW_WORDS or j == i):
            count += _words(lines[j][0])
            j += 1
        out.append(lines[i:j])
        if j >= len(lines):
            break
        back, k = 0, j
        while k > i + 1 and back < OVERLAP_WORDS:
            k -= 1
            back += _words(lines[k][0])
        i = k
    return out


def build_sections(pages):
    """pages: one list per page of {"kind": "text", "text", "size", "bold"} or {"kind": "table", "rows"} in reading order."""
    pages = _drop_furniture(pages)
    body = _body_size(pages)
    pages = _drop_cover_title(_join_wrapped(pages, body), body)
    sizes = sorted({s for page in pages for i in page if i["kind"] == "text"
                    for s in [_heading_size(i, body)] if s is not None}, reverse=True)
    level = {s: n for n, s in enumerate(sizes)}

    raw, stack, block = [], [], []  # raw: ("text", lines, path) or ("table", sections)

    def path():
        return " › ".join(text for _, text in stack)

    def flush():
        if block:
            raw.append(("text", list(block), path(), stack[-1][0] if stack else None))  # last: its heading's level
            block.clear()

    for number, page in enumerate(pages, start=1):
        for item in page:
            if item["kind"] == "table":
                flush()
                raw.append(("table", _table_sections(item["rows"], path(), number)))
                continue
            text = " ".join(item["text"].split())
            if not text:
                continue
            size = _heading_size(item, body) if sizes else None
            if size is not None:
                if any(same.casefold() == text.casefold() for _, same in stack):
                    continue  # a heading already on the path (a chapter title reprinted on the next page): not a new topic
                flush()
                stack[:] = [entry for entry in stack if not _CONTENTS.fullmatch(entry[1])]  # a contents page ends here
                while stack and stack[-1][0] >= level[size]:
                    stack.pop()
                stack.append((level[size], text))
                raw.append(("heading", text, path(), level[size]))
                continue
            block.append((text, number))
    flush()
    raw = _notes_to_tables(raw)

    if not sizes:  # no headings anywhere: windows over all the text, tables kept whole where they were
        lines = [line for kind, *rest in raw if kind == "text" for line in rest[0]]
        out = [_text_section(w, "") for w in _windows(lines)]
        out += [s for kind, *rest in raw if kind == "table" for s in rest[0]]
        return _flagged(sorted(out, key=lambda s: (s.page_from, s.kind == "table")))

    # merge a short text block into the next text block (its heading kept as a line), then split long ones. It merges
    # into its own subsections, and into the next sibling (same level) under their parent's path; never across a higher
    # heading (a new topic)
    merged, carry = [], None  # carry: a short block waiting to merge forward (see _Carry)
    for entry in raw:
        if entry[0] == "heading":
            if carry is not None and carry.level is not None and entry[3] < carry.level:
                merged.append(carry.alone())
                carry = None
            elif carry is not None:
                carry.headings.append(entry[1])
                if carry.level is not None and entry[3] == carry.level:
                    carry.lifted = _parent(entry[2])  # a sibling: if they merge, under the parent's path
            continue
        if entry[0] == "table":
            if carry is not None:
                merged.append(carry.alone())
                carry = None
            merged.append(entry)
            continue
        lines, here, depth = entry[1], entry[2], entry[3]
        if carry is not None:
            together = carry.lines + [(h, carry.lines[-1][1]) for h in carry.headings] + lines
            ours, theirs = _language(" ".join(t for t, _ in carry.lines)), _language(" ".join(t for t, _ in lines))
            if sum(_words(t) for t, _ in together) <= MAX_WORDS and not (ours and theirs and ours != theirs):
                if carry.level is not None:  # text before the first heading takes the next heading
                    here, depth = (carry.path if carry.lifted is None else carry.lifted), carry.level
                lines = together
            else:
                merged.append(carry.alone())
            carry = None
        if sum(_words(t) for t, _ in lines) < MIN_WORDS:
            carry = _Carry(list(lines), here, depth)
        else:
            merged.append(("text", lines, here))
    if carry is not None:
        merged.append(carry.alone())

    out = []
    for entry in merged:
        if entry[0] == "table":
            out += entry[1]
        else:
            out += [_text_section(piece, entry[2]) for piece in _split_long(entry[1])]
    return _flagged(out)


@dataclass
class _Carry:
    """A short text block waiting to merge into the next one. The headings met on the way and a sibling's lifted path
    only count if the merge happens; alone, it keeps its own lines and path."""
    lines: list
    path: str
    level: int = None  # its heading's level, or None before any heading
    headings: list = field(default_factory=list)
    lifted: str = None

    def alone(self):
        return ("text", self.lines, self.path)


def _parent(path):
    return path.rsplit(" › ", 1)[0] if " › " in path else ""


def _notes_to_tables(raw):
    """A short text block right after a table (no heading between) is that table's note: append it to the table.
    Otherwise, right before a table, it is the table's lead-in (a column of labels outside the ruling): prepend it."""
    out = []
    for n, entry in enumerate(raw):
        short = entry[0] == "text" and sum(_words(t) for t, _ in entry[1]) < NOTE_WORDS
        if short and out and out[-1][0] == "table" and out[-1][1]:
            last = out[-1][1][-1]
            last.body += "\n\n" + "\n".join(t for t, _ in entry[1])
            last.page_to = max(last.page_to, entry[1][-1][1])
            continue
        if short and n + 1 < len(raw) and raw[n + 1][0] == "table" and raw[n + 1][1]:
            first = raw[n + 1][1][0]
            first.body = "\n".join(t for t, _ in entry[1]) + "\n\n" + first.body
            first.page_from = min(first.page_from, entry[1][0][1])
            continue
        out.append(entry)
    return out


def _flagged(sections):
    for s in sections:
        lang = _language(s.body)
        if lang and f"lang:{lang}" not in s.flags:
            s.flags.append(f"lang:{lang}")  # kb.build stores it in place of the document's language
        if _INSTRUCTIONS.search(s.body) and "instructions" not in s.flags:
            s.flags.append("instructions")
    return sections


# ---------------------------------------------------------------- terms (D50)

def terms(text):
    """Code-like tokens, normalised to capitals and digits: error codes (E4, LO PS -> LOPS), part and model numbers."""
    found = set()
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-/]*[A-Za-z0-9]|[A-Za-z0-9]", text)
    for i, tok in enumerate(tokens):
        key = re.sub(r"[^A-Z0-9]", "", tok.upper())
        if not key or len(key) > 60:
            continue
        has_letter, has_digit = bool(re.search(r"[A-Z]", key)), bool(re.search(r"\d", key))
        if has_letter and has_digit:
            found.add(key)
        elif has_digit and not has_letter and (len(key) >= 7 or ("-" in tok and len(key) >= 6)):
            found.add(key)
        if i + 1 < len(tokens):
            nxt = tokens[i + 1]
            if (re.fullmatch(r"[A-Z]{1,3}", tok) and re.fullmatch(r"[A-Z]{1,3}", nxt)
                    and tok not in _STOPWORDS and nxt not in _STOPWORDS):
                found.add(tok + nxt)
    return found
