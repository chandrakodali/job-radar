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
LOCATION_LINE_RE = re.compile(
    r"^(remote|hybrid|on-?site|onsite|in-?office|anywhere|worldwide|global)\b.{0,50}$"
    r"|^[A-Z][A-Za-z .'\-]{1,30},\s*([A-Z]{2}|[A-Z][a-z]+(?: [A-Z][a-z]+){0,2})(\s*\(.{1,20}\))?$"
    r"|^(united states|usa|us|u\.s\.|canada|united kingdom|uk|europe|emea|india|germany|france|spain|"
    r"netherlands|poland|portugal|brazil|mexico|singapore|australia|israel)\b.{0,40}$",
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
        if LOCATION_LINE_RE.match(extra) or WORKPLACE_RE.search(extra):
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


def _card_text(el, other_titles: set[str]) -> str:
    """Text of the largest block around a title that holds no other job title (its job card)."""
    best = el.get_text("\n")
    node = el
    for _ in range(5):
        node = node.parent
        if node is None or node.name in ("body", "html", "[document]"):
            break
        txt = node.get_text("\n")
        flat = " ".join(txt.split()).lower()
        if len(txt) > 800 or any(t in flat for t in other_titles):
            break
        best = txt
    return best


def location_from_lines(lines: list[str]) -> str:
    for ln in lines[:60]:
        ln = " ".join(ln.split())
        m = LOCATION_LABEL_RE.search(ln)
        if m:
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
        card = _card_text(el, flat_titles - {" ".join(title.split()).lower()})
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
                url = link
        if not apply_url:  # a form link elsewhere in the same card ("Apply" button)
            for a in (el.parent.find_all("a", href=True, limit=6) if el.parent is not None else []):
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
