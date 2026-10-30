"""P16 (D80): originals live in AWS S3. A fake S3 here behaves like the real one where it matters: it recomputes the
SHA-256 sent with an object and refuses a mismatch, refuses to overwrite under If-None-Match: *, and answers 404 for a
missing key. The real bucket and keys are hidden from every test (support.AI_KEYS)."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from p3support import make_pdf, q

BUCKET = "test-docs-bucket"


def error(code, op):
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


class FakeS3:
    def __init__(self, fail=False):
        self.objects, self.puts, self.fail = {}, [], fail

    def head_object(self, Bucket, Key, ChecksumMode=None):
        if Key not in self.objects:
            raise error("404", "HeadObject")
        body = self.objects[Key]
        return {"ContentLength": len(body),
                "ChecksumSHA256": base64.b64encode(hashlib.sha256(body).digest()).decode()}

    def put_object(self, Bucket, Key, Body, ContentType=None, ChecksumSHA256=None, IfNoneMatch=None):
        self.puts.append(Key)
        if self.fail:
            raise error("InternalError", "PutObject")
        if ChecksumSHA256 != base64.b64encode(hashlib.sha256(Body).digest()).decode():
            raise error("BadDigest", "PutObject")
        if IfNoneMatch == "*" and Key in self.objects:
            raise error("PreconditionFailed", "PutObject")
        self.objects[Key] = Body
        return {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise error("NoSuchKey", "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def get_paginator(self, name):
        fake = self

        class Pages:
            def paginate(self, Bucket, **kwargs):
                yield {"Contents": [{"Key": k} for k in sorted(fake.objects)]}
        return Pages()


@pytest.fixture
def s3(monkeypatch):
    import crawler.library
    fake = FakeS3()
    monkeypatch.setenv("DOCS_BUCKET", BUCKET)
    monkeypatch.setattr(crawler.library, "_s3", fake)
    return fake


PDF = make_pdf([["Installation Manual for RA15AZ24AJ3CA", "Charge the system with R-454B to the nameplate amount."]])
SHA = hashlib.sha256(PDF).hexdigest()


def test_the_real_bucket_and_aws_keys_are_hidden_from_tests():
    assert not [k for k in ("DOCS_BUCKET", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY") if k in os.environ]


def test_an_original_is_stored_once_in_s3_under_its_sha(runtime_conn, s3):
    from crawler.library import keep
    assert keep(runtime_conn, PDF, "application/pdf") == f"{SHA}.pdf"
    assert keep(runtime_conn, PDF, "application/pdf") == f"{SHA}.pdf"
    assert s3.objects == {f"{SHA}.pdf": PDF}
    assert not any(Path(os.environ["DOCS_DIR"]).glob("*.pdf"))  # not also in the local folder
    assert q(runtime_conn, "SELECT storage_key FROM files WHERE sha256 = %s", (SHA,)) == ((f"{SHA}.pdf",),)


def test_an_existing_object_is_never_overwritten(s3):
    from crawler.library import save
    s3.objects[f"{SHA}.pdf"] = PDF
    assert save(PDF, "pdf") is False
    assert s3.objects[f"{SHA}.pdf"] == PDF


def test_a_failed_upload_stores_nothing_and_the_crawl_goes_on(runtime_conn, monkeypatch):
    import crawler.library
    from crawler.library import has_file, keep
    fake = FakeS3(fail=True)
    monkeypatch.setenv("DOCS_BUCKET", BUCKET)
    monkeypatch.setattr(crawler.library, "_s3", fake)
    assert keep(runtime_conn, PDF, "application/pdf") is None  # no exception: the page is fetched again later
    assert fake.objects == {} and not has_file(SHA)
    assert q(runtime_conn, "SELECT COUNT(*) FROM files WHERE sha256 = %s", (SHA,)) == ((0,),)


def test_originals_are_read_back_from_s3(s3):
    from crawler.kb import read_original
    from crawler.library import has_file, read
    s3.objects[f"{SHA}.pdf"] = PDF
    assert has_file(SHA) and read(SHA) == PDF and read("0" * 64) is None
    layout = read_original(SHA, 1)
    assert any("RA15AZ24AJ3CA" in i.get("text", "") for i in layout[0])


def test_preview_reads_an_original_from_s3(s3, capsys):
    from crawler.__main__ import _preview
    s3.objects[f"{SHA}.pdf"] = PDF
    _preview(SHA)
    assert "R-454B" in capsys.readouterr().out


def test_check_reports_live_documents_whose_original_is_missing(runtime_conn, monkeypatch, s3):
    from crawler.kb import check
    from test_p13_embed import live_document
    doc, _, _ = live_document(runtime_conn, monkeypatch)
    stored = q(runtime_conn, "SELECT content_sha FROM documents WHERE id = %s", (doc,))[0][0]
    assert doc in check(runtime_conn)["missing_originals"]
    s3.objects[f"{stored}.pdf"] = b"%PDF-"
    assert doc not in check(runtime_conn)["missing_originals"]


def test_the_local_library_is_copied_to_s3_once(s3):
    from crawler.library import upload_local
    folder = Path(os.environ["DOCS_DIR"])
    (folder / f"{SHA}.pdf").write_bytes(PDF)
    (folder / ".layout").mkdir()
    (folder / ".layout" / "cache.json").write_text("{}")  # caches stay local
    (folder / "notes.txt").write_text("not an original")
    assert upload_local() == {"uploaded": 1, "already there": 0, "failed": 0}
    assert upload_local() == {"uploaded": 0, "already there": 1, "failed": 0}
    assert list(s3.objects) == [f"{SHA}.pdf"]


def test_the_crawlers_aws_policy_allows_list_get_and_put_on_one_bucket_only():
    policy = json.loads((Path(__file__).parent.parent / "aws" / "crawler-docs-policy.json").read_text())
    actions, resources = set(), set()
    for statement in policy["Statement"]:
        assert statement["Effect"] == "Allow"
        found = statement["Action"]
        actions.update([found] if isinstance(found, str) else found)
        resources.add(statement["Resource"])
    assert actions == {"s3:ListBucket", "s3:GetObject", "s3:PutObject"}
    assert resources == {"arn:aws:s3:::BUCKET_NAME", "arn:aws:s3:::BUCKET_NAME/*"}


def test_an_uploaded_file_is_read_from_the_buckets_inbox_and_only_from_there(s3):
    # D84: the admin page puts files in inbox/; the crawler reads them there and never deletes them
    from crawler import library
    s3.objects["inbox/2026/rep-manual.pdf"] = b"%PDF-1.4 rep"
    assert library.read_inbox("inbox/2026/rep-manual.pdf") == b"%PDF-1.4 rep"
    assert library.read_inbox("inbox/2026/missing.pdf") is None
    for key in ("0" * 64 + ".pdf", "inbox/../secret.pdf", "inbox/", "docs/inbox/x.pdf"):
        with pytest.raises(ValueError):
            library.read_inbox(key)
