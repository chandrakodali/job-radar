"""Parsing helpers for career pages and feeds: JSON-LD JobPosting, sitemaps, RSS, links."""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin

# ---------------------------------------------------------------- dates

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_when(x) -> datetime | None:
    """Best-effort date parser for the many formats career sites use. Returns aware UTC."""
    if x is None or x == "":
        return None
    try:
        if isinstance(x, (int, float)) or (isinstance(x, str) and re.fullmatch(r"\d{9,13}", x.strip())):
            v = float(x)
            if v > 1e11:  # milliseconds
                v /= 1000
            if v < 9e8:
                return None
            return datetime.fromtimestamp(v, tz=timezone.utc)
        s = str(x).strip()
        m = re.fullmatch(r"/Date\((\d+)(?:[+-]\d+)?\)/", s)  # .NET JSON dates
        if m:
            return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc)
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?\s*(Z|UTC|[+-]\d{2}:?\d{2})?)?", s)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            hh, mm, ss = int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0)
            dt = datetime(y, mo, d, hh, mm, ss, tzinfo=timezone.utc)
            tz = m.group(7)
            if tz and tz not in ("Z", "UTC"):
                sign = 1 if tz[0] == "+" else -1
                t = tz[1:].replace(":", "")
                from datetime import timedelta
                dt -= sign * timedelta(hours=int(t[:2]), minutes=int(t[2:4] or 0))
            return dt
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)  # US style M/D/YYYY
        if m:
            return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)), tzinfo=timezone.utc)
        m = re.match(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", s)  # "Nov 5, 2026"
        if m and m.group(1).lower() in _MONTHS:
            return datetime(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)), tzinfo=timezone.utc)
        m = re.match(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})", s)  # "5 Nov 2026"
        if m and m.group(2).lower() in _MONTHS:
            return datetime(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)), tzinfo=timezone.utc)
        d = parsedate_to_datetime(s)  # RFC 822 (RSS pubDate)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError, IndexError):
        return None


# ---------------------------------------------------------------- JSON-LD

_LD = re.compile(r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.S | re.I)


def jsonld_objects(page: str) -> list[dict]:
    out: list[dict] = []
    for m in _LD.finditer(page or ""):
        raw = m.group(1).strip()
        if raw.startswith("<!--"):
            raw = raw[4:]
        if raw.endswith("-->"):
            raw = raw[:-3]
        raw = raw.strip().rstrip(";")
        data = None
        for cand in (raw, html.unescape(raw), re.sub(r",\s*([}\]])", r"\1", raw)):
            try:
                data = json.loads(cand, strict=False)
                break
            except ValueError:
                continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                out.append(x)
                if "@graph" in x:
                    stack.append(x["@graph"])
    return out


def find_jobposting(page: str) -> dict | None:
    for o in jsonld_objects(page):
        t = o.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(isinstance(tt, str) and tt.lower().endswith("jobposting") for tt in types):
            return o
    return None


def _name(v) -> str:
    if isinstance(v, dict):
        return str(v.get("name") or v.get("@id") or "").strip()
    if isinstance(v, list):
        return ", ".join(x for x in (_name(i) for i in v) if x)
    return str(v or "").strip()


def jobposting_location(p: dict) -> tuple[str, list[str], str]:
    """(location text, countries, workplace) from a JobPosting."""
    places = p.get("jobLocation") or []
    if isinstance(places, dict):
        places = [places]
    locs, countries = [], []
    for pl in places if isinstance(places, list) else []:
        if not isinstance(pl, dict):
            continue
        a = pl.get("address") or {}
        if isinstance(a, str):
            locs.append(a.strip())
            continue
        if isinstance(a, list):
            a = a[0] if a and isinstance(a[0], dict) else {}
        city = _name(a.get("addressLocality"))
        region = _name(a.get("addressRegion"))
        country = _name(a.get("addressCountry"))
        parts = [x for x in (city, region, country) if x and x.upper() not in ("UNAVAILABLE", "N/A", "NULL")]
        if parts:
            locs.append(", ".join(parts))
        if country and country.upper() not in ("UNAVAILABLE", "N/A"):
            countries.append(country)
    req = p.get("applicantLocationRequirements")
    if req:
        reqs = req if isinstance(req, list) else [req]
        for r in reqs:
            n = _name(r)
            if n:
                countries.append(n)
    loc_type = str(p.get("jobLocationType") or "").upper()
    workplace = "remote" if "TELECOMMUTE" in loc_type else ""
    seen, uniq = set(), []
    for l in locs:
        if l.lower() not in seen:
            seen.add(l.lower())
            uniq.append(l)
    loc = "; ".join(uniq[:4])
    if workplace == "remote" and not loc:
        loc = "Remote" + (f" ({', '.join(dict.fromkeys(countries))})" if countries else "")
    return loc, list(dict.fromkeys(countries)), workplace


