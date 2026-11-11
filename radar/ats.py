"""Fetchers for the public job-board APIs of each applicant-tracking system.

Each list fetcher returns a list of Job objects. All endpoints used here are the
public, unauthenticated feeds that the companies' own careers pages load.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

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

def greenhouse_list(b: Board, content: bool = False) -> list[Job]:
    """`content=True` asks for every job's description in the same call (one bigger response instead of
    one request per job), so all jobs can be judged on their text."""
    _, slug = _split_eu(b.board)  # EU-hosted boards are served by the same API host
    host = "boards-api.greenhouse.io"
    data = get_json(f"https://{host}/v1/boards/{slug}/jobs", params={"content": "true"} if content else None)
    jobs = []
    for j in data.get("jobs") or []:
        loc = ((j.get("location") or {}).get("name") or "").strip()
        offices = [o.get("name") or "" for o in j.get("offices") or []]
        jobs.append(Job(
            description=html_to_text(j.get("content")) if content else "",
            countries=[o for o in offices if o] if content else [],
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

def smartrecruiters_list(b: Board, max_pages: int = 5, max_age_days: int = 45, search: str = "") -> list[Job]:
    """`search` runs the board's own full-text search (title and description) instead of listing by date."""
    jobs = []
    now = datetime.now(timezone.utc)
    for page in range(max_pages):
        params = {"limit": 100, "offset": page * 100, "country": "us"}
        if search:
            params["q"] = search
        data = get_json(f"https://api.smartrecruiters.com/v1/companies/{b.board}/postings", params=params)
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
                needs_detail=True, found_by=search,
                detail_ref=p.get("ref") or f"https://api.smartrecruiters.com/v1/companies/{b.board}/postings/{p.get('id')}",
            ))
        if len(content) < 100:
            break
        last = parse_iso(content[-1].get("releasedDate"))
        if not search and last and (now - last).days > max_age_days:
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


def _wd_hosts(board: str) -> tuple[str, str]:
    """(API base, public job-page base). Boards starting with '@' use the myworkdaysite.com host."""
    tenant, wd, site = _wd_parts(board)
    if tenant.startswith("@"):
        tenant = tenant[1:]
        return (f"https://{wd}.myworkdaysite.com/wday/cxs/{tenant}/{site}",
                f"https://{wd}.myworkdaysite.com/recruiting/{tenant}/{site}")
    return (f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}",
            f"https://{tenant}.{wd}.myworkdayjobs.com/{site}")


def workday_list(b: Board, max_pages: int = 5, max_age_days: int = 7, search: str = "") -> list[Job]:
    """`search` runs the board's own full-text search (title and description); results come back by
    relevance, so the age cut-off that ends a newest-first listing is not applied."""
    tenant, wd, site = _wd_parts(b.board)
    base, public = _wd_hosts(b.board)
    now = datetime.now(timezone.utc)
    jobs = []
    seen_paths = set()
    for page in range(max_pages):
        data = post_json(f"{base}/jobs", {"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": search})
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
                company=b.company or tenant.lstrip("@"), title=(p.get("title") or "").strip(),
                location=p.get("locationsText") or "",
                url=f"{public}{path}",
                posted_at=(now - timedelta(days=days)) if days is not None else None,
                workplace=workplace_from_text(p.get("locationsText") or ""),
                needs_detail=True, detail_ref=f"{base}{path}", found_by=search,
            ))
        if len(postings) < 20:
            break
        last_days = workday_days_ago(postings[-1].get("postedOn"))
        if not search and last_days is not None and last_days > max_age_days:
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


# ---------------------------------------------------------------- Oracle Cloud (Oracle Recruiting Cloud)

def _oracle_parts(board: str) -> tuple[str, str]:
    host, site = board.split("|", 1)
    return host, site


