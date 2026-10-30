"""P19 (D75, D76): a brand from the admin page. The user types a brand name into control.brands; the next `auto`
takes it on with no command typed: it links the row to a crawler brand, asks the AI where the brand publishes, approves
the brand's own site (its name is in the address), leaves every other host to the host judge (P18), and crawls. The
page's pause, priority, budget and host-approval switch reach the crawler at the start of every run; `./crawl stop`
and `./crawl add` write back to the page's table so the two never disagree.

Fake web server, fake AI, `_testing`, one rolled-back transaction per test. No network and no key."""
import uuid

from fakeai import FakeDiscoverAI
from p11support import serve, serve_pdf, world  # noqa: F401  (world is a fixture)
from p3support import q
from test_host_judge import Both, auto
from test_p11b_tagging import tags


class Discovering(Both):
    """Tagging, host check and discovery in one fake client, like the real AnthropicClient."""

    def __init__(self, answer, host_answer=lambda host: True):
        super().__init__(lambda r: tags(), host_answer)
        self.discovery = FakeDiscoverAI(answer)

    def supports_web_search(self, model):
        return self.discovery.supports_web_search(model)

    def discover(self, *a):
        return self.discovery.discover(*a)

    def discovered(self):
        return [c[2] for c in self.discovery.calls if c[0] == "discover"]


def control(conn, name):
    return q(conn, "SELECT c.state, c.crawl_brand_id, b.status, b.monthly_budget_usd, b.auto_hosts FROM control.brands c "
                   "LEFT JOIN brands b ON b.id = c.crawl_brand_id WHERE c.name_key = lower(%s)", (name,))[0]


def page_adds(conn, name, **cols):
    cols = {"name": name, "added_by": "admin page", **cols}
    q(conn, f"INSERT INTO control.brands ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
      tuple(cols.values()))


def a_new_brand(world):
    """A brand only the page knows, its site with a start page and one manual, and the AI's discovery answer."""
    word = "zorbex" + uuid.uuid4().hex[:6]
    name, own = f"{word.capitalize()} Comfort", f"www.{word}.test"
    serve(world, "/start", "/manual.pdf", host=own)
    serve_pdf(world, "/manual.pdf", host=own)
    answer = {"domains": [{"host": own, "evidence_url": f"https://{own}/about"},
                          {"host": "bigbox-retail.test", "evidence_url": f"https://{own}/where-to-buy"}],
              "document_hosts": [{"host": "docs-cdn.test", "evidence_url": f"https://{own}/support"}],
              "start_urls": [{"url": world.url(own, "/start"), "evidence_url": f"https://{own}/"}]}
    page_adds(world.conn, name)
    return name, own, Discovering(answer)


def hosts(conn, name):
    return dict((h, (s, scope)) for h, s, scope in q(
        conn, "SELECT d.host, d.status, d.scope FROM brand_domains d JOIN brands b ON b.id = d.brand_id "
              "WHERE b.name_key = lower(%s)", (name,)))


# ---------------------------------------------------------------- a brand name in, a crawled brand out

def test_a_brand_typed_on_the_page_is_discovered_approved_and_crawled_by_the_next_run(world):
    name, own, client = a_new_brand(world)
    summary = auto(world, client)
    assert client.discovered().count(name) == 1
    state, brand_id, status, _, _ = control(world.conn, name)
    assert state == "active" and brand_id is not None and status == "active"
    found = hosts(world.conn, name)
    assert found[own] == ("approved", "all")  # the brand's own site: its name is in the address
    assert found["bigbox-retail.test"][0] == "waiting" and found["docs-cdn.test"][0] == "waiting"
    assert q(world.conn, "SELECT state, decided_by FROM host_decisions WHERE brand_id = %s AND host = %s",
             (brand_id, own)) == (("confirmed", "rule"),)
    assert summary["brands"][name]["fetched"] >= 2 and summary["brands"][name]["tagged"] == 1
    assert any(own in line and "own site" in line for line in summary["hosts"])
    assert q(world.conn, "SELECT manuals FROM control.brand_status WHERE name = %s", (name,))[0][0] == 1


def test_discovery_is_not_asked_again_on_the_next_run(world):
    name, own, client = a_new_brand(world)
    auto(world, client)
    auto(world, client)
    assert client.discovered().count(name) == 1


