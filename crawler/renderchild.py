"""Renders one page in headless Chromium and prints {status, content_type, final_url, html} as JSON.

Run only as a child of crawler/render.py: it reads its settings from stdin, has an empty environment, and reaches
the network only through the parent's proxy. Navigation outside the approved hosts is refused; images, media and
fonts are never loaded.
"""
import json
import sys
from urllib.parse import urlsplit

SKIPPED_TYPES = {"image", "media", "font"}


def in_scope(url, approved):
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    return any(host == a or host.endswith("." + a) for a in approved)


def main():
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    config = json.loads(sys.stdin.read())
    approved = [a.lower() for a in config["approved"]]

    def route(r):
        request = r.request
        if request.resource_type in SKIPPED_TYPES:
            return r.abort()
        if request.is_navigation_request() and not in_scope(request.url, approved):
            return r.abort()
        return r.continue_()

    with sync_playwright() as p:
        browser = p.chromium.launch(proxy={"server": config["proxy"]}, args=["--proxy-bypass-list=<-loopback>"])
        try:
            context = browser.new_context(user_agent=config["user_agent"], accept_downloads=False,
                                          service_workers="block", ignore_https_errors=False)
            context.route("**/*", route)  # on the context, so popups a script opens are covered too
            page = context.new_page()
            response = page.goto(config["url"], wait_until="load", timeout=config["timeout_ms"])
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightError:
                pass  # a page that keeps polling is read as it stands
            if response is None:
                raise SystemExit("no response")
            headers = response.headers
            out = {"status": response.status, "content_type": headers.get("content-type", ""),
                   "final_url": page.url, "html": page.content()}
        finally:
            browser.close()
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main()
