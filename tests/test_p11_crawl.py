"""P11 (D57-D62, D67): gathering on the new schema. A fake site in this process, a fake clock, a fake AI link check,
rows rolled back after each test.
"""
import hashlib
import os

from p11support import (Checker, crawl, links_from, page, paths, serve, serve_pdf, skip_keyword, start, state,
                        world)  # noqa: F401  (world is a fixture)
from p3support import make_pdf

# ---------------------------------------------------------------- every link, with its text (D58)


def test_every_link_is_recorded_with_its_text(world):
    start(world)
    serve(world, "/start", ("/a", "RA15AZ Installation Instructions"), ("/doc.pdf", "Spec sheet"), ("/a", "again"))
    serve(world, "/a")
    serve_pdf(world, "/doc.pdf")
    crawl(world)
    found = links_from(world, "/start")
    assert (world.url("ruud.test", "/a"), "RA15AZ Installation Instructions") in found
    assert (world.url("ruud.test", "/doc.pdf"), "Spec sheet") in found
    assert len(found) == 2  # one row per link target; the first text is kept


def test_a_pdf_on_a_host_not_approved_is_recorded_but_never_requested(world):
    start(world)
    serve(world, "/start", (world.url("files.other.test", "/manual.pdf"), "Manual"), world.url("other.test", "/page"))
    crawl(world)
    assert world.site.hits(host="files.other.test") == [] and world.site.hits(host="other.test") == []
    assert state(world, "/manual.pdf", host="files.other.test")[0] == "out_of_scope"
    assert state(world, "/page", host="other.test") is None  # an off-site web page is not recorded at all


def test_a_page_keeps_its_final_address_after_a_redirect(world):
    start(world)
    world.site.route("/start", b"", status=301, headers={"Location": "/moved"})
    serve(world, "/moved")
    crawl(world)
    rows = world.one("SELECT final_url, fetch_state FROM pages WHERE brand_id = %s AND url = %s",
                     (world.brand_id, world.url("ruud.test", "/start")))
    assert rows == ((world.url("ruud.test", "/moved"), "fetched"),)


# ---------------------------------------------------------------- depth per start address, limits (D59, D60)

def chain(world, n):
    names = [f"/l{i}" for i in range(n + 1)]
    for here, nxt in zip(["/start"] + names, names):
        serve(world, here, nxt)
    serve(world, names[-1])


def test_depth_comes_from_the_start_address_and_defaults_to_3(world):
    start(world)
    chain(world, 5)
    crawl(world)
    assert paths(world) == ["/start", "/l0", "/l1", "/l2"]
    assert state(world, "/l3")[0] == "beyond_depth"  # recorded, not fetched
    assert state(world, "/l4") is None  # never seen, since /l3 was never read


def test_a_start_address_can_go_deeper_or_shallower(world):
    start(world, depth=1)
    chain(world, 3)
    crawl(world)
    assert paths(world) == ["/start", "/l0"]
    assert state(world, "/l1")[0] == "beyond_depth"


def test_links_past_the_page_limit_are_recorded_and_the_run_says_so(world):
    sid = start(world)
    links = [f"/p{n}" for n in range(8)]
    serve(world, "/start", *links)
    for p in links:
        serve(world, p)
    crawl(world, max_pages=3)
    assert len(paths(world)) == 3
    states = [state(world, p)[0] for p in links]
    assert states.count("fetched") == 2 and states.count("over_limit") == 6
    run = world.one("SELECT pages_fetched, limit_hit, links_left, finished_at IS NOT NULL FROM crawl_runs "
                    "WHERE start_url_id = %s", (sid,))
    assert run == ((3, True, 6, True),)


def test_pdfs_are_fetched_first_then_product_links_then_the_rest(world):
    start(world)
    serve(world, "/start", ("/about", "About"), ("/product/ra15az", "RA15AZ"), ("/doc.pdf", "Doc"), ("/contact", "Contact"))
    for p in ("/about", "/product/ra15az", "/contact"):
        serve(world, p)
    serve_pdf(world, "/doc.pdf")
    crawl(world)
    assert paths(world) == ["/start", "/doc.pdf", "/product/ra15az", "/about", "/contact"]


# ---------------------------------------------------------------- keywords (D61)

def test_a_skip_keyword_stops_html_pages_but_never_a_pdf(world):
    world.approve("blog.ruud.test")
    skip_keyword(world, "blog")
    skip_keyword(world, "press")
    start(world)
    serve(world, "/start", "/blog/post", world.url("blog.ruud.test", "/x"), "/compressors/", "/blog/bulletin.pdf")
    serve(world, "/compressors/")
    serve_pdf(world, "/blog/bulletin.pdf")
    crawl(world)
    assert sorted(paths(world)) == ["/blog/bulletin.pdf", "/compressors/", "/start"]
    assert state(world, "/blog/post")[:2] == ("skipped_keyword", "rule")
    assert "blog" in state(world, "/blog/post")[2]
    assert state(world, "/x", host="blog.ruud.test")[0] == "skipped_keyword"


