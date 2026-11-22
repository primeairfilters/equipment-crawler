"""Help centers (Zendesk): an article page is read through the site's own help-center API.

support.trane.com (prod, 2026-11-21) answered 403 on all 2,494 article pages for the crawler, while its robots.txt
allows them and its help-center API (also allowed) answered 200 with each article's full text. So an address like
/hc/en-us/articles/123-title is fetched as /api/v2/help_center/en-us/articles/123.json and kept as a web page under the
article's own address. When the API does not answer, the article page itself is fetched, as before.
"""
import json

from p11support import HTML, crawl, links_from, paths, serve_pdf, start, state, world  # noqa: F401

JSON = {"Content-Type": "application/json"}
ARTICLE = "/hc/en-us/articles/24061830167949-SCWG-Wiring-Diagrams"
API = "/api/v2/help_center/en-us/articles/24061830167949.json"


def serve_article_api(world):
    world.site.route(API, json.dumps({"article": {
        "id": 24061830167949, "title": "SCWG Wiring Diagrams", "locale": "en-us",
        "body": '<p>Check the 24V transformer on terminal R.</p><a href="/docs/scwg-wiring.pdf">SCWG wiring PDF</a>',
    }}).encode(), headers=JSON)


def test_an_article_is_read_through_the_help_center_api_and_kept_under_its_own_address(world):
    start(world, ARTICLE)
    world.site.route(ARTICLE, b"blocked", status=403, headers=HTML)
    serve_article_api(world)
    serve_pdf(world, "/docs/scwg-wiring.pdf")
    crawl(world)

    assert API in paths(world) and ARTICLE not in paths(world)
    assert state(world, ARTICLE)[0] == "fetched"
    text = world.one("SELECT t.title, t.body FROM page_text t JOIN pages p ON p.id = t.page_id WHERE p.url = %s",
                     (world.url("ruud.test", ARTICLE),))
    assert text[0][0] == "SCWG Wiring Diagrams" and "24V transformer on terminal R" in text[0][1]
    assert (world.url("ruud.test", "/docs/scwg-wiring.pdf"), "SCWG wiring PDF") in links_from(world, ARTICLE)
    assert state(world, "/docs/scwg-wiring.pdf")[0] == "fetched"


def test_when_the_api_does_not_answer_the_article_page_itself_is_read(world):
    start(world, ARTICLE)
    world.site.route(ARTICLE, b"<html><head><title>SCWG</title></head><body><p>Page text</p></body></html>",
                     headers=HTML)
    crawl(world)  # no API route: the fake site answers 404

    assert paths(world) == [API, ARTICLE]
    assert state(world, ARTICLE)[0] == "fetched"


def test_only_help_center_article_addresses_go_to_the_api():
    from crawler.zendesk import article_api
    assert article_api("https://support.trane.com/hc/en-us/articles/123-How-do-I") == \
        "https://support.trane.com/api/v2/help_center/en-us/articles/123.json"
    assert article_api("https://support.trane.com/hc/fr-ca/articles/9") == \
        "https://support.trane.com/api/v2/help_center/fr-ca/articles/9.json"
    assert article_api("https://support.trane.com/hc/en-us/categories/123-Chillers") is None
    assert article_api("https://www.trane.com/articles/123-x") is None
    assert article_api("https://support.trane.com/hc/en-us/articles/123-x.pdf") is None
