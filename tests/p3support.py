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
        cur.execute("INSERT INTO hosts (host) VALUES (%s) ON CONFLICT (host) DO NOTHING", (host,))
    return q(conn, "SELECT id FROM hosts WHERE host = %s", (host,))[0][0]


def make_page(conn, brand_id, body, *, is_pdf=False, url=None):
    """A pages row for a fetched document, with its text stored and filtered (index_page)."""
    from crawler.textextract import index_page
    url = url or f"http://p3.test/{uuid.uuid4().hex}"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO pages (brand_id, host_id, url, url_sha, is_pdf, http_status, fetched_at, content_sha) "
                    "VALUES (%s, %s, %s, encode(sha256(convert_to(%s, 'UTF8')), 'hex'), %s, 200, NOW(), %s) RETURNING id",
                    (brand_id, _host_id(conn), url, url, is_pdf, hashlib.sha256(body).hexdigest()))
        page_id = cur.fetchone()[0]
    index_page(conn, page_id, body, is_pdf=is_pdf)
    return page_id


def make_doc(conn, brand_id, texts):
    """A pages row whose page_text rows are inserted directly, one per text (status waiting)."""
    url = f"http://p3.test/{uuid.uuid4().hex}"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO pages (brand_id, host_id, url, url_sha, is_pdf, http_status, fetched_at, content_sha) "
                    "VALUES (%s, %s, %s, encode(sha256(convert_to(%s, 'UTF8')), 'hex'), true, 200, NOW(), %s) RETURNING id",
                    (brand_id, _host_id(conn), url, url, hashlib.sha256("\f".join(texts).encode()).hexdigest()))
        page_id = cur.fetchone()[0]
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


def spec_pdf(model, seer2="15.2", tonnage="2.0"):
    """The bytes of a one-page spec-sheet PDF (real text, parsed by the PDF child)."""
    return make_pdf([[f"{model} spec sheet", f"Model {model}", f"SEER2 {seer2}", f"Tonnage {tonnage}"]])


def answer(*models, doc_type="spec_sheet"):
    """The structured output the fake AI returns for a document: its type and the model numbers it covers."""
    return {"doc_type": doc_type, "models": list(models)}


def responder_for(known):
    """known: {model: answer-dict-for-that-model}. Answers with every known model found in the request text."""
    def respond(request):
        flat = request.text.replace(" ", "")
        models, doc_type = [], "spec_sheet"
        for model, out in known.items():
            if model in flat:
                models.extend(out["models"])
                doc_type = out["doc_type"]
        return {"doc_type": doc_type, "models": models}
    return respond


def linked_doc(conn, manufacturer_name, model_key, doc_type="spec_sheet"):
    """A document (with doc_type, or NULL) linked to a model through document_models. The model row is made if it is new."""
    from crawler.store import manufacturer_for, save_model
    manufacturer_id = manufacturer_for(conn, manufacturer_name)
    model_id = save_model(conn, manufacturer_id, model_key)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO documents (content_sha, manufacturer_id, doc_type) VALUES (%s, %s, %s) RETURNING id",
                    (hashlib.sha256(uuid.uuid4().bytes).hexdigest(), manufacturer_id, doc_type))
        document_id = cur.fetchone()[0]
        cur.execute("INSERT INTO document_models (document_id, model_id, model_key, manufacturer_id) VALUES (%s, %s, %s, %s)",
                    (document_id, model_id, model_key, manufacturer_id))
    return document_id


def status_line(conn, brand_name):
    """The line `status` prints for one brand, read through the real command with the test's own connection.

    The connection is wrapped so the command's commit and close do nothing: the test's transaction stays open and rolls back.
    """
    import contextlib
    import io
    from unittest import mock

    class NoCommit:
        def __init__(self, inner):
            self._inner = inner

        def cursor(self, *a, **k):
            return self._inner.cursor(*a, **k)

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    from crawler.__main__ import main
    out = io.StringIO()
    with mock.patch("crawler.db.connect", lambda *a, **k: NoCommit(conn)), contextlib.redirect_stdout(out):
        assert main(["status"]) == 0
    lines = [ln for ln in out.getvalue().splitlines() if ln.startswith(f"{brand_name} (")]
    assert len(lines) == 1, out.getvalue()
    return lines[0]


def uncovered_count(conn, brand_name):
    """How many ENERGY STAR models `status` says have no documents for this brand (0 if it prints no such count)."""
    import re
    found = re.search(r"(\d+) ENERGY STAR models with no documents", status_line(conn, brand_name))
    return int(found.group(1)) if found else 0


def settle(conn, client, brand, **kwargs):
    """Run extraction passes until a submit and its collection have both had a chance to happen."""
    from crawler.extract import extract_pass
    for _ in range(3):
        extract_pass(conn, client, brand=brand, **kwargs)


def make_pdf(pages, drawings=None):
    """A small valid PDF. pages is a list of pages, each a list of text lines; drawings, if given, holds raw
    drawing operators per page (painted after the text)."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>"}
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode()
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    for i, lines in enumerate(pages):
        ops = ["BT", "/F1 12 Tf", "14 TL", "72 720 Td"] + [f"({esc(line)}) Tj T*" for line in lines] + ["ET"]
        if drawings:
            ops += drawings[i]
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