def test_a_keyword_for_another_brand_or_switched_off_does_nothing(world):
    from crawler.brands import add_brand
    other = add_brand(world.conn, world.brand + " other")
    skip_keyword(world, "blog", brand=other)
    with world.conn.cursor() as cur:
        cur.execute("INSERT INTO crawl_keywords (keyword, effect, source, active) VALUES ('docs', 'skip', 'user', false)")
    start(world)
    serve(world, "/start", "/blog/a", "/docs/b")
    serve(world, "/blog/a")
    serve(world, "/docs/b")
    crawl(world)
    assert sorted(paths(world)) == ["/blog/a", "/docs/b", "/start"]


# ---------------------------------------------------------------- the AI only when unsure (D62)

def test_unsure_links_go_to_the_ai_once_per_page_and_never_again(world):
    start(world)
    serve(world, "/start", ("/about", "About us"), ("/team", "Our team"), ("/product/ra15az", "RA15AZ"))
    for p in ("/about", "/team", "/product/ra15az"):
        serve(world, p)
    checker = Checker(lambda url, text: url.endswith("/about"))
    crawl(world, checker=checker)
    assert len(checker.calls) == 1  # one call for the page, carrying both unsure links
    context, asked = checker.calls[0]
    assert {u for u, _ in asked} == {world.url("ruud.test", "/about"), world.url("ruud.test", "/team")}
    assert ("About us" in [t for _, t in asked]) and context["title"] == "/start"
    assert sorted(paths(world)) == ["/about", "/product/ra15az", "/start"]
    assert state(world, "/team")[:2] == ("skipped_ai", "ai")
    assert state(world, "/about")[:2] == ("fetched", "ai")

    world.clock.advance(31 * 24 * 3600)
    crawl(world, checker=checker)
    assert len(checker.calls) == 1  # the stored answers decide on the next run


def test_with_no_ai_unsure_links_wait_and_are_asked_later(world):
    start(world)
    serve(world, "/start", ("/about", "About us"))
    serve(world, "/about")
    crawl(world, checker=None)
    assert paths(world) == ["/start"]
    assert state(world, "/about")[0] == "ask_ai"

    world.clock.advance(31 * 24 * 3600)
    checker = Checker()
    crawl(world, checker=checker)
    assert checker.asked() == [world.url("ruud.test", "/about")]
    assert "/about" in paths(world)


def test_after_five_matching_answers_a_folder_rule_is_learned_and_decides_for_free(world):
    start(world)
    first = [(f"/range/m{n}", f"Model {n}") for n in range(5)]
    serve(world, "/start", *first, ("/next", "Next"))
    serve(world, "/next", ("/range/m9", "Model 9"))
    for p, _ in first + [("/range/m9", "")]:
        serve(world, p)
    checker = Checker()
    crawl(world, checker=checker)
    learned = world.one("SELECT keyword, effect, host, source, active FROM crawl_keywords WHERE source = 'learned' "
                        "AND host = 'ruud.test'")
    assert learned == (("range", "prefer", "ruud.test", "learned", True),)
    assert world.url("ruud.test", "/range/m9") not in checker.asked()  # decided by the learned rule, for free
    assert "/range/m9" in paths(world)
    assert state(world, "/range/m9")[1] == "rule"


def test_mixed_answers_in_a_folder_teach_nothing(world):
    start(world)
    links = [(f"/mixed/m{n}", f"Model {n}") for n in range(6)]
    serve(world, "/start", *links)
    checker = Checker(lambda url, text: not url.endswith("m0"))
    crawl(world, checker=checker)
    assert world.one("SELECT COUNT(*) FROM crawl_keywords WHERE source = 'learned' AND keyword = 'mixed'")[0][0] == 0


def test_your_verdict_overrides_the_ai_and_the_keywords(world):
    from crawler.brands import verdict
    skip_keyword(world, "blog")
    start(world)
    serve(world, "/start", ("/about", "About"), "/blog/keep")
    serve(world, "/about")
    serve(world, "/blog/keep")
    crawl(world)
    assert "/about" in paths(world) and "/blog/keep" not in paths(world)

    assert verdict(world.conn, world.url("ruud.test", "/about"), "skip") == 1
    assert verdict(world.conn, world.url("ruud.test", "/blog/keep"), "crawl") == 1
    world.site.requests.clear()
    world.clock.advance(31 * 24 * 3600)
    crawl(world, checker=Checker(lambda u, t: True))
    assert "/blog/keep" in paths(world) and "/about" not in paths(world)
    assert state(world, "/about")[1] == "user" and state(world, "/blog/keep")[:2] == ("fetched", "user")