def jobposting_fields(p: dict) -> dict:
    loc, countries, workplace = jobposting_location(p)
    desc = p.get("description") or ""
    for k in ("qualifications", "experienceRequirements", "responsibilities", "skills"):
        v = p.get(k)
        if isinstance(v, str) and v:
            desc += "\n" + v
        elif isinstance(v, dict) and v.get("description"):
            desc += "\n" + str(v["description"])
    return {
        "title": html.unescape(str(p.get("title") or "")).strip(),
        "posted": parse_when(p.get("datePosted")),
        "location": loc,
        "countries": countries,
        "workplace": workplace,
        "description": desc,
        "company": _name(p.get("hiringOrganization")),
        "id": _name(p.get("identifier")) if not isinstance(p.get("identifier"), dict)
              else str((p.get("identifier") or {}).get("value") or ""),
    }


# ---------------------------------------------------------------- sitemaps / RSS

_SM_URL = re.compile(r"<(?:\w+:)?url\b[^>]*>(.*?)</(?:\w+:)?url>", re.S | re.I)
_SM_MAP = re.compile(r"<(?:\w+:)?sitemap\b[^>]*>(.*?)</(?:\w+:)?sitemap>", re.S | re.I)
_LOC = re.compile(r"<(?:\w+:)?loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</(?:\w+:)?loc>", re.S | re.I)
_LASTMOD = re.compile(r"<(?:\w+:)?lastmod>\s*(.*?)\s*</(?:\w+:)?lastmod>", re.S | re.I)


def sitemap(xml: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Return ([(loc, lastmod)], [child sitemap locs])."""
    urls = []
    for m in _SM_URL.finditer(xml or ""):
        loc = _LOC.search(m.group(1))
        if loc:
            lm = _LASTMOD.search(m.group(1))
            urls.append((html.unescape(loc.group(1)), lm.group(1) if lm else ""))
    maps = []
    for m in _SM_MAP.finditer(xml or ""):
        loc = _LOC.search(m.group(1))
        if loc:
            maps.append(html.unescape(loc.group(1)))
    return urls, maps


_ITEM = re.compile(r"<item\b[^>]*>(.*?)</item>", re.S | re.I)


def _tag(block: str, name: str) -> str:
    m = re.search(rf"<{name}\b[^>]*>(.*?)</{name}>", block, re.S | re.I)
    if not m:
        return ""
    v = m.group(1).strip()
    if v.startswith("<![CDATA["):
        v = v[9:]
        if v.endswith("]]>"):
            v = v[:-3]
    return html.unescape(v.strip())


def rss_items(xml: str, extra: tuple[str, ...] = ()) -> list[dict]:
    out = []
    for m in _ITEM.finditer(xml or ""):
        b = m.group(1)
        d = {k: _tag(b, k) for k in ("title", "link", "guid", "pubDate", "description") + extra}
        out.append(d)
    return out


# ---------------------------------------------------------------- links

class _Anchors(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(" ".join(self._text).split())))
            self._href = None


def anchors(page: str, base: str = "") -> list[tuple[str, str]]:
    """All <a href> links as (absolute url, text)."""
    p = _Anchors()
    try:
        p.feed(page or "")
        p.close()
    except Exception:  # malformed markup: keep what was parsed
        pass
    return [(urljoin(base, h.strip()), t) for h, t in p.links if h and not h.startswith(("javascript:", "mailto:", "#"))]


def slug_words(slug: str) -> str:
    """'senior-devops-engineer-remote' -> 'senior devops engineer remote'."""
    s = re.sub(r"%[0-9A-Fa-f]{2}", " ", slug or "")
    s = re.sub(r"[-_+.]+", " ", s)
    return " ".join(s.split())


def bracket_json(text: str, marker: str, open_ch: str = "[", close_ch: str = "]"):
    """Parse the JSON array/object that starts right after `marker` in a page's script."""
    i = (text or "").find(marker)
    if i < 0:
        return None
    j = text.find(open_ch, i + len(marker))
    if j < 0:
        return None
    depth, in_str, esc = 0, False, False
    for k in range(j, len(text)):
        ch = text[k]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[j:k + 1], strict=False)
                except ValueError:
                    return None
    return None
