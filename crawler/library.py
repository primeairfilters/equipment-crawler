"""The document library: every fetched original is kept once as <sha256>.<ext>, never replaced (D26, D57).

PDFs are <sha>.pdf, HTML pages <sha>.html and JSON feed responses <sha>.json.

Where they live (D80): in AWS S3 when DOCS_BUCKET is set (the bucket `tracforce-equipment-docs`), else in the local
folder DOCS_DIR (tests, and the library before S3). The rebuildable caches (.layout, .ocr, .eval) stay in DOCS_DIR
either way.

Nothing here renames, overwrites or deletes:
- S3: each object is sent with its SHA-256, which S3 checks before storing (a mismatch is refused), and with
  If-None-Match: *, so an existing key is never written again. The crawler's AWS user cannot delete
  (aws/crawler-docs-policy.json).
- Local: a file is written to .parts/ first, then given its final name with a hard link; a name that already exists
  means the document is already stored. .parts/ files are left where they are.
"""
import base64
import hashlib
import os
import re

_s3 = None  # the boto3 client, made on first use (tests put a fake here)
ORIGINAL = re.compile(r"[0-9a-f]{64}\.(pdf|html|json)")


class LibraryError(Exception):
    """The library could not store an original (S3 unreachable, refused, ...). Nothing partial is left."""


def _folder():
    """The library folder, read each time (not at import). None when DOCS_DIR is not set."""
    return os.environ.get("DOCS_DIR") or None


def _bucket():
    return os.environ.get("DOCS_BUCKET") or None


def _client():
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION"))
    return _s3


def _missing(exc):
    return exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound")


def configured():
    return _bucket() is not None or _folder() is not None


EXTENSIONS = {"application/pdf": "pdf", "text/html": "html", "application/xhtml+xml": "html", "application/json": "json"}
TYPES = {"pdf": "application/pdf", "html": "text/html", "json": "application/json"}


def has_file(content_sha, ext="pdf"):
    if _bucket():
        from botocore.exceptions import ClientError
        try:
            _client().head_object(Bucket=_bucket(), Key=f"{content_sha}.{ext}")
            return True
        except ClientError as exc:
            if _missing(exc):
                return False
            raise
    folder = _folder()
    return bool(folder) and os.path.exists(os.path.join(folder, f"{content_sha}.{ext}"))


def read(content_sha, ext="pdf"):
    """The original's bytes, or None when the library does not have it."""
    return _get(f"{content_sha}.{ext}")


INBOX = re.compile(r"inbox/[^/]+(/[^/]+)*")


def read_inbox(key):
    """The bytes of a file the admin page put in inbox/ (D84), or None when it is not there. Read only: the crawler
    cannot delete, so the bucket's lifecycle rule clears inbox/."""
    if not INBOX.fullmatch(key) or any(part in (".", "..") for part in key.split("/")):
        raise ValueError(f"{key} is not a file in inbox/")
    return _get(key)


def _get(key):
    if _bucket():
        from botocore.exceptions import ClientError
        try:
            return _client().get_object(Bucket=_bucket(), Key=key)["Body"].read()
        except ClientError as exc:
            if _missing(exc):
                return None
            raise
    folder = _folder()
    path = os.path.join(folder, *key.split("/")) if folder else None
    if not path or not os.path.isfile(path):
        return None
    with open(path, "rb") as handle:
        return handle.read()


def stored_keys():
    """Every original's key in the library (for `check`)."""
    if _bucket():
        keys = set()
        for page in _client().get_paginator("list_objects_v2").paginate(Bucket=_bucket()):
            keys.update(o["Key"] for o in page.get("Contents", []))
        return keys
    folder = _folder()
    return {n for n in os.listdir(folder) if ORIGINAL.fullmatch(n)} if folder else set()


def save_pdf(body):
    return save(body, "pdf")


def save(body, ext):
    """Store these bytes under their own hash. True when a new original was stored, False when it was already there.
    Raises LibraryError when S3 could not store it."""
    digest = hashlib.sha256(body).hexdigest()
    if _bucket():
        return _put(f"{digest}.{ext}", body, digest)
    folder = _folder()
    if not folder:
        return False
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


def _put(key, body, digest):
    from botocore.exceptions import BotoCoreError, ClientError
    try:
        _client().put_object(Bucket=_bucket(), Key=key, Body=body, ContentType=TYPES[key.rsplit(".", 1)[1]],
                             ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode(), IfNoneMatch="*")
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("PreconditionFailed", "ConditionalRequestConflict"):
            return False
        raise LibraryError(f"S3 refused {key}: {exc.response.get('Error', {}).get('Code')}") from exc
    except BotoCoreError as exc:
        raise LibraryError(f"S3 could not be reached for {key}: {exc}") from exc


def keep(conn, body, mime):
    """Store an original and record it in crawl.files. Returns the storage key, or None when there is no library or
    it could not be stored (the page has no original then, so the next check fetches it again in full)."""
    ext = EXTENSIONS.get(mime)
    if ext is None or not configured():
        return None
    try:
        save(body, ext)
    except LibraryError:
        return None
    digest = hashlib.sha256(body).hexdigest()
    key = f"{digest}.{ext}"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO files (sha256, size_bytes, mime_type, storage_key) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (sha256) DO NOTHING", (digest, len(body), mime, key))
    return key


def upload_local():
    """Copy every original in the local folder to S3 (`./crawl library upload`): each checked by S3 against its
    SHA-256, none written twice. The caches and anything else in the folder stay local."""
    folder = _folder()
    counts = {"uploaded": 0, "already there": 0, "failed": 0}
    for name in sorted(os.listdir(folder) if folder else []):
        if not ORIGINAL.fullmatch(name):
            continue
        with open(os.path.join(folder, name), "rb") as handle:
            body = handle.read()
        try:
            counts["uploaded" if _put(name, body, name.split(".")[0]) else "already there"] += 1
        except LibraryError:
            counts["failed"] += 1
    return counts
