"""The polite, safe fetcher.

One request at a time, at least 10 seconds between requests to a host, robots.txt obeyed
(RFC 9309) unless the user switched it off for that host (D67), approved hosts only, public
addresses only (resolved once per request and connected to by that address), caps counted on
decoded bytes. A host switched to the browser has its web pages rendered in headless Chromium
(D68a, crawler/render.py) under the same gap and address rules. State that must outlive the
process (the gap, the rest period, the robots cache, the page hashes) lives in the database.
Nothing here commits; the caller does.
"""
import hashlib
import ipaddress
import socket
import time
from collections import namedtuple
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx
from protego import Protego

from crawler import library
from crawler.hosts import in_scope, is_refused_host, normalize_host
from crawler.netpolicy import is_public_ip
from crawler.rules import is_pdf_link

USER_AGENT = "TracforceEquipmentBot/1.0 (+mailto:support+crawler@tracforce.ai)"
ROBOTS_TOKEN = "TracforceEquipmentBot"

MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_PDF_BYTES = 100 * 1024 * 1024  # Floating Air service manuals are 52-65 MB (user, 2026-10-28: 100 MB)
MAX_ROBOTS_BYTES = 512 * 1024
MAX_REDIRECTS = 5
GAP_SECONDS = 10
ROBOTS_TTL = timedelta(hours=24)
REST_AFTER_FAILURES = 5
REST_FOR = timedelta(hours=24)
RECHECK_AFTER = timedelta(days=30)
RETRY_FAILED_AFTER = timedelta(days=1)
MAX_URL_LENGTH = 2000
READ_DEADLINE_SECONDS = 120

HTML_TYPES = ("text/html", "application/xhtml+xml")
REDIRECTS = (301, 302, 303, 307, 308)

Target = namedtuple("Target", "scheme host port hostport path_query")
Raw = namedtuple("Raw", "status headers body too_big")


@dataclass
class Result:
    outcome: str  # fetched | not_modified | failed | refused
    reason: str = None
    http_status: int = None
    body: bytes = b""
    url: str = None
    is_pdf: bool = False
    page_id: int = None
    mime: str = None