# ---------------------------------------------------------------- robots.txt switch (D67)

def test_ignore_robots_is_a_per_host_switch_that_keeps_the_gap_and_the_honest_agent(world):
    from crawler.brands import approve_host
    from crawler.fetch import USER_AGENT
    world.approve("strict.test")
    world.site.route("/robots.txt", b"User-agent: *\nDisallow: /\n", headers={"Content-Type": "text/plain"})
    start(world, host="strict.test")
    start(world, "/start", host="ruud.test")
    serve(world, "/start", "/a")
    serve(world, "/a")
    crawl(world)
    assert paths(world) == []  # both hosts say Disallow: / and nothing is fetched

    approve_host(world.conn, "strict.test", ignore_robots=True)
    row = world.one("SELECT ignore_robots, ignore_robots_changed_at FROM hosts WHERE host = 'strict.test'")
    assert row[0][0] is True and row[0][1] is not None  # the switch is recorded with when it was flipped
    world.site.requests.clear()
    world.clock.advance(25 * 3600)
    crawl(world)
    hits = world.site.page_hits()
    assert {r["host"] for r in hits} == {"strict.test"}  # ruud.test still obeys its robots.txt
    assert [r["path"] for r in hits] == ["/start", "/a"]
    assert all(r["headers"]["user-agent"] == USER_AGENT for r in hits)
    assert (hits[1]["t"] - hits[0]["t"]).total_seconds() >= 10


# ---------------------------------------------------------------- documents and originals (D40, D57, D59)

def test_the_same_pdf_at_two_addresses_is_one_document_with_two_addresses(world):
    start(world)
    body = make_pdf([["Model RA15AZ24AJ3CA", "SEER2 15.2"]])
    serve(world, "/start", "/a.pdf", "/copy/a.pdf")
    serve_pdf(world, "/a.pdf", body)
    serve_pdf(world, "/copy/a.pdf", body)
    crawl(world)
    sha = hashlib.sha256(body).hexdigest()
    docs = world.one("SELECT id, state, page_count, text_pages, needs_ocr FROM documents WHERE content_sha = %s", (sha,))
    assert len(docs) == 1 and docs[0][1:] == ("new", 1, 1, False)
    urls = {r[0] for r in world.one("SELECT url FROM document_urls WHERE document_id = %s", (docs[0][0],))}
    assert urls == {world.url("ruud.test", "/a.pdf"), world.url("ruud.test", "/copy/a.pdf")}
    assert world.one("SELECT size_bytes, mime_type, storage_key FROM files WHERE sha256 = %s", (sha,)) == (
        (len(body), "application/pdf", f"{sha}.pdf"),)


def test_a_pdf_served_as_octet_stream_is_kept_in_the_library_as_a_pdf(world):
    """files.myrheem.com serves some manuals as application/octet-stream; the original must still be kept."""
    start(world)
    body = make_pdf([["Model RA15AZ24AJ3CA", "SEER2 15.2"]])
    serve(world, "/start", "/a.pdf")
    world.site.route("/a.pdf", body, headers={"Content-Type": "application/octet-stream"})
    crawl(world)
    sha = hashlib.sha256(body).hexdigest()
    assert os.path.exists(os.path.join(os.environ["DOCS_DIR"], f"{sha}.pdf"))
    assert world.one("SELECT size_bytes, mime_type, storage_key FROM files WHERE sha256 = %s", (sha,)) == (
        (len(body), "application/pdf", f"{sha}.pdf"),)


def test_a_changed_pdf_is_a_new_document_that_replaces_the_old_one(world):
    start(world)
    serve(world, "/start", "/a.pdf")
    old = make_pdf([["Model RA15AZ24AJ3CA", "Revision A"]])
    new = make_pdf([["Model RA15AZ24AJ3CA", "Revision B"]])
    serve_pdf(world, "/a.pdf", old)
    crawl(world)
    serve_pdf(world, "/a.pdf", new)
    world.clock.advance(31 * 24 * 3600)
    crawl(world)
    rows = world.one("SELECT d.content_sha, r.content_sha FROM documents d LEFT JOIN documents r ON r.id = d.replaces_id "
                     "WHERE d.content_sha IN (%s, %s) ORDER BY d.id",
                     (hashlib.sha256(old).hexdigest(), hashlib.sha256(new).hexdigest()))
    assert rows == ((hashlib.sha256(old).hexdigest(), None),
                    (hashlib.sha256(new).hexdigest(), hashlib.sha256(old).hexdigest()))


