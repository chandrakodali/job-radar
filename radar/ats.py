"""Fetchers for the public job-board APIs of each applicant-tracking system.

Each list fetcher returns a list of Job objects. All endpoints used here are the
public, unauthenticated feeds that the companies' own careers pages load.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

from .http import FetchError, get_json, post_json
from .models import Board, Job

# ---------------------------------------------------------------- helpers

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    if "&lt;" in s or "&gt;" in s:  # Greenhouse content is entity-escaped HTML
        s = html.unescape(s)
    s = re.sub(r"(?i)<\s*(br|/p|/li|/div|/h\d)\s*/?>", "\n", s)
    s = _TAG.sub(" ", s)
    s = _WS.sub(" ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def parse_iso(s) -> datetime | None:
    if not s:
        return None
    try:
        if isinstance(s, (int, float)):  # epoch milliseconds (Lever)
            return datetime.fromtimestamp(s / 1000, tz=timezone.utc)
        s = str(s).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        s = s.replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def workplace_from_text(*parts: str) -> str:
    t = " ".join(p for p in parts if p).lower()
    if "hybrid" in t:
        return "hybrid"
    if "remote" in t:
        return "remote"
    if "on-site" in t or "onsite" in t or "in office" in t or "in-office" in t:
        return "onsite"
    return ""


def _split_eu(board: str) -> tuple[bool, str]:
    if board.startswith("eu:"):
        return True, board[3:]
    return False, board


# ---------------------------------------------------------------- Greenhouse

def greenhouse_list(b: Board) -> list[Job]:
    _, slug = _split_eu(b.board)  # EU-hosted boards are served by the same API host
    host = "boards-api.greenhouse.io"
    data = get_json(f"https://{host}/v1/boards/{slug}/jobs")
    jobs = []
    for j in data.get("jobs") or []:
        loc = ((j.get("location") or {}).get("name") or "").strip()
        jobs.append(Job(
            ats="greenhouse", board=b.board, job_id=str(j.get("id")),
            company=b.company or j.get("company_name") or slug,
            title=(j.get("title") or "").strip(), location=loc,
            url=j.get("absolute_url") or "",
            posted_at=parse_iso(j.get("first_published") or j.get("updated_at")),
            workplace=workplace_from_text(loc),
            needs_detail=True, detail_ref=f"https://{host}/v1/boards/{slug}/jobs/{j.get('id')}",
        ))
    return jobs


def greenhouse_detail(job: Job) -> None:
    d = get_json(job.detail_ref)
    job.description = html_to_text(d.get("content"))
    offices = [o.get("name") or "" for o in d.get("offices") or []]
    job.countries = [o for o in offices if o]
    if not job.workplace:
        job.workplace = workplace_from_text(job.location, " ".join(offices))


# ---------------------------------------------------------------- Lever

def lever_list(b: Board) -> list[Job]:
    eu, slug = _split_eu(b.board)
    host = "api.eu.lever.co" if eu else "api.lever.co"
    data = get_json(f"https://{host}/v0/postings/{slug}", params={"mode": "json"})
    if isinstance(data, dict):  # error object, e.g. {"ok": false, "error": "Document not found"}
        raise FetchError(str(data.get("error") or "unexpected response"), 404)
    jobs = []
    for p in data:
        cat = p.get("categories") or {}
        locs = cat.get("allLocations") or ([cat.get("location")] if cat.get("location") else [])
        lists = " ".join(f"{li.get('text', '')}\n{html_to_text(li.get('content'))}" for li in p.get("lists") or [])
        desc = "\n".join(x for x in [p.get("descriptionPlain") or "", lists, p.get("additionalPlain") or ""] if x)
        wp = (p.get("workplaceType") or "").lower()
        jobs.append(Job(
            ats="lever", board=b.board, job_id=str(p.get("id")),
            company=b.company or slug, title=(p.get("text") or "").strip(),
            location="; ".join(l for l in locs if l),
            url=p.get("hostedUrl") or "",
            posted_at=parse_iso(p.get("createdAt")),
            workplace=wp if wp in ("remote", "hybrid", "onsite") else workplace_from_text(*locs),
            countries=[p["country"]] if p.get("country") else [],
            description=desc,
        ))
    return jobs


# ---------------------------------------------------------------- Ashby

def ashby_list(b: Board) -> list[Job]:
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{b.board}",
                    params={"includeCompensation": "false"})
    jobs = []
    for j in data.get("jobs") or []:
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [s.get("location") or "" for s in j.get("secondaryLocations") or []]
        countries = []
        for a in [j.get("address")] + [s.get("address") for s in j.get("secondaryLocations") or []]:
            c = (((a or {}).get("postalAddress") or {}).get("addressCountry") or "").strip()
            if c:
                countries.append(c)
        wp = (j.get("workplaceType") or "").lower().replace("-", "")
        wp = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}.get(wp, "")
        if not wp and j.get("isRemote"):
            wp = "remote"
        jobs.append(Job(
            ats="ashby", board=b.board, job_id=str(j.get("id")),
            company=b.company or b.board, title=(j.get("title") or "").strip(),
            location="; ".join(l for l in locs if l),
            url=j.get("jobUrl") or "",
            posted_at=parse_iso(j.get("publishedAt")),
            workplace=wp, countries=countries,
            description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
        ))
    return jobs


# ---------------------------------------------------------------- SmartRecruiters

def smartrecruiters_list(b: Board, max_pages: int = 5, max_age_days: int = 45) -> list[Job]:
    jobs = []
    now = datetime.now(timezone.utc)
    for page in range(max_pages):
        data = get_json(f"https://api.smartrecruiters.com/v1/companies/{b.board}/postings",
                        params={"limit": 100, "offset": page * 100, "country": "us"})
        content = data.get("content") or []
        for p in content:
            loc = p.get("location") or {}
            full = loc.get("fullLocation") or ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
            wp = "remote" if loc.get("remote") else ("hybrid" if loc.get("hybrid") else "")
            ident = (p.get("company") or {}).get("identifier") or b.board
            jobs.append(Job(
                ats="smartrecruiters", board=b.board, job_id=str(p.get("id")),
                company=b.company or (p.get("company") or {}).get("name") or b.board,
                title=(p.get("name") or "").strip(), location=full,
                url=f"https://jobs.smartrecruiters.com/{ident}/{p.get('id')}",
                posted_at=parse_iso(p.get("releasedDate")),
                workplace=wp, countries=[loc["country"]] if loc.get("country") else [],
                needs_detail=True,
                detail_ref=p.get("ref") or f"https://api.smartrecruiters.com/v1/companies/{b.board}/postings/{p.get('id')}",
            ))
        if len(content) < 100:
            break
        last = parse_iso(content[-1].get("releasedDate"))
        if last and (now - last).days > max_age_days:
            break
    return jobs


def smartrecruiters_detail(job: Job) -> None:
    d = get_json(job.detail_ref)
    sections = ((d.get("jobAd") or {}).get("sections") or {})
    parts = []
    for key in ("jobDescription", "qualifications", "additionalInformation"):
        sec = sections.get(key) or {}
        parts.append(html_to_text(sec.get("text")))
    job.description = "\n".join(p for p in parts if p)
    if d.get("postingUrl"):
        job.url = d["postingUrl"]


# ---------------------------------------------------------------- Workday

_WD_POSTED = re.compile(r"(?i)posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)")


def workday_days_ago(posted_on: str | None) -> int | None:
    m = _WD_POSTED.search(posted_on or "")
    if not m:
        return None
    word = m.group(1).lower()
    if word == "today":
        return 0
    if word == "yesterday":
        return 1
    n = int(m.group(2))
    return n + 1 if "+" in m.group(1) else n


def _wd_parts(board: str) -> tuple[str, str, str]:
    tenant, wd, site = board.split("|", 2)
    return tenant, wd, site


def workday_list(b: Board, max_pages: int = 5, max_age_days: int = 7) -> list[Job]:
    tenant, wd, site = _wd_parts(b.board)
    base = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    now = datetime.now(timezone.utc)
    jobs = []
    seen_paths = set()
    for page in range(max_pages):
        data = post_json(f"{base}/jobs", {"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": ""})
        postings = data.get("jobPostings") or []
        for p in postings:
            path = p.get("externalPath") or ""
            if not path or path in seen_paths:
                continue
            seen_paths.add(path)
            days = workday_days_ago(p.get("postedOn"))
            bullets = p.get("bulletFields") or []
            job_id = bullets[0] if bullets else path.rsplit("/", 1)[-1]
            jobs.append(Job(
                ats="workday", board=b.board, job_id=str(job_id),
                company=b.company or tenant, title=(p.get("title") or "").strip(),
                location=p.get("locationsText") or "",
                url=f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{path}",
                posted_at=(now - timedelta(days=days)) if days is not None else None,
                workplace=workplace_from_text(p.get("locationsText") or ""),
                needs_detail=True, detail_ref=f"{base}{path}",
            ))
        if len(postings) < 20:
            break
        last_days = workday_days_ago(postings[-1].get("postedOn"))
        if last_days is not None and last_days > max_age_days:
            break
    return jobs


def workday_detail(job: Job) -> None:
    d = get_json(job.detail_ref)
    info = d.get("jobPostingInfo") or {}
    job.description = html_to_text(info.get("jobDescription"))
    locs = [info.get("location") or ""] + list(info.get("additionalLocations") or [])
    locs = [l for l in locs if l]
    if locs:
        job.location = "; ".join(locs)
    country = (info.get("country") or {}).get("descriptor")
    if country:
        job.countries = [country]
    start = parse_iso(info.get("startDate"))
    if start:
        job.posted_at = start
    if info.get("externalUrl"):
        job.url = info["externalUrl"]
    if not job.workplace:
        job.workplace = workplace_from_text(job.location, info.get("remoteType") or "")


# ---------------------------------------------------------------- registry

LISTERS = {
    "greenhouse": greenhouse_list,
    "lever": lever_list,
    "ashby": ashby_list,
    "smartrecruiters": smartrecruiters_list,
    "workday": workday_list,
}

DETAILERS = {
    "greenhouse": greenhouse_detail,
    "smartrecruiters": smartrecruiters_detail,
    "workday": workday_detail,
}


def resolve_auto(slug: str) -> tuple[str, str] | None:
    """Find which ATS hosts a company slug. Returns (ats, company_name) or None."""
    try:
        d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
        if isinstance(d, dict) and d.get("name"):
            return "greenhouse", d["name"]
    except FetchError:
        pass
    try:
        d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        if isinstance(d, dict) and isinstance(d.get("jobs"), list) and d["jobs"]:
            return "ashby", slug
    except FetchError:
        pass
    try:
        d = get_json(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
        if isinstance(d, list) and d:
            return "lever", slug
    except FetchError:
        pass
    return None