class _Refused(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _ConnError(Exception):
    pass


def _default_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _default_resolver(host):
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise _ConnError(str(exc)) from exc
    return list(dict.fromkeys(info[4][0] for info in infos))


def _ceil_second(moment):
    """The database keeps whole seconds; round up so a stored wait is never shorter than asked."""
    if moment.microsecond:
        moment = moment.replace(microsecond=0) + timedelta(seconds=1)
    return moment


def _encoding_ok(headers):
    """No encoding, or exactly one gzip/deflate layer. Stacked layers can expand past the cap in one step."""
    layers = [e.strip().lower() for e in headers.get("content-encoding", "").split(",") if e.strip()]
    return len(layers) == 0 or (len(layers) == 1 and layers[0] in ("identity", "gzip", "deflate"))


def _body_limit(status, headers):
    if not 200 <= status < 300:
        return 0
    if not _encoding_ok(headers):
        return 0
    kind = headers.get("content-type", "").split(";")[0].strip().lower()
    if kind in HTML_TYPES:
        return MAX_HTML_BYTES
    if kind == "application/pdf":
        return MAX_PDF_BYTES
    return 0


OCTET_TYPES = ("application/octet-stream", "binary/octet-stream")


def _pdf_address_limit(status, headers):
    """A .pdf address: some hosts serve PDFs as octet-stream (files.myrheem.com, 2026-10-28); the body must still start
    with the PDF signature (fetch checks it)."""
    if 200 <= status < 300 and _encoding_ok(headers) and kind_of(headers) in OCTET_TYPES:
        return MAX_PDF_BYTES
    return _body_limit(status, headers)


def _robots_limit(status, headers):
    if not 200 <= status < 300:
        return 0
    if not _encoding_ok(headers):
        return 0
    return MAX_ROBOTS_BYTES


XML_TYPES = ("application/xml", "text/xml")
MAX_JSON_BYTES = 30 * 1024 * 1024  # one page of 10,000 ENERGY STAR rows


def _json_body_limit(status, headers):
    if 200 <= status < 300 and _encoding_ok(headers):
        if headers.get("content-type", "").split(";")[0].strip().lower() in ("application/json", "text/json"):
            return MAX_JSON_BYTES
    return 0


def _xml_body_limit(status, headers):
    """Sitemaps only: XML is accepted as well, under the same caps."""
    if 200 <= status < 300 and _encoding_ok(headers):
        if headers.get("content-type", "").split(";")[0].strip().lower() in XML_TYPES:
            return MAX_HTML_BYTES
    return _body_limit(status, headers)


def _feed_body_limit(status, headers):
    """Document feeds (D68b): JSON from a brand's approved host, under the same cap as ENERGY STAR pages."""
    return _json_body_limit(status, headers)


LIMITS = {"page": _body_limit, "xml": _xml_body_limit, "json": _feed_body_limit}


class Fetcher:
    def __init__(self, conn, *, resolver=None, now=None, sleep=None, ip_allowed=None, renderer=None):
        self.conn = conn
        self.resolver = resolver or _default_resolver
        self.now = now or _default_now
        self.sleep = sleep or time.sleep
        self.ip_allowed = ip_allowed or is_public_ip
        self.renderer = renderer  # made on first use, so a crawl with no browser host never loads Playwright

    # ------------------------------------------------------------ database helpers

    def _rows(self, sql, args=()):
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()

    def _exec(self, sql, args=()):
        with self.conn.cursor() as cur:
            cur.execute(sql, args)

    def _approved(self, brand_id):
        return [r[0] for r in self._rows(
            "SELECT host FROM brand_domains WHERE brand_id = %s AND status = 'approved'", (brand_id,))]

    def _host_row(self, host):
        self._exec("INSERT INTO hosts (host) VALUES (%s) ON CONFLICT (host) DO NOTHING", (host,))
        row = self._rows("SELECT id, rest_until, ignore_robots, use_browser FROM hosts WHERE host = %s", (host,))[0]
        return {"id": row[0], "rest_until": row[1], "ignore_robots": row[2], "use_browser": row[3]}

    def _page(self, brand_id, url):
        rows = self._rows(
            "SELECT id, content_sha, etag, last_modified, is_pdf FROM pages WHERE brand_id = %s AND url_sha = %s",
            (brand_id, _sha(url)))
        if not rows:
            return None
        return {"id": rows[0][0], "content_sha": rows[0][1], "etag": rows[0][2], "last_modified": rows[0][3], "is_pdf": rows[0][4]}

    def due_info(self, brand_id, url):
        """(due, page_id). Not due when the page was checked recently and its next check is still ahead."""
        rows = self._rows("SELECT next_check_at, id FROM pages WHERE brand_id = %s AND url_sha = %s",
                          (brand_id, _sha(url)))
        if not rows:
            return True, None
        due = not (rows[0][0] is not None and rows[0][0] > self.now())
        return due, rows[0][1]

    def is_due(self, brand_id, url):
        return self.due_info(brand_id, url)[0]

    def _save_page(self, brand_id, host_id, start_url_id, url, depth, existing, cols, bump_fail=False, parent_id=None):
        if existing is None:
            cols = dict(cols, brand_id=brand_id, host_id=host_id, start_url_id=start_url_id,
                        url=url, url_sha=_sha(url), depth=depth, parent_id=parent_id)
            if bump_fail:
                cols["fail_count"] = 1
            names = ", ".join(cols)
            marks = ", ".join(["%s"] * len(cols))
            return self._rows(f"INSERT INTO pages ({names}) VALUES ({marks}) RETURNING id", tuple(cols.values()))[0][0]
        sets = [f"{name} = %s" for name in cols]
        values = list(cols.values())
        if parent_id is not None:
            sets.append("parent_id = COALESCE(parent_id, %s)")
            values.append(parent_id)
        if bump_fail:
            sets.append("fail_count = fail_count + 1")
        self._exec(f"UPDATE pages SET {', '.join(sets)} WHERE id = %s", (*values, existing["id"]))
        return existing["id"]

    # ------------------------------------------------------------ host health

    def _host_ok(self, host_id):
        self._exec("UPDATE hosts SET failure_count = 0 WHERE id = %s AND failure_count > 0", (host_id,))

    def _host_failed(self, host_id):
        self._exec("UPDATE hosts SET failure_count = failure_count + 1 WHERE id = %s", (host_id,))
        count = self._rows("SELECT failure_count FROM hosts WHERE id = %s", (host_id,))[0][0]
        if count >= REST_AFTER_FAILURES:
            self._exec("UPDATE hosts SET rest_until = %s WHERE id = %s",
                       (_ceil_second(self.now() + REST_FOR), host_id))

    # ------------------------------------------------------------ one request

    def _wait_gap(self, host_id):
        waits = self._rows("SELECT next_allowed_at FROM hosts WHERE id = %s", (host_id,))[0][0]
        if waits is not None and waits > self.now():
            self.sleep((waits - self.now()).total_seconds())

    def _render(self, host_id, url, approved):
        """One page through headless Chromium, after the same gap as any request to the host. Raw, with the final URL."""
        from crawler.render import RenderError, Renderer
        self._wait_gap(host_id)
        if self.renderer is None:
            self.renderer = Renderer()
        try:
            page = self.renderer.render(url, resolver=self.resolver, ip_allowed=self.ip_allowed, approved_hosts=approved,
                                        user_agent=USER_AGENT)
        except RenderError as exc:
            raise _ConnError(str(exc)) from exc
        finally:
            self._exec("UPDATE hosts SET last_request_at = %s, next_allowed_at = %s WHERE id = %s",
                       (self.now(), _ceil_second(self.now() + timedelta(seconds=GAP_SECONDS)), host_id))
        too_big = len(page.html) > MAX_HTML_BYTES
        headers = httpx.Headers({"content-type": page.content_type or "text/html; charset=utf-8"})
        return Raw(page.status, headers, None if too_big else page.html, too_big), page.final_url

    def _get(self, host_id, target, headers, limit_for):
        """One GET to one host: resolve once, wait out the gap, connect to that address."""
        self._wait_gap(host_id)

        try:
            ips = list(self.resolver(target.host))
        except _ConnError:
            raise
        except Exception as exc:
            raise _ConnError(str(exc)) from exc
        if not ips:
            raise _ConnError("no address")
        if not all(self.ip_allowed(ip) for ip in ips):
            raise _Refused("ip")
        ip = ips[0]

        literal = f"[{ip}]" if ":" in ip else ip
        pinned = f"{target.scheme}://{literal}:{target.port}{target.path_query}"
        send = {"User-Agent": USER_AGENT, "Host": target.hostport, "Accept-Encoding": "gzip, deflate", **headers}
        gap = GAP_SECONDS
        raw = None
        try:
            with httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=False,
                              trust_env=False) as client:
                with client.stream("GET", pinned, headers=send,
                                   extensions={"sni_hostname": target.host}) as resp:
                    status = resp.status_code
                    limit = limit_for(status, resp.headers)
                    body, too_big = None, False
                    if limit:
                        chunks, size = [], 0
                        deadline = time.monotonic() + READ_DEADLINE_SECONDS
                        for chunk in resp.iter_bytes():
                            size += len(chunk)
                            if size > limit:
                                too_big = True
                                break
                            if time.monotonic() > deadline:
                                raise _ConnError("too slow")
                            chunks.append(chunk)
                        if not too_big:
                            body = b"".join(chunks)
                    raw = Raw(status, resp.headers, body, too_big)
        except _ConnError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise _ConnError(str(exc)) from exc
        finally:
            if raw is not None and raw.status in (429, 503):
                gap = self._retry_gap(raw.headers)
            self._exec("UPDATE hosts SET last_request_at = %s, next_allowed_at = %s WHERE id = %s",
                       (self.now(), _ceil_second(self.now() + timedelta(seconds=gap)), host_id))
        return raw

    def _retry_gap(self, headers):
        value = (headers.get("retry-after") or "").strip()
        seconds = None
        if value.isdigit():
            seconds = int(value)
        elif value:
            try:
                when = parsedate_to_datetime(value)
                seconds = (when.replace(tzinfo=None) - self.now()).total_seconds()
            except (TypeError, ValueError):
                seconds = None
        if seconds is None:
            return GAP_SECONDS * 2
        return min(max(seconds, GAP_SECONDS), 3600)

    # ------------------------------------------------------------ robots.txt

    def _robots_allows(self, host_id, target, url, approved):
        row = self._rows("SELECT robots_status, robots_body, robots_fetched_at FROM hosts WHERE id = %s",
                         (host_id,))[0]
        status, body, fetched = row
        if fetched is None or self.now() - fetched >= ROBOTS_TTL:
            status, body = self._fetch_robots(host_id, target, approved)
            self._exec("UPDATE hosts SET robots_status = %s, robots_body = %s, robots_fetched_at = %s WHERE id = %s",
                       (status, body, self.now(), host_id))
        if self._rows("SELECT ignore_robots FROM hosts WHERE id = %s", (host_id,))[0][0]:
            return True  # the user's per-host switch (D67); the gap and the honest agent still apply
        if status is None or status == 0 or status >= 500 or 300 <= status < 400:
            return False
        if 400 <= status < 500:
            return True
        return Protego.parse(body or "").can_fetch(url, ROBOTS_TOKEN)

    def _fetch_robots(self, host_id, target, approved):
        target = target._replace(path_query="/robots.txt")
        for _ in range(4):
            try:
                raw = self._get(host_id, target, {},
                                _robots_limit)
            except _ConnError:
                return 0, None
            location = raw.headers.get("location")
            if raw.status in REDIRECTS and location:
                nxt = urlsplit(urljoin(f"{target.scheme}://{target.hostport}/robots.txt", location))
                try:
                    ok = in_scope(nxt.geturl(), approved)
                    new_host = normalize_host(nxt.hostname or "")
                    port = nxt.port or (443 if nxt.scheme == "https" else 80)
                except ValueError:
                    ok = False
                if not ok:
                    return 0, None
                if new_host != target.host:
                    host_id = self._host_row(new_host)["id"]
                hostport = new_host + (f":{nxt.port}" if nxt.port else "")
                target = Target(nxt.scheme, new_host, port, hostport, "/robots.txt")
                continue
            if raw.too_big or (200 <= raw.status < 300 and raw.body is None):
                return 0, None
            return raw.status, (raw.body or b"").decode("utf-8", "replace")
        return 0, None

    # ------------------------------------------------------------ fetch

    def _target(self, url, approved):
        try:
            parts = urlsplit(url)
            scheme = parts.scheme.lower()
            if scheme not in ("http", "https"):
                raise _Refused("scheme")
            host = normalize_host(parts.hostname or "")
            explicit = parts.port
        except ValueError:
            raise _Refused("scope")
        if is_refused_host(host):
            raise _Refused("refused_host")
        if not in_scope(url, approved):
            raise _Refused("scope")
        path_query = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        return Target(scheme, host, explicit or (443 if scheme == "https" else 80),
                      host + (f":{explicit}" if explicit else ""), path_query)

    def get_json(self, url, allowed_hosts):
        """One JSON GET from a fixed list of hosts (no brand approval), with the same politeness and address rules."""
        allowed = sorted(allowed_hosts)
        host_id = None
        try:
            target = self._target(url, allowed)
            if target.host not in allowed:
                raise _Refused("scope")
            row = self._host_row(target.host)
            host_id = row["id"]
            if row["rest_until"] is not None:
                if row["rest_until"] > self.now():
                    raise _Refused("rest")
                self._exec("UPDATE hosts SET failure_count = 0, rest_until = NULL WHERE id = %s", (host_id,))
            if not self._robots_allows(host_id, target, url, allowed):
                raise _Refused("robots")
            raw = self._get(host_id, target, {"Accept": "application/json"}, _json_body_limit)
        except _Refused as refused:
            return Result("refused", refused.reason, url=url)
        except _ConnError:
            if host_id is not None:
                self._host_failed(host_id)
            return Result("failed", "network", url=url)
        if not 200 <= raw.status < 300:
            if raw.status == 429 or raw.status >= 500:
                self._host_failed(host_id)
            return Result("failed", f"http_{raw.status}", http_status=raw.status, url=url)
        if raw.too_big:
            return Result("failed", "too_big", http_status=raw.status, url=url)
        if raw.body is None:
            return Result("failed", "bad_content_type", http_status=raw.status, url=url)
        self._host_ok(host_id)
        return Result("fetched", http_status=raw.status, body=raw.body, url=url)

    def fetch(self, url, brand_id, *, depth=0, start_url_id=None, parent_id=None, xml=False, kind=None):
        """kind: 'page' (HTML or PDF), 'xml' (sitemaps) or 'json' (document feeds). xml=True is the older spelling."""
        kind = kind or ("xml" if xml else "page")
        url = urldefrag((url or "").strip())[0]
        if len(url) > MAX_URL_LENGTH:
            return Result("refused", "url", url=url)
        approved = self._approved(brand_id)
        existing = self._page(brand_id, url)
        current = url
        origin_host_id = None
        host_id = None

        def save(cols, bump_fail=False):
            return self._save_page(brand_id, origin_host_id, start_url_id, url, depth, existing, cols, bump_fail,
                                   parent_id)

        def failed(reason, status=None, host_failure=False):
            cols = {"next_check_at": self.now() + RETRY_FAILED_AFTER, "fetch_state": "failed"}
            if status is not None:
                cols["http_status"] = status
            page_id = save(cols, bump_fail=True)
            if host_failure and host_id is not None:
                self._host_failed(host_id)
            return Result("failed", reason, http_status=status, url=current, page_id=page_id)

        for hop in range(MAX_REDIRECTS + 1):
            conditional = {}
            try:
                target = self._target(current, approved)
                row = self._host_row(target.host)
                host_id = row["id"]
                if origin_host_id is None:
                    origin_host_id = host_id
                if row["rest_until"] is not None:
                    if row["rest_until"] > self.now():
                        raise _Refused("rest")
                    self._exec("UPDATE hosts SET failure_count = 0, rest_until = NULL WHERE id = %s", (host_id,))
                if not self._robots_allows(host_id, target, current, approved):
                    raise _Refused("robots")
                if kind == "page" and row["use_browser"] and not current.lower().split("?")[0].endswith(".pdf"):
                    raw, final = self._render(host_id, current, approved)
                    if raw.status in range(200, 300) and not raw.headers.get("content-type", "").startswith("text/html"):
                        raw = self._get(host_id, target, {}, _body_limit)  # not a web page after all: plain HTTP
                    elif final and final != current:
                        try:
                            landed = self._target(final, approved)
                        except _Refused:
                            raise _Refused("scope")
                        if landed.host != target.host:  # the browser was redirected: that host's robots.txt counts too
                            if not self._robots_allows(self._host_row(landed.host)["id"], landed, final, approved):
                                raise _Refused("robots")
                        current = final
                else:
                    if hop == 0 and existing and existing["content_sha"] and (
                            not existing["is_pdf"] or not library.configured() or library.has_file(existing["content_sha"])):
                        if existing["etag"]:
                            conditional["If-None-Match"] = existing["etag"]
                        if existing["last_modified"]:
                            conditional["If-Modified-Since"] = existing["last_modified"]
                    limit = _pdf_address_limit if kind == "page" and is_pdf_link(current) else LIMITS[kind]
                    raw = self._get(host_id, target, conditional, limit)
            except _Refused as refused:
                return Result("refused", refused.reason, url=current)
            except _ConnError:
                return failed("network", host_failure=True)

            location = raw.headers.get("location")
            if raw.status in REDIRECTS and location:
                if hop == MAX_REDIRECTS:
                    return failed("redirects")
                current = urldefrag(urljoin(current, location))[0]
                continue

            now = self.now()
            if raw.status == 304 and conditional:
                page_id = save({"fetched_at": now, "next_check_at": now + RECHECK_AFTER, "fail_count": 0,
                                "fetch_state": "fetched", "final_url": current})
                self._host_ok(host_id)
                return Result("not_modified", http_status=304, url=current, page_id=page_id)
            if not 200 <= raw.status < 300:
                return failed(f"http_{raw.status}", raw.status,
                              host_failure=raw.status == 429 or raw.status >= 500)
            if raw.too_big:
                return failed("too_big", raw.status)
            if raw.body is None:
                return failed("bad_content_type", raw.status)

            is_pdf = kind_of(raw.headers) == "application/pdf" or kind_of(raw.headers) in OCTET_TYPES
            if is_pdf and not raw.body.startswith(b"%PDF-"):
                return failed("not_pdf", raw.status)

            sha = hashlib.sha256(raw.body).hexdigest()
            cols = {
                "http_status": raw.status,
                "content_type": raw.headers.get("content-type", "")[:190],
                "etag": (raw.headers.get("etag") or None) and raw.headers.get("etag")[:255],
                "last_modified": (raw.headers.get("last-modified") or None) and raw.headers.get("last-modified")[:100],
                "content_sha": sha,
                "is_pdf": is_pdf,
                "server_date": (raw.headers.get("date") or None) and raw.headers.get("date")[:100],
                "fetched_at": now,
                "next_check_at": now + RECHECK_AFTER,
                "fail_count": 0,
                "fetch_state": "fetched",
                "final_url": current,
            }
            unchanged = existing is not None and existing["content_sha"] == sha
            if not unchanged:
                cols["extraction_status"] = "waiting"
            page_id = save(cols)
            self._host_ok(host_id)
            if unchanged:
                return Result("not_modified", http_status=raw.status, url=current, is_pdf=is_pdf, page_id=page_id,
                              body=raw.body if is_pdf else b"", mime=kind_of(raw.headers))
            return Result("fetched", http_status=raw.status, body=raw.body, url=current, is_pdf=is_pdf,
                          page_id=page_id, mime=kind_of(raw.headers))

        return failed("redirects")


def kind_of(headers):
    return headers.get("content-type", "").split(";")[0].strip().lower()


def _sha(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()
