"""P11 (D61, D62): the free rules that decide a link before any AI is asked. No database, no network."""
import pytest

from crawler.rules import Keyword, decide, folder_of, keyword_hits

U = "https://www.ruud.com"


@pytest.mark.parametrize("keyword,url", [
    ("blog", "https://blog.ruud.com/post"),
    ("blog", U + "/blog/"),
    ("blog", U + "/blog/how-to-size-a-heat-pump"),
    ("blog", U + "/en/blog"),
    ("BLOG", U + "/Blog/x"),
    ("news*", U + "/news/2026"),
    ("news*", U + "/newsroom/"),
    ("news*", U + "/news-events/x"),
    ("news*", "https://newsroom.ruud.com/"),
])
def test_a_keyword_matches_a_whole_host_label_or_a_whole_path_folder(keyword, url):
    assert keyword_hits(keyword, url)


@pytest.mark.parametrize("keyword,url", [
    ("press", U + "/compressors/"),
    ("press", U + "/products/compressor-press-kit"),
    ("news", U + "/newsome-series/"),
    ("news", "https://newsome.ruud.com/"),
    ("blog", U + "/blogs/"),
    ("blog", U + "/product/?ref=blog"),
    ("blog", U + "/product#blog"),
    ("ruud", U + "/product/x"),  # the brand's own domain label is not a folder the user means
])
def test_a_keyword_never_matches_part_of_a_word(keyword, url):
    assert not keyword_hits(keyword, url)


def test_folder_is_the_first_path_segment():
    assert folder_of(U + "/Product/ra15az/") == "product"
    assert folder_of(U + "/product?x=1") == "product"
    assert folder_of(U + "/") is None
    assert folder_of(U) is None


def kw(word, effect="skip", host=None, source="user"):
    return Keyword(word, effect, host, source)


def test_a_pdf_is_always_crawled_even_under_a_skip_keyword():
    d = decide(U + "/news/bulletin-42.pdf", "Service bulletin", keywords=[kw("news*")])
    assert d.verdict == "crawl" and d.by == "rule"


@pytest.mark.parametrize("url", [
    "https://media.ruud.com/wp-content/uploads/sites/2/2023/12/RuPROF_Ultra_75Gal_LeftFacing_LoRes.jpg",
    "https://media.ruud.com/wp-content/uploads/sites/2/2023/07/Ruud_TritonLD_CommGasWH_50Gal_CoffeeShop_LoRes.PNG",
    "https://media.ruud.com/site/ruuddotcom/_WP/downloads/CertiSpec3Setup.exe",
    U + "/products/specs/video/install-walkthrough.mp4?v=2",
])
def test_an_image_video_or_installer_link_is_skipped_before_it_is_fetched(url):
    # Ruud batch 6: 20 of 49 failures were .jpg/.png/.exe links fetched, refused as bad_content_type and retried
    d = decide(url, "Specifications", keywords=[kw("products", "prefer")])
    assert (d.verdict, d.state, d.by) == ("skip", "skipped_rule", "rule")
    assert "not a document" in d.reason


def test_a_skip_keyword_skips_with_the_keyword_as_the_reason():
    d = decide(U + "/blog/post", "Specifications", keywords=[kw("blog")])
    assert (d.verdict, d.state, d.by) == ("skip", "skipped_keyword", "rule")
    assert "blog" in d.reason


def test_a_prefer_keyword_crawls():
    d = decide(U + "/catalog/x", "link", keywords=[kw("catalog", "prefer")])
    assert d.verdict == "crawl" and "catalog" in d.reason


def test_a_learned_folder_rule_decides_for_its_host_only():
    learned = [kw("range", "prefer", host="www.ruud.com", source="learned")]
    assert decide(U + "/range/zz", "link", keywords=learned).verdict == "crawl"
    assert decide("https://shop.ruud.com/range/zz", "link", keywords=learned).verdict == "unsure"
    skip_learned = [kw("about", "skip", host="www.ruud.com", source="learned")]
    d = decide(U + "/about/team", "link", keywords=skip_learned)
    assert (d.verdict, d.state) == ("skip", "skipped_rule")


def test_a_skip_keyword_beats_a_learned_crawl_rule():
    keywords = [kw("product", "prefer", host="www.ruud.com", source="learned"), kw("blog")]
    assert decide(U + "/product/blog/x", "link", keywords=keywords).verdict == "skip"


def test_a_model_code_in_the_address_or_spec_like_link_text_says_crawl():
    assert decide(U + "/product/ra15az24aj3ca/", "link", keywords=[]).verdict == "crawl"
    assert decide(U + "/x", "Specifications", keywords=[]).verdict == "crawl"
    assert decide(U + "/x", "Installation Manual", keywords=[]).verdict == "crawl"
    assert decide(U + "/literature/", "link", keywords=[]).verdict == "crawl"


def test_read_more_or_press_release_text_says_skip():
    d = decide(U + "/x", "Read more", keywords=[])
    assert (d.verdict, d.state, d.by) == ("skip", "skipped_rule", "rule")
    assert decide(U + "/y", "Press release", keywords=[]).verdict == "skip"


def test_links_on_an_article_page_lean_towards_skip():
    assert decide(U + "/x", "link", keywords=[], page_is_article=True).verdict == "skip"
    assert decide(U + "/x", "Specifications", keywords=[], page_is_article=True).verdict == "unsure"


def test_a_plain_link_is_unsure():
    d = decide(U + "/about-us", "About us", keywords=[])
    assert d.verdict == "unsure" and d.by is None
