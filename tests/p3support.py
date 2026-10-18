"""Helpers shared by the P3 tests: pages, a tiny real PDF, answers for the fake AI."""
import hashlib
import uuid


def q(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def new_brand(conn):
    """A brand whose alias key is a clean unique word, so manufacturer rows never collide."""
    from crawler.brands import add_brand
    name = "Zz" + uuid.uuid4().hex[:10]
    return name, add_brand(conn, name)


def _host_id(conn, host="p3.test"):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO hosts (host) VALUES (%s) ON DUPLICATE KEY UPDATE host = host", (host,))
    return q(conn, "SELECT id FROM hosts WHERE host = %s", (host,))[0][0]


def make_page(conn, brand_id, body, *, is_pdf=False, url=None):
    """A pages row for a fetched document, with its text stored and filtered (index_page)."""
    from crawler.textextract import index_page
    url = url or f"http://p3.test/{uuid.uuid4().hex}"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO pages (brand_id, host_id, url, url_sha, is_pdf, http_status, fetched_at) "
                    "VALUES (%s, %s, %s, SHA2(%s, 256), %s, 200, NOW())",
                    (brand_id, _host_id(conn), url, url, 1 if is_pdf else 0))
        page_id = cur.lastrowid
    index_page(conn, page_id, body, is_pdf=is_pdf)
    return page_id


def make_doc(conn, brand_id, texts):
    """A pages row whose page_text rows are inserted directly, one per text (status waiting)."""
    url = f"http://p3.test/{uuid.uuid4().hex}"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO pages (brand_id, host_id, url, url_sha, is_pdf, http_status, fetched_at) "
                    "VALUES (%s, %s, %s, SHA2(%s, 256), 1, 200, NOW())",
                    (brand_id, _host_id(conn), url, url))
        page_id = cur.lastrowid
        for n, text in enumerate(texts, start=1):
            cur.execute("INSERT INTO page_text (page_id, page_number, title, text_sha, body) VALUES (%s, %s, %s, %s, %s)",
                        (page_id, n, "doc", hashlib.sha256(text.encode()).hexdigest(), text))
    return page_id


def spec_html(model, seer2="15.2", tonnage="2.0", note=""):
    return (f"<html><head><title>{model} spec sheet</title></head><body><p>{note}</p><table>"
            f"<tr><th>Model</th><td>{model}</td></tr>"
            f"<tr><th>SEER2</th><td>{seer2}</td></tr>"
            f"<tr><th>Tonnage</th><td>{tonnage}</td></tr>"
            f"</table></body></html>").encode()


def answer(model, seer2="15.2", tonnage="2.0", base=False):
    """The structured output the fake AI returns for a document."""
    return {"models": [{
        "model_number": model,
        "is_base_model": base,
        "specs": [
            {"key": "seer2", "value": seer2, "unit": None},
            {"key": "tonnage", "value": tonnage, "unit": "tons"},
        ],
    }]}


def responder_for(known):
    """known: {model: answer-dict-for-that-model}. Answers with every known model found in the request text."""
    def respond(request):
        flat = request.text.replace(" ", "")
        models = []
        for model, out in known.items():
            if model in flat:
                models.extend(out["models"])
        return {"models": models}
    return respond


def settle(conn, client, brand, **kwargs):
    """Run extraction passes until a submit and its collection have both had a chance to happen."""
    from crawler.extract import extract_pass
    for _ in range(3):
        extract_pass(conn, client, brand=brand, **kwargs)


def make_pdf(pages):
    """A small valid PDF. pages is a list of pages, each a list of text lines."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>"}
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode()
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    for i, lines in enumerate(pages):
        ops = ["BT", "/F1 12 Tf", "14 TL", "72 720 Td"] + [f"({esc(line)}) Tj T*" for line in lines] + ["ET"]
        stream = "\n".join(ops).encode()
        objects[5 + 2 * i] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        objects[4 + 2 * i] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {5 + 2 * i} 0 R "
                              f"/Resources << /Font << /F1 3 0 R >> >> >>").encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objects[num] + b"\nendobj\n"
    xref = len(out)
    total = max(objects) + 1
    out += f"xref\n0 {total}\n".encode() + b"0000000000 65535 f \n"
    for num in range(1, total):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {total} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
