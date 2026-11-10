"""Polite HTTP fetching for career pages: robots.txt, per-host pacing, size caps, clear error kinds."""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from radar.robots import is_allowed, parse as parse_robots

USER_AGENT = ("Mozilla/5.0 (compatible; job-radar-careers-scan/1.0; "
              "+https://github.com/chandrakodali/job-radar)")
ROBOTS_AGENT = "job-radar-careers-scan"   # robots.txt groups for "job-radar" also apply (substring match)
HTML_ACCEPT = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"
TEXTUAL = ("text/", "application/xhtml", "application/xml", "application/json", "application/ld+json")


@dataclass
class Page:
    url: str                 # final URL after redirects (or the requested URL on failure)
    status: int = 0          # HTTP status; 0 when no response
    ctype: str = ""
    text: str = ""
    error: str = ""          # "", dns, connect, timeout, ssl, redirects, invalid_url, network, robots, not_html

    @property
    def ok(self) -> bool:
        return not self.error and 200 <= self.status < 300


class _CappedRetry(Retry):
    def get_retry_after(self, response):
        v = super().get_retry_after(response)
        return None if v is None else min(v, 6)


def decode_body(raw: bytes, header_ctype: str) -> str:
    """Decode bytes using the HTTP charset, then <meta charset>, then UTF-8, then cp1252."""
    m = re.search(r"charset=([\w.:-]+)", header_ctype or "", re.I)
    candidates = [m.group(1)] if m else []
    mm = re.search(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([\w.:-]+)", raw[:4096], re.I)
    if mm:
        candidates.append(mm.group(1).decode("ascii", "ignore"))
    for enc in candidates:
        enc = enc.strip().lower()
        if enc in ("iso-8859-1", "latin-1", "latin1", "us-ascii", "ascii"):
            enc = "cp1252"  # what browsers actually use for these labels
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _error_kind(e: Exception) -> str:
    if isinstance(e, requests.exceptions.SSLError):
        return "ssl"
    if isinstance(e, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, requests.exceptions.Timeout)):
        return "timeout"
    if isinstance(e, requests.exceptions.TooManyRedirects):
        return "redirects"
    if isinstance(e, (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema,
                      requests.exceptions.InvalidSchema, UnicodeError, ValueError)):
        return "invalid_url"
    if isinstance(e, requests.exceptions.ConnectionError):
        s = str(e).lower()
        if any(k in s for k in ("name or service not known", "nodename nor servname", "failed to resolve",
                                "nameresolutionerror", "getaddrinfo failed", "no address associated",
                                "temporary failure in name resolution", "name resolution")):
            return "dns"
        if "timed out" in s:
            return "timeout"
        return "connect"
    if isinstance(e, requests.exceptions.ChunkedEncodingError):
        return "network"
    return "network"


