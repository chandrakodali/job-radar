"""Read job titles off careers pages that have no job board, keep the cloud / DevOps / SRE / platform /
infrastructure ones, and build a quick-apply list.

The title rules, scoring and visa/clearance checks are the radar's own (config.yaml + radar/match.py),
so both lists agree. Used by watch.py, which already fetches every careers page each day.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import yaml

from radar.match import Rules, assess, title_category
from radar.models import Board, Job

from .detect import (CAREER_TEXT_RE, ROLE_RE, _soup, analyze, find_emails, email_kind, match_vendor)
from .urls import absolute, registrable_domain

CONFIG_PATH = "config.yaml"

# Lines that contain role words but are not job titles.
NOT_TITLE_RE = re.compile(
    r"^(meet|join|we|our|learn|apply|view|see|read|become|why|how|what|who|about|life|benefits|perks|explore|"
    r"discover|find|browse|check|contact|email|send|interested|don'?t|no|there|all|open|current|featured|"
    r"latest|more|back|share|follow|get|start|talk|book|schedule|hire|hiring|looking|want|help|introducing|"
    r"announcing|case study|customer|for|as|subject|re|experience|knowledge|deep|strong|solid|proficien\w*|"
    r"familiar\w*|understanding|credibility|ability|hands-on|excellent|good|working|bachelor\w*|degree|"
    r"certified|grab|tips|new announcement)\b"
    r"|\b(is|are) now\b|open[- ]source|\blaunch(es|ed)?\b|\breleased?\b|\bintroduc\w+"
    r"|\b(we|our|you|your|us|my|their|experience|years?|certified|certification|award|winner|show|"
    r"webinar|podcast|conference|summit|tips|blog)\b"
    r"|\b(19|20)\d{2}\b|[?!;]|\.$|:|https?://|@",
    re.I,
)
# A job title names a person doing the work, not just a field ("DevOps", "Cloud & DevOps", "DevOps Team").
PERSON_NOUN_RE = re.compile(
    r"\b(engineer|developer|administrator|admin|architect|specialist|analyst|operator|consultant|technician|"
    r"programmer|scientist|sre|lead|manager|intern|associate|officer|expert|sysops)\b",
    re.I,
)
SEPARATORS_RE = re.compile(r"\s+(?:[-–—|·•]|\bat\b|@)\s+")
TRAILING_JUNK_RE = re.compile(r"\s*(?:[→»›>]+|\b(apply( now)?|learn more|read more|view( job| role)?|details|new)\b)\s*$",
                              re.I)
US_STATES = ("alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|"
             "idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|"
             "minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|"
             "north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|"
             "south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming|"
             "district of columbia")
STATE_CODES = ("AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|"
               "NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC")
PLACES = (r"united states( of america)?|usa|us|u\.s\.a?\.?|north america|americas|canada|mexico|"
          r"united kingdom|uk|england|ireland|europe|emea|apac|latam|india|germany|france|spain|italy|netherlands|"
          r"belgium|switzerland|austria|sweden|norway|denmark|finland|poland|portugal|romania|ukraine|greece|"
          r"czech( republic)?|hungary|estonia|lithuania|latvia|serbia|croatia|bulgaria|turkey|israel|uae|"
          r"brazil|argentina|chile|colombia|peru|uruguay|singapore|australia|new zealand|japan|korea|china|"
          r"hong kong|taiwan|vietnam|philippines|indonesia|malaysia|thailand|pakistan|nigeria|kenya|"
          r"south africa|egypt|worldwide|anywhere|global|" + US_STATES)
WORKPLACE_WORDS = r"remote|hybrid|on-?site|onsite|in[- ]?office|in[- ]person"
_FILLER = re.compile(r"\b(in|within|from|only|ok|okay|friendly|preferred|based|first|or|and|the|time ?zones?|"
                     r"possible|available|position|role|option|optional|eligible|across|any(where)?|location|"
                     r"locations|full[- ]?time|part[- ]?time|contract)\b|[,;:/()\-–—&|.+]", re.I)
_CITY_STATE = re.compile(rf"^[A-Z][A-Za-z .'\-]{{1,30}},\s*(({STATE_CODES})\b|(?i:{PLACES})\b).{{0,25}}$")
_NOT_PLACE = re.compile(r"\b(cloud|multi|kubernetes|team|teams|work|culture|data|ai|platform|engineering|product|"
                        r"staff|technical|member|manager|engineer|devops|infrastructure|security|software)\b", re.I)
_PLACE_ONLY = re.compile(rf"^(({PLACES})\b|({STATE_CODES})\b)", re.I)


def looks_like_location(s: str) -> bool:
    """'Remote (US)', 'Austin, TX', 'Berlin, Germany', 'Hybrid - New York' yes; 'Kubernetes, Cloud' no."""
    s = " ".join((s or "").split()).strip(" .")
    if not (2 <= len(s) <= 70):
        return False
    if _NOT_PLACE.search(s):
        return False
    if _CITY_STATE.match(s):
        return True
    rest = re.sub(rf"^((fully|100%|full)\s+)?({WORKPLACE_WORDS})\b", "", s, flags=re.I).strip()
    had_workplace = rest != s
    words = [w for w in _FILLER.sub(" ", rest).split() if w]
    if not words:
        return had_workplace
    remainder = " ".join(words)
    if _CITY_STATE.match(rest) or re.fullmatch(rf"(({PLACES})|({STATE_CODES}))( ({PLACES}|{STATE_CODES}))*",
                                               remainder, re.I):
        return True
    # "Hybrid - New York": workplace word plus a capitalised place name
    return had_workplace and bool(re.fullmatch(r"([A-Z][a-z]+)( [A-Z][a-z]+){0,2}", remainder))


class _LocRE:
    """Keeps the old `LOCATION_LINE_RE.match(x)` call sites working with the stricter check."""

    @staticmethod
    def match(s: str):
        return looks_like_location(s) or None


LOCATION_LINE_RE = _LocRE()
# A role whose link leads to product or marketing pages is a menu item, not a job.
PRODUCT_LINK_RE = re.compile(
    r"/(solutions?|use-?cases?|by-role|roles?-we-serve|for-(teams|devops|sre|engineers|developers)|services?|"
    r"industries|products?|features?|integrations?|ai-operations|hire|hire-\w+|hiring-solutions|talent-solutions|"
    r"blog|news|docs?|documentation|customers|case-stud(y|ies)|pricing|resources|webinars?|events?|"
    r"partners?|platform-overview|technology)(/|$)|[?&]hire=",
    re.I,
)
LOCATION_LABEL_RE = re.compile(r"\blocation\s*[:\-–]\s*([^\n]{3,70})", re.I)
WORKPLACE_RE = re.compile(r"\b(remote|hybrid|on-?site|onsite|in[- ]office)\b", re.I)


def load_rules(path: str = CONFIG_PATH) -> tuple[Rules, dict]:
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    return Rules.from_config(cfg), cfg


@dataclass
class PageRole:
    title: str
    url: str                       # the job's own page, or the careers page when there is none
    page_url: str
    location: str = ""
    emails: list[str] = field(default_factory=list)
    apply_url: str = ""            # a form / document link next to the role


def _clean_title(text: str) -> tuple[str, str]:
    """'DevOps Engineer – Remote (US)' -> ('DevOps Engineer', 'Remote (US)')."""
    t = " ".join((text or "").split())
    t = TRAILING_JUNK_RE.sub("", t).strip(" -–—|·•:")
    parts = SEPARATORS_RE.split(t)
    title, loc = parts[0].strip(), ""
    for extra in parts[1:]:
        extra = extra.strip()
        if looks_like_location(extra):
            loc = loc or extra
    return title, loc


def is_title(t: str) -> bool:
    if not (4 <= len(t) <= 90) or len(t.split()) > 10:
        return False
    if NOT_TITLE_RE.search(t) or CAREER_TEXT_RE.fullmatch(t):
        return False
    if t.count("(") != t.count(")"):
        return False  # "Isaac Van Doren (Platform Engineer" - a team bio cut mid-sentence
    return bool(ROLE_RE.search(t)) and bool(PERSON_NOUN_RE.search(t))


def _link_for(el, page_url: str) -> tuple[str, str]:
    """(job page URL or '', mailto address or '') for a title element."""
    cands = []
    if el.name == "a" and el.get("href"):
        cands.append(el)
    cands += el.find_all("a", href=True, limit=3)
    parent, depth = el.parent, 0
    while parent is not None and depth < 3:
        if parent.name == "a" and parent.get("href"):
            cands.append(parent)
        parent, depth = parent.parent, depth + 1
    for a in cands:
        href = (a.get("href") or "").strip()
        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?", 1)[0].strip()
            return "", addr
        u = absolute(page_url, href)
        if u:
            return u, ""
    return "", ""


def _card(el, other_titles: set[str]):
    """The largest block around a title that holds no other job title (its job card): (element, text)."""
    best_el, best = el, el.get_text("\n")
    node = el
    for _ in range(5):
        node = node.parent
        if node is None or node.name in ("body", "html", "[document]"):
            break
        txt = node.get_text("\n")
        flat = " ".join(txt.split()).lower()
        if len(txt) > 800 or any(t in flat for t in other_titles):
            break
        best_el, best = node, txt
    return best_el, best


def location_from_lines(lines: list[str]) -> str:
    for ln in lines[:60]:
        ln = " ".join(ln.split())
        m = LOCATION_LABEL_RE.search(ln)
        if m and looks_like_location(m.group(1).strip(" .")):
            return m.group(1).strip(" .")
    for ln in lines[:60]:
        ln = " ".join(ln.split())
        if 2 <= len(ln) <= 70 and LOCATION_LINE_RE.match(ln):
            return ln
    return ""


TITLE_TAGS = ["a", "h1", "h2", "h3", "h4", "h5", "h6", "li", "strong", "b", "dt", "summary", "td", "th", "p",
              "span", "div", "button", "label"]


def extract_roles(html: str, page_url: str) -> list[PageRole]:
    """Every job-title-looking entry on a careers page (not yet filtered to infrastructure roles)."""
    soup = _soup(html)
    for t in soup(["script", "style", "noscript", "template", "svg", "nav", "header", "footer", "form",
                   "select", "title", "meta", "link"]):
        t.decompose()
    base_rd = registrable_domain(page_url)

    # Pass 1: the innermost element carrying each title.
    found = []
    for el in soup.find_all(TITLE_TAGS):
        raw = el.get_text(" ")
        if len(raw) > 160 or not raw.strip():
            continue
        title, loc = _clean_title(raw)
        if not is_title(title):
            continue
        inner = False
        for child in el.find_all(TITLE_TAGS):
            ct, _ = _clean_title(child.get_text(" "))
            if is_title(ct):
                inner = True
                break
        if not inner:
            found.append((el, title, loc))
    keys = {re.sub(r"[^a-z0-9]+", " ", t.lower()).strip() for _, t, _ in found}
    flat_titles = {" ".join(t.split()).lower() for _, t, _ in found}

    roles: dict[str, PageRole] = {}
    for el, title, loc in found:
        key = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
        link, mail = _link_for(el, page_url)
        card_el, card = _card(el, flat_titles - {" ".join(title.split()).lower()})
        card_lines = [x for x in (" ".join(l.split()) for l in card.splitlines()) if x and x != title]
        loc = loc or location_from_lines(card_lines)
        if not loc:  # "<h3>Title</h3><p>Berlin, Germany</p>" with no wrapping card
            sib = el
            for _ in range(2):
                sib = sib.find_next_sibling() if sib is not None else None
                if sib is None:
                    break
                st, _x = _clean_title(sib.get_text(" "))
                if is_title(st):
                    break
                loc = location_from_lines([" ".join(sib.get_text(" ").split())])
                if loc:
                    break
        emails = [mail] if mail else []
        for e, _pos in find_emails(card):
            if e not in emails and email_kind(e, card):
                emails.append(e)
        url, apply_url = page_url, ""
        if link:
            v = match_vendor(link)
            if v and v.kind in ("form", "doc"):
                apply_url = link
            elif registrable_domain(link) == base_rd and \
                    link.split("#")[0].rstrip("/") != page_url.split("#")[0].rstrip("/"):
                if PRODUCT_LINK_RE.search(urlsplit(link).path + ("?" + urlsplit(link).query if urlsplit(link).query else "")):
                    continue  # a menu link to a product page that happens to be named like a role
                url = link
        if PRODUCT_LINK_RE.search(urlsplit(page_url).path + ("?" + urlsplit(page_url).query if urlsplit(page_url).query else "")):
            continue  # the "careers page" itself is a product page
        if not apply_url and card_el is not el:  # a form link in the same card ("Apply" button)
            for a in card_el.find_all("a", href=True, limit=8):
                u = absolute(page_url, a["href"])
                v = match_vendor(u) if u else None
                if v and v.kind in ("form", "doc"):
                    apply_url = u
                    break
        cur = roles.get(key)
        if cur is None:
            roles[key] = PageRole(title=title, url=url, page_url=page_url, location=loc, emails=emails,
                                  apply_url=apply_url)
        else:
            if cur.url == page_url and url != page_url:
                cur.url = url
            cur.location = cur.location or loc
            cur.apply_url = cur.apply_url or apply_url
            cur.emails += [e for e in emails if e not in cur.emails]
    _ = keys
    return list(roles.values())


def read_job_page(html: str, url: str, company_domains: set[str]) -> dict:
    """Description, location, apply addresses and form links from a single job's page."""
    info = analyze(html, url)
    lines = info.text.splitlines()
    emails = []
    for e, kind, _ in info.emails:
        rd = registrable_domain(e.split("@", 1)[1])
        if kind == "hiring" or rd in company_domains or rd in ("gmail.com", "outlook.com", "proton.me"):
            emails.append(e)
    forms = [l.url for l in info.vendor_links if l.kind in ("form", "doc")]
    ats = [l.url for l in info.vendor_links if l.kind == "ats"]
    return {"description": info.text[:8000], "location": location_from_lines(lines), "emails": emails,
            "apply_url": (forms or ats or [""])[0], "blocked": info.blocked or info.soft_404}


