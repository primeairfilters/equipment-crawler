"""P11 (D68b): a document feed (the JSON API a JS page calls) is walked directly, without a browser.

A feed start address is fetched as JSON. Inside it, addresses under the feed's own API folder are fetched as more
JSON, and PDF addresses become documents, with the entry's title as the link text. mylinkdrive-style feeds name
their categories by slug, so a slugName on that host becomes a category request.
"""
import hashlib
import json
import os

from p11support import crawl, links_from, paths, serve_pdf, start, state, world  # noqa: F401
from p3support import make_pdf

JSON = {"Content-Type": "application/json"}


def serve_json(world, path, data, host="*"):
    world.site.route(path, json.dumps(data).encode(), headers=JSON, host=host)


def test_a_feed_is_walked_into_documents_without_a_browser(world):
    world.approve("files.ruud.test")
    start(world, "/api/v0/menu", kind="feed")
    serve_json(world, "/api/v0/menu", {"categories": [
        {"name": "Ductless", "href": "/api/v0/category?slug=ductless"},
        {"name": "About", "href": "/about"},                       # outside the API folder: not walked
    ]})
    serve_json(world, "/api/v0/category", {"items": [
        {"title": "MSZ-GL Installation Manual", "fileUrl": world.url("files.ruud.test", "/docs/msz-gl.pdf")},
        {"title": "Elsewhere", "fileUrl": world.url("cdn.other.test", "/x.pdf")},
    ]})
    body = make_pdf([["MSZ-GL09NA Installation Manual"]])
    serve_pdf(world, "/docs/msz-gl.pdf", body, host="files.ruud.test")
    crawl(world, checker=None)  # no AI: feed entries are decided by the feed rules alone

    assert paths(world) == ["/api/v0/menu", "/api/v0/category?slug=ductless", "/docs/msz-gl.pdf"]
    sha = hashlib.sha256(body).hexdigest()
    doc = world.one("SELECT d.id FROM documents d WHERE d.content_sha = %s", (sha,))
    assert len(doc) == 1
    assert world.one("SELECT url FROM document_urls WHERE document_id = %s", (doc[0][0],)) == (
        (world.url("files.ruud.test", "/docs/msz-gl.pdf"),),)
    assert (world.url("files.ruud.test", "/docs/msz-gl.pdf"), "MSZ-GL Installation Manual") in links_from(
        world, "/api/v0/category?slug=ductless")
    assert state(world, "/x.pdf", host="cdn.other.test")[0] == "out_of_scope"
    assert state(world, "/about") is None


def test_feed_responses_are_kept_as_originals_and_never_sent_for_tagging(world):
    start(world, "/api/v0/menu", kind="feed")
    data = {"categories": []}
    serve_json(world, "/api/v0/menu", data)
    crawl(world, checker=None)
    sha = hashlib.sha256(json.dumps(data).encode()).hexdigest()
    assert os.path.exists(os.path.join(os.environ["DOCS_DIR"], f"{sha}.json"))
    row = world.one("SELECT fetch_state, extraction_status, is_pdf FROM pages WHERE url = %s",
                    (world.url("ruud.test", "/api/v0/menu"),))
    assert row == (("fetched", "skipped", False),)


def test_a_mylinkdrive_slug_becomes_a_category_request(world):
    world.approve("www.mylinkdrive.com")
    start(world, "/api/v0/categoryMenu?slugName=USA", host="www.mylinkdrive.com", kind="feed")
    serve_json(world, "/api/v0/categoryMenu", {"menu": [{"name": "M-Series", "slugName": "M_Series"}]},
               host="www.mylinkdrive.com")
    serve_json(world, "/api/v0/category", {"documents": []}, host="www.mylinkdrive.com")
    crawl(world, checker=None)
    assert paths(world) == ["/api/v0/categoryMenu?slugName=USA", "/api/v0/category?slugName=M_Series"]


def test_a_feed_that_is_not_json_fails_cleanly(world):
    start(world, "/api/v0/menu", kind="feed")
    world.site.route("/api/v0/menu", b"<html>not json</html>", headers={"Content-Type": "text/html"})
    summary = crawl(world, checker=None)
    assert summary["failed"] == 1
    assert state(world, "/api/v0/menu")[0] == "failed"
