"""The document library: every fetched original is kept once as <sha256>.<ext> in DOCS_DIR, never replaced (D26, D57).

PDFs are <sha>.pdf, HTML pages <sha>.html and JSON feed responses <sha>.json.

Nothing here renames, overwrites or deletes. A file is written to .parts/ first, then given its
final name with a hard link; a name that already exists means the document is already stored.
.parts/ files are left where they are.
"""
import hashlib
import os


def _folder():
    """The library folder, read each time (not at import). None when DOCS_DIR is not set."""
    return os.environ.get("DOCS_DIR") or None


def configured():
    return _folder() is not None


EXTENSIONS = {"application/pdf": "pdf", "text/html": "html", "application/xhtml+xml": "html", "application/json": "json"}


def has_file(content_sha, ext="pdf"):
    folder = _folder()
    return bool(folder) and os.path.exists(os.path.join(folder, f"{content_sha}.{ext}"))


def save_pdf(body):
    return save(body, "pdf")


def save(body, ext):
    """Store these bytes under their own hash. True when a new file was made, False when nothing was written."""
    folder = _folder()
    if not folder:
        return False
    digest = hashlib.sha256(body).hexdigest()
    final = os.path.join(folder, f"{digest}.{ext}")
    if os.path.exists(final):
        return False
    parts = os.path.join(folder, ".parts")
    os.makedirs(parts, exist_ok=True)
    part = os.path.join(parts, f"{digest}.{os.getpid()}.part")
    try:
        handle = open(part, "xb")
    except FileExistsError:  # left by an earlier run with the same process id
        part = os.path.join(parts, f"{digest}.{os.getpid()}.{os.urandom(4).hex()}.part")
        handle = open(part, "xb")
    with handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(part, final)
    except FileExistsError:
        return False
    return True


def keep(conn, body, mime):
    """Store an original and record it in crawl.files. Returns the storage key, or None when there is no library."""
    ext = EXTENSIONS.get(mime)
    if ext is None or not configured():
        return None
    save(body, ext)
    digest = hashlib.sha256(body).hexdigest()
    key = f"{digest}.{ext}"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO files (sha256, size_bytes, mime_type, storage_key) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (sha256) DO NOTHING", (digest, len(body), mime, key))
    return key