def role_key(domain: str, title: str) -> str:
    return domain + "|" + re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def mailto(email: str, title: str, company: str) -> str:
    subject = quote(f"Application: {title} – {company}"[:150])
    return f"mailto:{email}?subject={subject}"


def assess_role(rules: Rules, company: str, domain: str, title: str, category: str, location: str,
                description: str, url: str, now: datetime | None = None):
    workplace = ""
    m = WORKPLACE_RE.search(location or "")
    if m:
        w = m.group(1).lower().replace("-", "").replace(" ", "")
        workplace = {"onsite": "onsite", "inoffice": "onsite"}.get(w, w)
    job = Job(ats="careerpage", board=domain, job_id=hashlib.sha1(title.lower().encode()).hexdigest()[:12],
              company=company, title=title, location=location, url=url, workplace=workplace,
              description=description)
    board = Board(ats="careerpage", board=domain, company=company, named=True, tier="found")
    return assess(job, board, rules, category, now=now or datetime.now(timezone.utc))


def infra_category(title: str, rules: Rules) -> str | None:
    return title_category(title, rules)


def is_careers_url(url: str) -> bool:
    """Role titles are only read from careers pages: a path (acme.com/careers) or a careers host
    (careers.acme.com). A bare homepage has product headings that look like titles."""
    try:
        p = urlsplit(url)
    except ValueError:
        return False
    if (p.path or "/").strip("/"):
        return True
    return (p.hostname or "").split(".")[0] in ("careers", "career", "jobs", "join", "work", "hiring", "karriere")


__all__ = ["PageRole", "extract_roles", "read_job_page", "assess_role", "infra_category", "load_rules",
           "role_key", "mailto", "is_careers_url", "location_from_lines"]
_ = os  # keep import for callers that patch paths
