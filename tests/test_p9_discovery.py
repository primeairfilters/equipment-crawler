"""P9: discovery asks for the brand's own listing pages, and keeps a start URL only if its host is one of the answer's domains.

Fake web-search client, `_testing`, rolled-back transactions. Before P9 a start URL on a host nothing else covered was
kept and its host was proposed as a waiting host too; now such a URL is dropped and no host row is made for it.
"A host in the answer's domains" is tested as the listed domain itself or a dot-bounded subdomain of it (the existing
tests keep https://www.ruud.com/... with domains ["ruud.com"]).
"""
import re

import pytest

from fakeai import FakeDiscoverAI
from p3support import new_brand
from test_p4_discover import T0, domains, found, run_discover, start_urls

OWN = "https://www.ruud.com/products/hvac/air_conditioners/"


# ---------------------------------------------------------------- the start URL rule

def test_a_manualslib_start_url_is_dropped_and_makes_no_waiting_host(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    out = found(domains=["ruud.com"], docs=["files.myrheem.com"],
                starts=[OWN, "https://www.manualslib.com/brand/ruud/"])
    run_discover(runtime_conn, FakeDiscoverAI(out), name)

    assert [r[0] for r in start_urls(runtime_conn, brand_id)] == [OWN]                      # the own-site one went in (path ran)
    assert {r[0] for r in domains(runtime_conn, brand_id)} == {"ruud.com", "files.myrheem.com"}
    assert not any("manualslib" in r[0] for r in domains(runtime_conn, brand_id))


def test_a_start_url_on_a_listed_domain_is_kept(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    out = found(domains=["www.ruud.com"], starts=["https://www.ruud.com/a/"])
    run_discover(runtime_conn, FakeDiscoverAI(out), name)
    assert [r[0] for r in start_urls(runtime_conn, brand_id)] == ["https://www.ruud.com/a/"]
    assert [r[0] for r in domains(runtime_conn, brand_id)] == ["www.ruud.com"]              # and no extra host row


def test_a_start_url_on_a_subdomain_of_a_listed_domain_is_kept(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    out = found(domains=["ruud.com"], starts=["https://www.ruud.com/a/", "https://shop.eu.ruud.com/b/"])
    run_discover(runtime_conn, FakeDiscoverAI(out), name)
    assert sorted(r[0] for r in start_urls(runtime_conn, brand_id)) == ["https://shop.eu.ruud.com/b/", "https://www.ruud.com/a/"]
    assert [r[0] for r in domains(runtime_conn, brand_id)] == ["ruud.com"]


@pytest.mark.parametrize("url", ["https://notruud.com/a/", "https://ruud.com.evil.test/a/", "https://catalog.newhost.test/b/"])
def test_a_start_url_whose_host_is_not_a_listed_domain_is_dropped_with_no_host_row(runtime_conn, url):
    name, brand_id = new_brand(runtime_conn)
    out = found(domains=["ruud.com"], starts=["https://www.ruud.com/a/", url])
    run_discover(runtime_conn, FakeDiscoverAI(out), name)
    assert [r[0] for r in start_urls(runtime_conn, brand_id)] == ["https://www.ruud.com/a/"]  # control: the good one stays
    assert [r[0] for r in domains(runtime_conn, brand_id)] == ["ruud.com"]


def test_an_answer_with_no_domains_keeps_no_start_url(runtime_conn):
    name, brand_id = new_brand(runtime_conn)
    run_discover(runtime_conn, FakeDiscoverAI(found(starts=[OWN])), name)
    assert start_urls(runtime_conn, brand_id) == ()
    assert domains(runtime_conn, brand_id) == ()


def test_discovery_is_still_recorded_as_done_when_start_urls_were_dropped(runtime_conn):
    from datetime import timedelta
    name, brand_id = new_brand(runtime_conn)
    fake = FakeDiscoverAI(found(domains=["ruud.com"], starts=["https://www.manualslib.com/brand/ruud/"]))
    result = run_discover(runtime_conn, fake, name)
    assert isinstance(result, dict) and len(fake.discover_calls()) == 1
    assert run_discover(runtime_conn, fake, name, now=T0 + timedelta(days=10)) == "recent"
    assert len(fake.discover_calls()) == 1


# ---------------------------------------------------------------- the prompt

def _prompt():
    from crawler.discover import DISCOVER_SYSTEM
    return DISCOVER_SYSTEM.lower()


def test_the_prompt_asks_for_the_brands_own_listing_pages_one_per_product_family():
    text = _prompt()
    assert re.search(r"\bown\b", text)
    assert "listing page" in text
    assert re.search(r"one per product famil", text)


def test_the_prompt_forbids_third_party_manual_sites():
    lines = [ln for ln in _prompt().splitlines() if "third" in ln and "manual" in ln]
    assert lines, "no line names third-party manual sites"
    assert any(re.search(r"\b(never|not|no)\b", ln) for ln in lines)


def test_the_prompt_still_excludes_ahri_and_asks_for_json_with_evidence():
    text = _prompt()
    assert "ahridirectory.org" in text and "ahrinet.org" in text
    assert "evidence_url" in text and "json" in text


def test_the_answer_schema_is_unchanged():
    from crawler.discover import DISCOVER_SCHEMA
    assert set(DISCOVER_SCHEMA["properties"]) == {"domains", "document_hosts", "start_urls"}
    assert set(DISCOVER_SCHEMA["required"]) == {"domains", "document_hosts", "start_urls"}


def test_the_prompt_also_asks_for_the_brands_document_library_page():
    """P11 (D60): the document library page is usually one or two clicks from the PDFs."""
    from crawler.discover import DISCOVER_SYSTEM
    assert re.search(r"document (library|literature)", DISCOVER_SYSTEM, flags=re.I)