def test_html_pages_are_kept_by_hash_next_to_the_pdfs(world):
    start(world)
    serve(world, "/start")
    crawl(world)
    body = page(title="/start")
    sha = hashlib.sha256(body).hexdigest()
    assert os.path.exists(os.path.join(os.environ["DOCS_DIR"], f"{sha}.html"))
    assert world.one("SELECT mime_type, storage_key FROM files WHERE sha256 = %s", (sha,)) == (("text/html", f"{sha}.html"),)


def test_a_pdf_with_text_less_pages_needs_ocr(world):
    start(world)
    serve(world, "/start", "/scan.pdf")
    body = make_pdf([[], ["Model RA15AZ24AJ3CA spec sheet page two"]])
    serve_pdf(world, "/scan.pdf", body)
    crawl(world)
    assert world.one("SELECT page_count, text_pages, needs_ocr FROM documents WHERE content_sha = %s",
                     (hashlib.sha256(body).hexdigest(),)) == ((2, 1, True),)
    assert [r[0] for r in world.one("SELECT has_text FROM page_text pt JOIN pages p ON p.id = pt.page_id "
                                    "WHERE p.url = %s ORDER BY page_number", (world.url("ruud.test", "/scan.pdf"),))] == [
        False, True]


# ---------------------------------------------------------------- review fixes (2026-10-27)

def test_an_unreadable_pdf_link_to_another_host_is_skipped_not_a_crash(world):
    start(world)
    serve(world, "/start", "http://a..b.test/x.pdf", "https://[2001:db8::1]/a.pdf", "/a")
    serve(world, "/a")
    crawl(world)  # before the fix this raised ValueError half way through the page
    assert sorted(paths(world)) == ["/a", "/start"]


def test_a_page_is_visited_at_its_shallowest_depth(world):
    start(world)  # depth 3
    serve(world, "/start", ("/product/a", "Products"), ("/b", "B"))
    serve(world, "/product/a", ("/product/c", "Products"))
    serve(world, "/product/c", ("/z", "Z"))
    serve(world, "/b", ("/z", "Z"))
    serve(world, "/z", ("/deep", "Deep"))
    serve(world, "/deep")
    crawl(world)
    assert "/deep" in paths(world)  # /z is depth 2 through /b, so its link is depth 3, inside the limit


def test_a_page_redirected_to_is_not_fetched_again_in_the_same_run(world):
    start(world)
    world.site.route("/old", b"", status=301, headers={"Location": "/new"})
    serve(world, "/start", ("/old", "Old"), ("/new", "New"))
    serve(world, "/new")
    crawl(world)
    assert paths(world).count("/new") == 1


def test_a_learned_skip_never_beats_a_product_signal_and_a_locale_folder_is_not_one_folder(world):
    start(world)
    links = [(f"/en/about/p{n}", f"About {n}") for n in range(6)]
    serve(world, "/start", *links, ("/en/x/ra15az24aj3ca", "RA15AZ"))
    for p, _ in links:
        serve(world, p)
    serve(world, "/en/x/ra15az24aj3ca")
    crawl(world, checker=Checker(lambda url, text: False))
    learned = world.one("SELECT keyword, effect FROM crawl_keywords WHERE source = 'learned' AND host = 'ruud.test'")
    assert learned == (("en/about", "skip"),)
    assert "/en/x/ra15az24aj3ca" in paths(world)


def test_an_ai_error_leaves_the_links_waiting_and_the_crawl_goes_on(world):
    from crawler.linkcheck import LinkChecker

    class Down:
        def ask_links(self, *a, **k):
            raise RuntimeError("529 overloaded")

    start(world)
    serve(world, "/start", ("/about", "About"), ("/product/x", "Products"))
    serve(world, "/product/x")
    crawl(world, checker=LinkChecker(Down()))
    assert state(world, "/about")[0] == "ask_ai" and "/product/x" in paths(world)


def test_a_changed_pdf_at_a_hidden_manuals_address_stays_hidden(world):
    from crawler.kb import hide
    start(world)
    serve(world, "/start", "/a.pdf")
    old = make_pdf([["Model RA15AZ24AJ3CA", "Revision A"]])
    serve_pdf(world, "/a.pdf", old)
    crawl(world)
    old_id = world.one("SELECT id FROM documents WHERE content_sha = %s", (hashlib.sha256(old).hexdigest(),))[0][0]
    hide(world.conn, "document", str(old_id), reason="takedown", by_whom="tester")
    new = make_pdf([["Model RA15AZ24AJ3CA", "Revision B"]])
    serve_pdf(world, "/a.pdf", new)
    world.clock.advance(31 * 24 * 3600)
    crawl(world)
    assert world.one("SELECT hidden_reason FROM documents WHERE content_sha = %s",
                     (hashlib.sha256(new).hexdigest(),)) == (("takedown",),)