def oracle_list(b: Board, max_pages: int = 4, max_age_days: int = 7, search: str = "") -> list[Job]:
    """`search` runs the site's own keyword search (title and description) instead of listing by date."""
    host, site = _oracle_parts(b.board)
    base = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    now = datetime.now(timezone.utc)
    jobs, seen = [], set()
    keyword = f"keyword=%22{quote(search)}%22," if search else ""
    for page in range(max_pages):
        url = (f"{base}?onlyData=true&expand=requisitionList.secondaryLocations"
               f"&finder=findReqs;siteNumber={site},{keyword}limit=25,offset={page * 25},"
               f"sortBy={'RELEVANCY' if search else 'POSTING_DATES_DESC'}")
        data = get_json(url)
        items = (data.get("items") or [{}])[0].get("requisitionList") or []
        for r in items:
            rid = str(r.get("Id") or "")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            locs = [r.get("PrimaryLocation") or ""] + [s.get("Name") or "" for s in r.get("secondaryLocations") or []]
            wp = (r.get("WorkplaceType") or "").lower().replace("-", "").replace(" ", "")
            jobs.append(Job(
                ats="oracle", board=b.board, job_id=rid, company=b.company or site,
                title=(r.get("Title") or "").strip(), location="; ".join(l for l in locs if l),
                url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}",
                posted_at=parse_iso(r.get("PostedDate")),
                workplace={"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}.get(wp, workplace_from_text(*locs)),
                countries=[r["PrimaryLocationCountry"]] if r.get("PrimaryLocationCountry") else [],
                needs_detail=True, found_by=search,
                detail_ref=(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                            f"?expand=all&onlyData=true&finder=ById;Id=%22{rid}%22,siteNumber={site}"),
            ))
        if len(items) < 25:
            break
        last = parse_iso(items[-1].get("PostedDate"))
        if not search and last and (now - last).days > max_age_days:
            break
    return jobs