class Fetcher:
    """Thread-safe page fetcher. One requests.Session per thread."""

    def __init__(self, timeout=(8, 20), max_bytes: int = 3_000_000, host_delay: float = 1.0,
                 use_robots: bool = True):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.host_delay = host_delay
        self.use_robots = use_robots
        self._local = threading.local()
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self._host_locks: dict[str, threading.Lock] = {}
        self._robots: dict[str, tuple[str, list]] = {}   # host -> (state, rules); state allow|rules|<error>
        self.requests_made = 0

    # -- plumbing
    def _session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            retry = _CappedRetry(total=2, connect=1, read=1, status=2, backoff_factor=1.0, backoff_max=6,
                                 status_forcelist=(429, 502, 503, 504), allowed_methods=frozenset(["GET", "HEAD"]),
                                 respect_retry_after_header=True, raise_on_status=False)
            adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
            s.mount("https://", adapter)
            s.mount("http://", adapter)
            s.max_redirects = 10
            s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"})
            self._local.s = s
        return s

    def _host_lock(self, host: str) -> threading.Lock:
        with self._lock:
            return self._host_locks.setdefault(host, threading.Lock())

    def _pace(self, host: str) -> None:
        """At most one request per host every `host_delay` seconds (across threads)."""
        if self.host_delay <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                wait = self._last.get(host, 0.0) + self.host_delay - now
                if wait <= 0:
                    self._last[host] = now
                    return
            time.sleep(min(wait, self.host_delay))

    def _raw_get(self, url: str, accept: str) -> Page:
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            return Page(url=url, error="invalid_url")
        if not host:
            return Page(url=url, error="invalid_url")
        self._pace(host)
        with self._lock:
            self.requests_made += 1
        try:
            r = self._session().get(url, timeout=self.timeout, headers={"Accept": accept}, stream=True,
                                    allow_redirects=True)
        except Exception as e:  # noqa: BLE001 - every failure becomes a labelled result
            return Page(url=url, error=_error_kind(e))
        try:
            ctype = (r.headers.get("Content-Type") or "").lower()
            final = r.url or url
            if ctype and not ctype.startswith(TEXTUAL):
                return Page(url=final, status=r.status_code, ctype=ctype, error="not_html")
            chunks, size = [], 0
            for chunk in r.iter_content(65536):
                chunks.append(chunk)
                size += len(chunk)
                if size >= self.max_bytes:
                    break
            return Page(url=final, status=r.status_code, ctype=ctype,
                        text=decode_body(b"".join(chunks), r.headers.get("Content-Type", "")))
        except Exception as e:  # noqa: BLE001
            return Page(url=r.url or url, status=r.status_code, error=_error_kind(e))
        finally:
            r.close()

    # -- robots.txt (RFC 9309)
    def robots_state(self, url: str) -> str:
        """'allow', 'deny', or an error kind when robots.txt itself could not be reached."""
        if not self.use_robots:
            return "allow"
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        key = f"{p.scheme}://{p.netloc.lower()}"
        with self._host_lock(key):
            rec = self._robots.get(key)
            if rec is None:
                rp = self._raw_get(f"{key}/robots.txt", "text/plain,*/*;q=0.5")
                if rp.error and rp.error != "not_html":
                    rec = (rp.error, [])            # host unreachable: report the network error
                elif rp.status in (429,) or rp.status >= 500:
                    rec = ("deny", [])              # RFC 9309: server errors mean "assume disallowed"
                elif 400 <= rp.status < 500 or rp.error == "not_html":
                    rec = ("rules", [])             # no robots.txt: everything allowed
                else:
                    body = rp.text if "<html" not in rp.text[:500].lower() else ""
                    rec = ("rules", parse_robots(body, ROBOTS_AGENT))
                self._robots[key] = rec
        state, rules = rec
        if state != "rules":
            return state
        path = (p.path or "/") + (("?" + p.query) if p.query else "")
        return "allow" if is_allowed(rules, path) else "deny"

    # -- public
    def page(self, url: str, robots: bool = True) -> Page:
        if robots:
            st = self.robots_state(url)
            if st == "deny":
                return Page(url=url, error="robots")
            if st != "allow":
                return Page(url=url, error=st)
        pg = self._raw_get(url, HTML_ACCEPT)
        if robots and pg.url != url:
            # Redirected (e.g. /careers -> careers.acme.com): the destination's robots.txt applies too.
            try:
                moved_host = urlsplit(pg.url).netloc.lower() != urlsplit(url).netloc.lower()
            except ValueError:
                moved_host = False
            if moved_host and self.robots_state(pg.url) == "deny":
                return Page(url=pg.url, error="robots")
        return pg

    def json(self, url: str):
        """GET a public JSON API (ATS board endpoints). Returns (status, data or None)."""
        p = self._raw_get(url, "application/json")
        if p.error or p.status != 200:
            return p.status, None
        try:
            import json as _json
            return p.status, _json.loads(p.text)
        except ValueError:
            return p.status, None
