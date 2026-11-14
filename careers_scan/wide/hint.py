"""Read one known careers page (found by the Common Crawl sweep) the way scan.py reads a company, but
without the homepage crawl, guessed paths or tracking-system API probes: a few requests per site."""
from __future__ import annotations

import gzip
import html as htmllib
import re
import time
from datetime import datetime, timezone

from ..detect import analyze, match_vendor
from ..fetch import Page, decode_body
from ..scan import FIELDS, Company, Evidence, Scanner
from ..urls import canon, registrable_domain

BLOCK_STATUS = (401, 403, 429, 503)
# Job titles in IT / software / infrastructure (decides which sites are re-read daily).
TECH_RE = re.compile(
    r"\b(software|developer|devops|sre|site reliability|platform engineer|infrastructure|cloud|sysadmin|"
    r"systems? (administrator|engineer|analyst)|network (engineer|administrator)|help ?desk|"
    r"it (support|specialist|administrator|manager|technician|analyst)|data (engineer|analyst|scientist)|"
    r"security (engineer|analyst)|qa|quality assurance|full[- ]?stack|back[- ]?end|front[- ]?end|programmer|"
    r"database administrator|machine learning|kubernetes|linux)\b", re.I)


class HintScanner(Scanner):
    """Build with HintScanner(fetcher, probe=False). `fetcher` needs .page(url) -> Page, like fetch.Fetcher."""
    max_pages = 5      # page fetches per site (robots.txt not counted)
    max_detail = 2     # single-job pages opened when the listing shows no apply route

    def scan_hint(self, domain: str, url: str, alt_url: str = "", name: str = "") -> tuple[dict, dict]:
        """-> (row with scan.FIELDS, page) where page = url/html/title/text of the careers page that was read."""
        t0 = time.monotonic()
        c = Company(name=name or domain, website=f"https://{domain}/", careers_url=url, sources="wide")
        row = {k: "" for k in FIELDS}
        row.update(name=c.name, website=c.website, domain=domain, sources="wide",
                   scanned_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        ev = Evidence()
        page = {"url": "", "html": "", "title": "", "text": ""}
        domains = {domain}
        queue = [(u, False) for u in (url, alt_url) if u]
        seen: set[str] = set()
        detail_urls: set[str] = set()
        pages, first_info, first_error, blocked = 0, None, "", False
        while queue and pages < self.max_pages:
            if ev.ats:
                break                                   # decided: a tracking system
            if ev.emails and ev.careers_pages and not any(strong for _, strong in queue):
                break                                   # decided: apply by email
            if time.monotonic() - t0 > self.time_budget:
                ev.notes.append("time budget reached")
                break
            u, strong = queue.pop(0)
            if canon(u) in seen:
                continue
            seen.add(canon(u))
            pg = self.f.page(u)
            pages += 1
            v = match_vendor(pg.url)
            if v and canon(pg.url) != canon(u):         # redirected straight to a job board or form
                ev.add_vendor(v.kind, v.name, pg.url, "careers redirect")
                continue
            if pg.error or pg.status >= 400:
                first_error = first_error or pg.error or f"http_{pg.status}"
                blocked = blocked or pg.status in BLOCK_STATUS
                if pg.error == "robots":
                    ev.notes.append("robots.txt disallows the careers page")
                continue
            info = analyze(pg.text, pg.url, pg.status)
            first_info = first_info or info
            if info.blocked:
                blocked = True
                continue
            if info.soft_404 or info.parked:
                continue
            is_detail = u in detail_urls
            if not (info.is_careers_like or strong or is_detail):
                for score, link in info.careers_links[:2]:   # not a careers page, but it may link to one
                    if not link.vendor and score >= 4 and registrable_domain(link.url) in domains:
                        queue.append((link.url, True))
                continue
            rd = registrable_domain(pg.url)
            if rd and rd not in domains:                # the company moved to another domain
                domains.add(rd)
                row["final_site"] = rd
            where = "job page" if is_detail else "careers page"
            if not is_detail and pg.url not in ev.careers_pages:
                ev.careers_pages.append(pg.url)
                if not page["html"]:
                    page.update(url=pg.url, html=pg.text, title=info.title, text=info.text)
            self._take_vendor_links(ev, info, c, domains, True, where)
            self._take_page(ev, info, True, where, domains)
            for score, link in info.careers_links[:2]:  # "See open roles" on a landing page
                if not link.vendor and registrable_domain(link.url) in domains:
                    queue.insert(0, (link.url, score >= 4))
            if not ev.ats and not ev.emails and not ev.forms:
                for link in info.detail_links:
                    if len(detail_urls) >= self.max_detail:
                        break
                    if canon(link.url) not in seen:
                        detail_urls.add(link.url)
                        queue.append((link.url, True))
        row = self._finish(row, ev, pages, c=c, domain=domain, home_info=first_info, home_error=first_error,
                           blocked_home=blocked)
        return row, page


class OnePageFetcher:
    """Serves one already-downloaded page (e.g. Common Crawl's archived copy) to HintScanner."""

    def __init__(self, url: str, html: str):
        self.url, self.html = url, html

    def page(self, url: str, robots: bool = True) -> Page:
        if canon(url) == canon(self.url):
            return Page(url=url, status=200, ctype="text/html", text=self.html)
        return Page(url=url, status=404, ctype="text/html", text="")

    def json(self, url: str):
        return 404, None


def parse_warc_record(raw_gz: bytes) -> str:
    """HTML body of one gzip-compressed WARC response record (what a Range request to Common Crawl returns)."""
    raw = gzip.decompress(raw_gz)
    _warc_head, _, rest = raw.partition(b"\r\n\r\n")
    http_head, _, body = rest.partition(b"\r\n\r\n")
    m = re.search(rb"(?im)^content-type:\s*([^\r\n]+)", http_head)
    return decode_body(body, m.group(1).decode("latin-1") if m else "")


def archived_page(session, warc: str, timeout: float = 30) -> str:
    """The archived copy named by a candidate's `warc` field ('filename offset length'); '' on any failure."""
    try:
        name, off, length = warc.split()
        off, length = int(off), int(length)
        if not name or length <= 0 or length > 5_000_000:
            return ""
        r = session.get("https://data.commoncrawl.org/" + name,
                        headers={"Range": f"bytes={off}-{off + length - 1}"}, timeout=timeout)
        return parse_warc_record(r.content) if r.status_code == 206 else ""
    except Exception:  # noqa: BLE001 - the archive is a best-effort fallback
        return ""


_ABBR = ("AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|"
         "OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC")
_US_POS = (
    re.compile(rf"\b[A-Z][A-Za-z.' -]{{2,25}},\s*(?:{_ABBR})\b"),                      # Austin, TX
    re.compile(r"\(\d{3}\)\s?\d{3}[-.\s]\d{4}|\b\d{3}[-.]\d{3}[-.]\d{4}\b"),           # (512) 555-1212
    re.compile(r"\b(?:United States|USA|U\.S\.A?\.?)(?!\w)"),
    re.compile(r"equal opportunity employer|\bEOE\b|E-Verify|401\(?k\)?|"
               r"authorized to work in the (?:US|U\.S\.|United States)", re.I),
)
_US_NEG = (
    re.compile(r"[£€₹]|\b(?:GBP|EUR|INR|CTC|LPA)\b"),
    re.compile(r"\+(?:44|91|61|49|33|353|971|65|92|234|27)[\s(\d-]"),
    re.compile(r"\b(?:Ltd|Limited|Pvt|GmbH|PLC|Pty)\b"),
    re.compile(r"\b(?:United Kingdom|England|India|Canada|Australia|Germany|Ireland|Singapore|Pakistan|Nigeria|"
               r"South Africa|London|Bangalore|Bengaluru|Mumbai|Hyderabad|Pune|Chennai|Toronto|Vancouver|Sydney|"
               r"Melbourne|Dubai|Berlin)\b"),
)


def us_class(text: str) -> str:
    """'us', 'non_us' or 'unknown' from a page's visible text (addresses, phones, employer wording)."""
    t = (text or "")[:60000]
    pos = sum(1 for p in _US_POS if p.search(t))
    neg = sum(1 for p in _US_NEG if p.search(t))
    if pos and pos > neg:
        return "us"
    if neg >= 2 and not pos:
        return "non_us"
    return "unknown"


_OG = re.compile(r'<meta[^>]+(?:property|name)=["\']og:site_name["\'][^>]*content=["\']([^"\'<>]{2,80})["\']'
                 r'|<meta[^>]+content=["\']([^"\'<>]{2,80})["\'][^>]*(?:property|name)=["\']og:site_name["\']', re.I)
_TITLE_NOISE = re.compile(
    r"\b(careers?( page| opportunities)?|jobs?( openings| opportunities)?|join (us|our team|the team)|"
    r"work (with|for) us|employment( opportunities)?|we'?re hiring|now hiring|hiring|open (positions|roles)|"
    r"current openings|home)\b", re.I)


def guess_name(html: str, title: str, domain: str) -> str:
    """Company name for a site found by URL only: og:site_name, else the page title, else the domain."""
    m = _OG.search((html or "")[:300000])
    if m:
        return htmllib.unescape(m.group(1) or m.group(2)).strip()[:80]
    parts = []
    for p in re.split(r"\s+[|\-–—·:»]\s+|\s+at\s+", title or ""):
        p = " ".join(_TITLE_NOISE.sub(" ", htmllib.unescape(p)).split()).strip(" -|–—·:,")
        if 2 <= len(p) <= 60:
            parts.append(p)
    return parts[-1] if parts else domain.split(".")[0].replace("-", " ").title()