def oracle_detail(job: Job) -> None:
    d = get_json(job.detail_ref)
    item = (d.get("items") or [{}])[0]
    parts = [item.get(k) for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")]
    job.description = "\n".join(html_to_text(p) for p in parts if p)
    if item.get("PrimaryLocationCountry"):
        job.countries = [item["PrimaryLocationCountry"]]
    posted = parse_iso(item.get("ExternalPostedStartDate"))
    if posted:
        job.posted_at = posted


# ---------------------------------------------------------------- Workable

def _workable_job(b: Board, j: dict, locs: list, published) -> Job:
    sc = str(j.get("shortcode") or "")
    text = "; ".join(", ".join(x for x in [l.get("city"), l.get("region") or l.get("state"), l.get("country")] if x)
                     for l in locs)
    wp = (j.get("workplace") or "").lower().replace("_", "").replace("-", "")
    remote = j.get("remote") or j.get("telecommuting")
    return Job(
        ats="workable", board=b.board, job_id=sc, company=b.company or b.board,
        title=(j.get("title") or "").strip(), location=text,
        url=f"https://apply.workable.com/{b.board}/j/{sc}/",
        posted_at=parse_iso(published),
        workplace={"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}.get(wp, "remote" if remote else ""),
        countries=[l.get("countryCode") or l.get("country") for l in locs if l.get("countryCode") or l.get("country")],
        needs_detail=True, detail_ref=f"https://apply.workable.com/api/v2/accounts/{b.board}/jobs/{sc}",
    )


def workable_list(b: Board, max_pages: int = 3) -> list[Job]:
    # The embed-widget feed returns every job in one GET; the v3 search API pages and rate-limits hard.
    try:
        data = get_json(f"https://apply.workable.com/api/v1/widget/accounts/{b.board}")
        jobs = []
        for j in data.get("jobs") or []:
            if not j.get("shortcode"):
                continue
            locs = [l for l in (j.get("locations") or []) if not l.get("hidden")] or \
                   [{"city": j.get("city"), "region": j.get("state"), "country": j.get("country")}]
            jobs.append(_workable_job(b, j, locs, j.get("published_on") or j.get("created_at")))
        return jobs
    except FetchError as e:
        if e.status != 404:
            raise
    url = f"https://apply.workable.com/api/v3/accounts/{b.board}/jobs"
    payload: dict = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
    jobs, seen = [], set()
    for _ in range(max_pages):
        data = post_json(url, payload)
        fresh = 0
        for j in data.get("results") or []:
            sc = str(j.get("shortcode") or "")
            if not sc or sc in seen or j.get("isInternal"):
                continue
            seen.add(sc)
            fresh += 1
            locs = [l for l in (j.get("locations") or []) if not l.get("hidden")] or ([j["location"]] if j.get("location") else [])
            jobs.append(_workable_job(b, j, locs, j.get("published")))
        token = data.get("nextPage")
        if not token or not fresh:
            break
        payload = {**payload, "token": token}
    return jobs


def workable_detail(job: Job) -> None:
    d = get_json(job.detail_ref)
    job.description = "\n".join(html_to_text(d.get(k)) for k in ("description", "requirements", "benefits") if d.get(k))


# ---------------------------------------------------------------- Rippling

def rippling_list(b: Board) -> list[Job]:
    data = get_json(f"https://ats.rippling.com/api/v2/board/{b.board}/jobs")
    items = data.get("items") if isinstance(data, dict) else data
    jobs = []
    for j in items or []:
        jid = str(j.get("id") or j.get("uuid") or "")
        if not jid:
            continue
        locs = j.get("locations") or []
        text = "; ".join(l.get("name") or "" for l in locs if l.get("name"))
        wps = {(l.get("workplaceType") or "").lower() for l in locs}
        wp = "remote" if "remote" in wps else ("hybrid" if "hybrid" in wps else ("onsite" if "on_site" in wps else ""))
        jobs.append(Job(
            ats="rippling", board=b.board, job_id=jid, company=b.company or b.board,
            title=(j.get("name") or "").strip(), location=text,
            url=j.get("url") or f"https://ats.rippling.com/{b.board}/jobs/{jid}",
            workplace=wp, countries=[l.get("countryCode") for l in locs if l.get("countryCode")],
            needs_detail=True, detail_ref=f"https://ats.rippling.com/api/v2/board/{b.board}/jobs/{jid}",
        ))
    return jobs


def rippling_detail(job: Job) -> None:
    d = get_json(job.detail_ref)
    desc = d.get("description") or {}
    job.description = "\n".join(html_to_text(desc.get(k)) for k in ("role", "company") if desc.get(k))
    created = parse_iso(d.get("createdOn"))
    if created:
        job.posted_at = created


# ---------------------------------------------------------------- BambooHR

def bamboohr_list(b: Board) -> list[Job]:
    data = get_json(f"https://{b.board}.bamboohr.com/careers/list")
    jobs = []
    for j in data.get("result") or []:
        jid = str(j.get("id") or "")
        if not jid:
            continue
        ats_loc = j.get("atsLocation") or {}
        loc = j.get("location") or {}
        city = ats_loc.get("city") or loc.get("city")
        state = ats_loc.get("state") or loc.get("state")
        country = ats_loc.get("country")
        text = ", ".join(x for x in [city, state, country] if x)
        remote = bool(j.get("isRemote")) or str(j.get("locationType")) == "1" and not text
        jobs.append(Job(
            ats="bamboohr", board=b.board, job_id=jid, company=b.company or b.board,
            title=(j.get("jobOpeningName") or "").strip(), location=text or ("Remote" if remote else ""),
            url=f"https://{b.board}.bamboohr.com/careers/{jid}",
            workplace="remote" if j.get("isRemote") else "",
            countries=[country] if country else [],
            needs_detail=True, detail_ref=f"https://{b.board}.bamboohr.com/careers/{jid}/detail",
        ))
    return jobs


def bamboohr_detail(job: Job) -> None:
    d = get_json(job.detail_ref)
    o = (d.get("result") or {}).get("jobOpening") or {}
    job.description = html_to_text(o.get("description"))
    posted = parse_iso(o.get("datePosted"))
    if posted:
        job.posted_at = posted


# ---------------------------------------------------------------- Recruitee

def recruitee_list(b: Board) -> list[Job]:
    data = get_json(f"https://{b.board}.recruitee.com/api/offers/")
    jobs = []
    for o in data.get("offers") or []:
        if o.get("status") and o.get("status") != "published":
            continue
        locs = o.get("locations") or []
        text = "; ".join(", ".join(x for x in [l.get("city"), l.get("state"), l.get("country")] if x) for l in locs) \
            or o.get("location") or ""
        created = str(o.get("published_at") or o.get("created_at") or "").replace(" UTC", "+00:00").replace(" ", "T", 1)
        jobs.append(Job(
            ats="recruitee", board=b.board, job_id=str(o.get("id")), company=b.company or b.board,
            title=(o.get("title") or "").strip(), location=text,
            url=o.get("careers_url") or f"https://{b.board}.recruitee.com/o/{o.get('slug')}",
            posted_at=parse_iso(created),
            workplace="remote" if o.get("remote") else ("hybrid" if o.get("hybrid") else ""),
            countries=[l.get("country_code") or l.get("country") for l in locs if l.get("country_code") or l.get("country")],
            description="\n".join(html_to_text(o.get(k)) for k in ("description", "requirements") if o.get(k)),
        ))
    return jobs


LISTERS.update({"oracle": oracle_list, "workable": workable_list, "rippling": rippling_list,
                "bamboohr": bamboohr_list, "recruitee": recruitee_list})
DETAILERS.update({"oracle": oracle_detail, "workable": workable_detail, "rippling": rippling_detail,
                  "bamboohr": bamboohr_detail})

# More job systems (career-site pages, sitemaps and their JSON feeds); see radar/ats_more.py.
from . import ats_more as _more  # noqa: E402  (imports html_to_text from this module)

LISTERS.update(_more.LISTERS)
DETAILERS.update(_more.DETAILERS)