def test_with_host_approval_off_the_own_site_waits_for_the_user(world):
    name, own, client = a_new_brand(world)
    q(world.conn, "UPDATE control.brands SET auto_hosts = false WHERE name = %s", (name,))
    summary = auto(world, client)
    assert hosts(world.conn, name)[own] == ("waiting", "all")
    assert any(name in line and own in line and "approval" in line for line in summary["needs_you"])


def test_a_brand_over_its_budget_is_not_discovered(world):
    name, own, client = a_new_brand(world)
    q(world.conn, "UPDATE control.brands SET monthly_budget_usd = 0 WHERE name = %s", (name,))
    auto(world, client)
    assert name not in client.discovered()


def test_a_host_without_the_brand_name_is_never_approved_as_its_own_site():
    from crawler.control import own_site
    assert own_site("Ruud", "www.ruud.com") and own_site("Ruud", "media.ruud.com")
    assert own_site("American Standard", "www.americanstandardair.com")
    assert own_site("Mitsubishi Electric", "www.mitsubishicomfort.com")
    assert own_site("LG", "www.lg.com") and not own_site("LG", "www.lgcorp-reviews.com")
    assert not own_site("Ruud", "www.homedepot.com") and not own_site("Ruud", "ruud.facebook.com")
    assert not own_site("A. O. Smith", "www.hotwater.com")


# ---------------------------------------------------------------- the page's controls reach the crawler

def test_a_page_brand_with_the_name_of_a_crawler_brand_is_linked_to_it(world):
    from crawler.control import sync
    q(world.conn, "UPDATE control.brands SET crawl_brand_id = NULL WHERE crawl_brand_id = %s", (world.brand_id,))
    sync(world.conn)
    assert control(world.conn, world.brand)[1] == world.brand_id


def test_pause_budget_and_switch_are_copied_and_resume_undoes_the_pause(world):
    from crawler.control import sync
    q(world.conn, "UPDATE control.brands SET state = 'paused', monthly_budget_usd = 9, auto_hosts = false "
                  "WHERE crawl_brand_id = %s", (world.brand_id,))
    sync(world.conn)
    assert control(world.conn, world.brand)[2:] == ("stopped", 9, False)
    assert {s for (s,) in q(world.conn, "SELECT status FROM start_urls WHERE brand_id = %s", (world.brand_id,))} <= {"stopped"}
    q(world.conn, "UPDATE control.brands SET state = 'active' WHERE crawl_brand_id = %s", (world.brand_id,))
    sync(world.conn)
    assert control(world.conn, world.brand)[2] == "active"


def test_an_archived_brand_is_not_crawled(world):
    q(world.conn, "UPDATE control.brands SET state = 'archived' WHERE crawl_brand_id = %s", (world.brand_id,))
    summary = auto(world, Discovering({"domains": [], "document_hosts": [], "start_urls": []}))
    assert world.brand not in summary["brands"]


def test_brands_run_in_the_page_priority_order(world):
    from crawler.brands import add_brand
    other = f"[T] Rheem {uuid.uuid4().hex[:8]}"
    add_brand(world.conn, other)
    q(world.conn, "UPDATE control.brands SET priority = 10 WHERE name = %s", (other,))
    q(world.conn, "UPDATE brands SET discovered_at = now() WHERE name IN (%s, %s)", (other, world.brand))
    summary = auto(world, Discovering({"domains": [], "document_hosts": [], "start_urls": []}))
    order = [b for b in summary["brands"] if b in (other, world.brand)]
    assert order == [other, world.brand]


def test_the_command_line_writes_back_to_the_page_table(world):
    from crawler.brands import add_brand, resume, stop
    assert control(world.conn, world.brand)[:2] == ("active", world.brand_id)  # ./crawl add made the page row
    stop(world.conn, world.brand)
    assert control(world.conn, world.brand)[0] == "paused"
    resume(world.conn, world.brand)
    assert control(world.conn, world.brand)[0] == "active"
    add_brand(world.conn, world.brand)  # adding again changes nothing
    assert q(world.conn, "SELECT COUNT(*) FROM control.brands WHERE crawl_brand_id = %s", (world.brand_id,))[0][0] == 1
