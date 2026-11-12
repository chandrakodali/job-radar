"""Readers for more job systems. Each lister returns Job objects; detailers fill in description,
date and location for the few jobs whose titles match.

Board formats (the `board` column):
  icims        careers-acme.icims.com           (host)
  jazzhr       acme                             (acme.applytojob.com)
  jobvite      acme                             (jobs.jobvite.com/acme)
  sfrmk        jobs.acme.com                    (SuccessFactors career site host)
  paylocity    0f1e...-guid                     (company GUID)
  taleo        acme|section                     (acme.taleo.net/careersection/<section>)
  avature      acme.avature.net|careers         (host|portal path)
  phenom       jobs.acme.com|us/en              (host|locale path)
  eightfold    acme|acme.com                    (acme.eightfold.ai|company domain)
  jibe         careers.acme.com                 (host serving /api/jobs)
  breezy       acme                             (acme.breezy.hr)
  pinpoint     acme                             (acme.pinpointhq.com)
  teamtailor   acme.teamtailor.com              (host, custom domains work too)
  dover        acme                             (app.dover.com/jobs/acme)
  gem          acme                             (jobs.gem.com/acme)
  ukg          host|TENANT|guid                 (recruiting.ultipro.com/TENANT/JobBoard/guid)
  ukgready     secure7.saashr.com|6203759       (host|company id)
  dayforce     tenant|site                      (jobs.dayforcehcm.com/en-US/tenant/site)
  adp          cid|ccId                         (workforcenow.adp.com recruitment cid / ccId)
  manatal      acme                             (careers-page.com/acme)
  hireology    acme                             (careers.hireology.com/acme)
  oorwin       acme                             (acme.oorwin.com)
  mcloud       2016|careers.acme.com            (Symphony Talent org number|careers site)
  dspace       www.dspace.com|en/inc/home/career/jobfinder/stellen.cfm   (dSPACE's own job finder)
"""
from __future__ import annotations

import html as htmllib
import re
from datetime import timezone
from urllib.parse import quote, urlencode, urljoin, urlsplit

from . import robots
from .feeds import (anchors, bracket_json, country_name, find_jobposting, jobposting_fields, parse_when, rss_items,
                    sitemap, slug_words)
from .http import FetchError, get_json, get_text, post_json
from .models import Board, Job

MAX_JOBS = 1500  # per board; guards against runaway pagination


def html_to_text(x) -> str:
    from .ats import html_to_text as h  # late import: radar.ats imports this module at its end
    return h(x)


def _page(url: str, **kw) -> str:
    robots.check(url)
    return get_text(url, **kw)


def _apply_ld(job: Job, page: str) -> bool:
    p = find_jobposting(page)
    if not p:
        return False
    f = jobposting_fields(p)
    if f["title"]:
        job.title = f["title"]
    if f["posted"]:
        job.posted_at = f["posted"]
    if f["location"]:
        job.location = f["location"]
    if f["countries"]:
        job.countries = f["countries"]
    if f["workplace"] and not job.workplace:
        job.workplace = f["workplace"]
    if f["description"]:
        job.description = html_to_text(htmllib.unescape(f["description"]))
    if f["company"] and job.company_guess:
        job.company = f["company"]
    return True


def _job(b: Board, **kw) -> Job:
    return Job(ats=b.ats, board=b.board, company=b.company, company_guess=not b.named, **kw)


def _loc(*parts) -> str:
    return ", ".join(str(p).strip() for p in parts if p and str(p).strip())


def _wp(text) -> str:
    t = str(text or "").lower().replace("_", "").replace("-", "").replace(" ", "")
    if "remote" in t or "telecommut" in t or "virtual" in t:
        return "remote"
    if "hybrid" in t:
        return "hybrid"
    if "onsite" in t or "office" in t or "inperson" in t:
        return "onsite"
    return ""


def _ld_detail(job: Job) -> None:
    _apply_ld(job, _page(job.detail_ref or job.url))


def _sitemap_urls(start: str, want=lambda u: True, max_children: int = 12) -> list[tuple[str, str]]:
    urls, maps = sitemap(_page(start, accept="application/xml,text/xml,*/*;q=0.5"))
    out = [u for u in urls if want(u[0])]
    for child in maps[:max_children]:
        try:
            cu, _ = sitemap(_page(child, accept="application/xml,text/xml,*/*;q=0.5"))
        except FetchError:
            continue
        out += [u for u in cu if want(u[0])]
        if len(out) > MAX_JOBS * 3:
            break
    return out


# ------------------------------------------------------------------ any career site with a job sitemap

def ldsite_list(b: Board, max_jobs: int = 4000) -> list[Job]:
    """A career site that lists its jobs in a sitemap and describes each job with schema.org JobPosting
    data (made for Google Jobs; Radancy/TalentBrew and many custom sites do). Board: "sitemap URL|regex"
    where the regex picks the job pages (and may capture the job id as its first group)."""
    sm, _, rx = b.board.partition("|")
    sm = sm if sm.startswith("http") else "https://" + sm
    pat = re.compile(rx or r"/jobs?/", re.I)
    jobs, seen = [], set()
    for u, lastmod in _sitemap_urls(sm, lambda u: bool(pat.search(u)), max_children=20):
        m = pat.search(u)
        jid = (m.group(1) if m and m.groups() and m.group(1) else
               re.sub(r"\W+", "-", urlsplit(u).path.strip("/"))[-60:])
        if jid in seen:
            continue
        seen.add(jid)
        # the title is the last part of the address that is not a number (.../job/<city>/<title>/<org>/<id>)
        parts = [x for x in urlsplit(u).path.split("/") if x and not re.fullmatch(r"[\d_-]+", x)]
        slug = parts[-1] if parts else ""
        jobs.append(_job(b, job_id=jid, title=slug_words(slug), url=u, title_guess=True,
                         posted_at=parse_when(lastmod), needs_detail=True, detail_ref=u))
        if len(jobs) >= max_jobs:
            break
    return jobs


# ------------------------------------------------------------------ iCIMS

_ICIMS_JOB = re.compile(r"/jobs/(\d+)/([^/?#]+)/job", re.I)


def icims_list(b: Board) -> list[Job]:
    host = b.board
    seen, jobs = set(), []
    try:
        found = [(m.group(1), m.group(2), lm) for u, lm in _sitemap_urls(f"https://{host}/sitemap.xml")
                 if (m := _ICIMS_JOB.search(u))]
    except FetchError as e:
        if e.status not in (404, 403, 999):
            raise
        found = []
        for page in range(0, 10):  # fallback: the search listing, 20-50 jobs a page
            html = _page(f"https://{host}/jobs/search?ss=1&in_iframe=1&pr={page}")
            got = [(m.group(1), m.group(2), "") for href, _ in anchors(html, f"https://{host}/")
                   if (m := _ICIMS_JOB.search(href))]
            fresh = [g for g in got if g[0] not in {f[0] for f in found}]
            if not fresh:
                break
            found += fresh
    for jid, slug, lastmod in found:
        if jid in seen or "intro" in slug.lower():
            continue
        seen.add(jid)
        url = f"https://{host}/jobs/{jid}/{slug}/job"
        jobs.append(_job(b, job_id=jid, title=slug_words(slug), url=url, title_guess=True, needs_detail=True,
                         detail_ref=url + "?in_iframe=1"))
    return jobs[:MAX_JOBS]


def icims_detail(job: Job) -> None:
    try:
        page = _page(job.detail_ref)
    except robots.Disallowed:
        page = _page(job.url)
    if not _apply_ld(job, page):
        title = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S | re.I)
        if title:
            job.title = html_to_text(title.group(1))
        job.description = html_to_text(page)[:20000]


# ------------------------------------------------------------------ JazzHR

def jazzhr_list(b: Board) -> list[Job]:
    base = f"https://{b.board}.applytojob.com"
    html = _page(f"{base}/apply")
    jobs, seen = [], set()
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for li in soup.select("li.list-group-item"):
        a = li.find("a", href=True)
        if not a:
            continue
        m = re.search(r"/apply/([A-Za-z0-9]+)/", a["href"])
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        loc = li.select_one(".fa-map-marker")
        loc_text = loc.parent.get_text(" ", strip=True) if loc else ""
        url = urljoin(base + "/", a["href"])
        jobs.append(_job(b, job_id=m.group(1), title=a.get_text(" ", strip=True), url=url, location=loc_text,
                         needs_detail=True, detail_ref=url))
    if not jobs:  # other theme: any /apply/<id>/<slug> link
        for href, text in anchors(html, base + "/"):
            m = re.search(r"applytojob\.com/apply/([A-Za-z0-9]+)/([^/?#]+)", href)
            if m and m.group(1) not in seen and text:
                seen.add(m.group(1))
                jobs.append(_job(b, job_id=m.group(1), title=text, url=href, needs_detail=True, detail_ref=href))
    return jobs


# ------------------------------------------------------------------ Jobvite

def jobvite_list(b: Board) -> list[Job]:
    base = f"https://jobs.jobvite.com/{b.board}"
    html = _page(f"{base}/jobs")
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    jobs, seen = [], set()
    for a in soup.find_all("a", href=True):
        m = re.search(r"/job/([A-Za-z0-9]+)", a["href"])
        if not m or m.group(1) in seen:
            continue
        title = a.get_text(" ", strip=True)
        if not title or len(title) > 200:
            continue
        seen.add(m.group(1))
        row = a.find_parent(["tr", "li", "div"])
        loc = ""
        if row is not None:
            el = row.select_one(".jv-job-list-location, .jv-job-location, [class*=location]")
            loc = el.get_text(" ", strip=True) if el else ""
        url = urljoin(base + "/", a["href"])
        jobs.append(_job(b, job_id=m.group(1), title=title, url=url, location=" ".join(loc.split()),
                         needs_detail=True, detail_ref=url))
    return jobs


def jobvite_detail(job: Job) -> None:
    page = _page(job.detail_ref)
    if not _apply_ld(job, page):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(page, "html.parser")
        d = soup.select_one(".jv-job-detail-description") or soup.body
        job.description = d.get_text("\n", strip=True)[:20000] if d else ""


# ------------------------------------------------------------------ SuccessFactors career sites (RMK)

_RMK_JOB = re.compile(r"/job/([^/]+)/(\d{5,})/?$")


# "...-Engineer-MI-48326": a US state and ZIP code ("BW-89081" is Baden-Württemberg, so the state is checked)
_RMK_US_SLUG = re.compile(
    r"-(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|"
    r"OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)-(\d{5})(-\d{4})?$")


def _rmk_slug_place(slug: str) -> tuple[str, list[str]]:
    m = _RMK_US_SLUG.search(slug)
    return (f"{m.group(1)} {m.group(2)}, United States", ["US"]) if m else ("", [])


def sfrmk_list(b: Board, max_jobs: int = MAX_JOBS) -> list[Job]:
    host = b.board
    try:
        urls = _sitemap_urls(f"https://{host}/job_sitemap.xml", lambda u: "/job/" in u)
    except FetchError as e:
        if e.status not in (404, 403):
            raise
        urls = _sitemap_urls(f"https://{host}/sitemap.xml", lambda u: "/job/" in u)
    jobs, seen = [], set()
    for u, lastmod in urls:
        m = _RMK_JOB.search(urlsplit(u).path)
        if not m or m.group(2) in seen:
            continue
        seen.add(m.group(2))
        place, countries = _rmk_slug_place(m.group(1))
        jobs.append(_job(b, job_id=m.group(2), title=slug_words(m.group(1)), url=u, title_guess=True,
                         posted_at=parse_when(lastmod), needs_detail=True, detail_ref=u,
                         location=place, countries=countries))
    if len(jobs) > max_jobs:   # worldwide sites: keep the jobs whose address looks American first
        jobs.sort(key=lambda j: not j.countries)
    return jobs[:max_jobs]


def sfrmk_search(b: Board, search: str, max_pages: int = 3) -> list[Job]:
    """The career site's own keyword search (title and description). Big sites list thousands of jobs
    worldwide, more than the sitemap reader keeps, so searching is how their US jobs are all reached."""
    from bs4 import BeautifulSoup
    host = b.board
    jobs, seen = [], set()
    for page in range(max_pages):
        html = _page(f"https://{host}/search/", params={"q": search, "locationsearch": "", "startrow": page * 25})
        if page == 0 and "SearchResultsUnify" in html and "data-row" not in html:
            return sfrmk_search_unify(b, search, max_pages=max_pages * 3)
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("tr.data-row") or soup.select("li.job-tile, div.job-tile")
        new = 0
        for row in rows:
            a = row.select_one("a.jobTitle-link") or row.select_one('a[href*="/job/"]')
            if not a or not a.get("href"):
                continue
            url = urljoin(f"https://{host}/", a["href"])
            m = _RMK_JOB.search(urlsplit(url).path)
            if not m or m.group(2) in seen:
                continue
            seen.add(m.group(2))
            new += 1
            loc_el = row.select_one(".jobLocation")
            loc, countries = rmk_location(loc_el.get_text(" ", strip=True)) if loc_el else ("", [])
            date_el = row.select_one(".jobDate")
            jobs.append(_job(b, job_id=m.group(2), title=a.get_text(" ", strip=True) or slug_words(m.group(1)),
                             url=url.split("?")[0], location=loc, countries=countries,
                             posted_at=parse_when(date_el.get_text(" ", strip=True)) if date_el else None,
                             needs_detail=True, detail_ref=url.split("?")[0], found_by=search))
        if not new or len(rows) < 25:
            break
    return jobs


def _unify_date(text) -> "datetime | None":
    """'9/1/26' (month/day/2-digit year), as the newer career sites write posting dates."""
    m = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\s*", str(text or ""))
    if not m:
        return parse_when(text)
    from datetime import datetime
    year = int(m.group(3))
    try:
        return datetime(year + 2000 if year < 100 else year, int(m.group(1)), int(m.group(2)), tzinfo=timezone.utc)
    except ValueError:
        return None


def sfrmk_search_unify(b: Board, search: str, max_pages: int = 9) -> list[Job]:
    """Keyword search on the newer SuccessFactors career sites, whose search page is filled in by
    JavaScript from a JSON service (10 results a page)."""
    host = b.board
    url = f"https://{host}/services/recruiting/v1/jobs"
    robots.check(url)
    jobs, seen = [], set()
    for page in range(max_pages):
        data = post_json(url, {"locale": "en_US", "pageNumber": page, "sortBy": "", "keywords": search,
                               "location": "", "facetFilters": {}, "brand": "", "skills": [], "categoryId": 0,
                               "alertId": "", "rcmCandidateId": ""})
        rows = data.get("jobSearchResult") or []
        for row in rows:
            r = row.get("response") or {}
            jid = str(r.get("id") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            slug = r.get("urlTitle") or r.get("unifiedUrlTitle") or "job"
            locs = [str(x).strip(" ,") for x in (r.get("jobLocationShort") or []) if str(x).strip(" ,")]
            page_url = f"https://{host}/job/{quote(slug)}/{jid}-en_US"
            jobs.append(_job(b, job_id=jid, title=(r.get("unifiedStandardTitle") or slug_words(slug)).strip(),
                             url=page_url, location="; ".join(locs[:4]),
                             countries=[str(c) for c in (r.get("country_obj") or [])][:4],
                             posted_at=_unify_date(r.get("unifiedStandardStart")),
                             needs_detail=True, detail_ref=page_url, found_by=search))
        if len(rows) < 10 or len(seen) >= int(data.get("totalJobs") or 0):
            break
    return jobs


def rmk_location(text: str) -> tuple[str, list[str]]:
    """SuccessFactors writes 'City, Region, CC, Zip' (e.g. 'Muenchen, DE' or 'Tampa, FL, US, 33605').
    The last letters-only part is the country code, so 'DE' is Germany here, not Delaware."""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    while parts and re.search(r"\d", parts[-1]):
        parts.pop()  # postal code
    if len(parts) >= 2 and re.fullmatch(r"[A-Z]{2}", parts[-1]):
        cc = parts[-1]
        return ", ".join(parts[:-1] + [country_name(cc)]), [cc]
    return ", ".join(parts), []


def sfrmk_detail(job: Job) -> None:
    page = _page(job.detail_ref)
    if _apply_ld(job, page):
        return
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page, "html.parser")
    t = soup.select_one('[itemprop="title"]') or soup.find("h1")
    if t and t.get_text(strip=True):
        job.title = t.get_text(" ", strip=True)
        job.title_guess = False
    loc = soup.select_one(".jobGeoLocation") or soup.select_one('[data-careersite-propertyid="location"]')
    if loc and loc.get_text(strip=True):
        job.location, job.countries = rmk_location(loc.get_text(" ", strip=True))
    else:
        parts = [m.get("content") for m in soup.select('meta[itemprop="addressLocality"], meta[itemprop="addressRegion"], meta[itemprop="addressCountry"]')]
        if any(parts):
            job.location = ", ".join(p for p in parts if p)
    og = soup.select_one('meta[property="og:site_name"]')
    if og and og.get("content") and job.company_guess:
        job.company = og["content"]
    dp = soup.select_one('[itemprop="datePosted"]')
    if dp:
        job.posted_at = parse_when(dp.get("content") or dp.get_text(strip=True)) or job.posted_at
    d = soup.select_one('[itemprop="description"], .jobdescription, .job-description')
    if d:
        job.description = d.get_text("\n", strip=True)[:20000]
        return
    # Newer career sites fill the job text in with JavaScript from a service their robots.txt closes to
    # automated readers. Take the title from the page head and leave the text unread rather than
    # mistaking the page's menus and footer for it.
    if job.title_guess:
        head = soup.select_one('meta[property="og:title"]')
        text = (head.get("content") if head else "") or (soup.title.get_text(" ", strip=True) if soup.title else "")
        text = re.split(r"\s+(?:Job Details|Stellendetails)\b|\s+\|\s+", text.strip())[0].strip()
        if text:
            job.title = text
            job.title_guess = False


# ------------------------------------------------------------------ Paylocity

def paylocity_list(b: Board) -> list[Job]:
    html = _page(f"https://recruiting.paylocity.com/Recruiting/Jobs/All/{b.board}")
    data = bracket_json(html, '"Jobs":') or []
    jobs = []
    for j in data:
        jid = str(j.get("JobId") or "")
        if not jid:
            continue
        jl = j.get("JobLocation") or {}
        loc = _loc(jl.get("City"), jl.get("State"), jl.get("Country")) or (j.get("LocationName") or "")
        url = f"https://recruiting.paylocity.com/Recruiting/Jobs/Details/{jid}"
        jobs.append(_job(b, job_id=jid, title=(j.get("JobTitle") or "").strip(), url=url, location=loc,
                         posted_at=parse_when(j.get("PublishedDate")),
                         workplace="remote" if j.get("IsRemote") else "",
                         countries=[jl["Country"]] if jl.get("Country") else [],
                         needs_detail=True, detail_ref=url))
    return jobs


def paylocity_detail(job: Job) -> None:
    page = _page(job.detail_ref)
    if not _apply_ld(job, page):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(page, "html.parser")
        d = soup.select_one(".job-preview-details, .job-listing-details") or soup.body
        job.description = d.get_text("\n", strip=True)[:20000] if d else ""


# ------------------------------------------------------------------ Taleo (Enterprise)

def taleo_list(b: Board, max_pages: int = 3) -> list[Job]:
    tenant, section = b.board.split("|", 1)
    host = f"https://{tenant}.taleo.net"
    page = _page(f"{host}/careersection/{section}/jobsearch.ftl?lang=en")
    m = re.search(r"portal=(\d+)", page) or re.search(r"portalNo['\"]?\s*[:=]\s*['\"]?(\d+)", page)
    if not m:
        raise FetchError("Taleo portal number not found", 422)
    url = f"{host}/careersection/rest/jobboard/searchjobs?lang=en&portal={m.group(1)}"
    robots.check(url)
    jobs, seen = [], set()
    for page_no in range(1, max_pages + 1):
        body = {"multilineEnabled": False, "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
                "fieldData": {"fields": {"KEYWORD": "", "LOCATION": ""}, "valid": True},
                "filterSelectionParam": {"searchFilterSelections": [
                    {"id": "POSTING_DATE", "selectedValues": []}, {"id": "LOCATION", "selectedValues": []},
                    {"id": "JOB_FIELD", "selectedValues": []}]},
                "advancedSearchFiltersSelectionParam": {"searchFilterSelections": []}, "pageNo": page_no}
        data = post_json(url, body, headers={"tz": "GMT-06:00", "tzname": "America/Chicago",
                                             "Referer": f"{host}/careersection/{section}/jobsearch.ftl"})
        rows = data.get("requisitionList") or []
        for r in rows:
            cols = r.get("column") or []
            jid = str(r.get("contestNo") or r.get("jobId") or "")
            if not jid or jid in seen or not cols:
                continue
            seen.add(jid)
            texts = [c for c in cols if isinstance(c, str)]
            title_i = 1 if len(texts) > 1 and (texts[0] == jid or texts[0].isdigit()) else 0
            title = texts[title_i] if texts else ""
            locs = next((c for c in texts[title_i + 1:] if c.startswith("[")), "")
            if locs:
                try:
                    import json as _j
                    locs = "; ".join(_j.loads(locs))
                except ValueError:
                    pass
            else:
                locs = next((c for c in texts[title_i + 1:] if c and not parse_when(c) and not c.isdigit()), "")
            posted = next((parse_when(c) for c in texts[title_i + 1:] if parse_when(c)), None)
            jurl = f"{host}/careersection/{section}/jobdetail.ftl?job={jid}&lang=en"
            jobs.append(_job(b, job_id=jid, title=htmllib.unescape(title), url=jurl, location=str(locs),
                             posted_at=posted, needs_detail=True, detail_ref=jurl))
        total = (data.get("pagingData") or {}).get("totalCount") or 0
        if not rows or len(seen) >= total:
            break
    return jobs


def taleo_detail(job: Job) -> None:
    page = _page(job.detail_ref)
    m = re.search(r"name=\"initialHistory\"[^>]*value=\"([^\"]*)\"", page)
    text = ""
    if m:
        from urllib.parse import unquote
        raw = unquote(htmllib.unescape(m.group(1)))
        text = html_to_text(raw.replace("!*!", "\n"))
    job.description = text or html_to_text(page)[:20000]


# ------------------------------------------------------------------ Avature

def avature_list(b: Board) -> list[Job]:
    host, _, portal = b.board.partition("|")
    portal = portal or "careers"
    urls = _sitemap_urls(f"https://{host}/{portal}/sitemap_index.xml", lambda u: "/JobDetail/" in u)
    jobs, seen = [], set()
    for u, lastmod in urls:
        m = re.search(r"/JobDetail/([^/?#]*)/(\d+)", u)
        if not m or m.group(2) in seen:
            continue
        seen.add(m.group(2))
        jobs.append(_job(b, job_id=m.group(2), title=slug_words(m.group(1)), url=u, title_guess=True,
                         posted_at=parse_when(lastmod), needs_detail=True, detail_ref=u))
    return jobs[:MAX_JOBS]


def avature_detail(job: Job) -> None:
    page = _page(job.detail_ref)
    if _apply_ld(job, page):
        return
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page, "html.parser")
    t = soup.select_one("h2.banner__text__title, h1, .article__header__text__title")
    if t:
        job.title = t.get_text(" ", strip=True)
    fields = {}
    for f in soup.select(".article__content__view__field"):
        lab = f.select_one(".article__content__view__field__label")
        val = f.select_one(".article__content__view__field__value")
        if lab and val:
            fields[lab.get_text(" ", strip=True).lower()] = val.get_text(" ", strip=True)
    for k, v in fields.items():
        if "location" in k and not job.location:
            job.location = v
        elif ("posted" in k or "date" in k) and parse_when(v):
            job.posted_at = parse_when(v)
        elif "work" in k and _wp(v):
            job.workplace = _wp(v)
    d = soup.select_one(".article__content") or soup.body
    job.description = d.get_text("\n", strip=True)[:20000] if d else ""


# ------------------------------------------------------------------ Phenom

def _phenom_body(**kw) -> dict:
    return {"lang": "en_us", "deviceType": "desktop", "country": "us", "pageName": "search-results",
            "siteType": "external", "global": True, "selected_fields": {}, "locationData": {}, **kw}


def phenom_list(b: Board, size: int = 100, max_pages: int = 4, search: str = "") -> list[Job]:
    host, _, locale = b.board.partition("|")
    locale = locale or "us/en"
    url = f"https://{host}/widgets"
    robots.check(url)
    jobs, seen = [], set()
    for page in range(max_pages):
        data = post_json(url, _phenom_body(ddoKey="refineSearch", sortBy="" if search else "Most recent", subsearch="",
                                           jobs=True, counts=False, all_fields=[], size=size, clearAll=False,
                                           jdsource="facets", isSliderEnable=False, keywords=search,
                                           **{"from": page * size}))
        rs = (data.get("refineSearch") or {})
        rows = ((rs.get("data") or {}).get("jobs")) or []
        for j in rows:
            seq = str(j.get("jobSeqNo") or j.get("jobId") or "")
            if not seq or seq in seen:
                continue
            seen.add(seq)
            loc = j.get("location") or _loc(j.get("city"), j.get("state"), j.get("country")) or j.get("cityStateCountry") or ""
            jobs.append(_job(b, job_id=seq, title=(j.get("title") or "").strip(),
                             url=f"https://{host}/{locale}/job/{quote(seq)}", location=loc,
                             posted_at=parse_when(j.get("postedDate") or j.get("dateCreated")),
                             countries=[j["country"]] if j.get("country") else [],
                             workplace=_wp(j.get("workplaceType") or j.get("remote")),
                             description=html_to_text(j.get("descriptionTeaser") or ""),
                             needs_detail=True, detail_ref=seq, found_by=search))
        total = rs.get("totalHits") or 0
        if not rows or len(seen) >= total:
            break
    return jobs


def phenom_detail(job: Job) -> None:
    host = job.board.partition("|")[0]
    data = post_json(f"https://{host}/widgets", _phenom_body(ddoKey="jobDetail", jobSeqNo=job.detail_ref,
                                                             pageName="job-details"))
    j = ((data.get("jobDetail") or {}).get("data") or {}).get("job") or {}
    if j.get("description"):
        job.description = html_to_text(j["description"])
    if j.get("postedDate") and not job.posted_at:
        job.posted_at = parse_when(j["postedDate"])


# ------------------------------------------------------------------ Eightfold

def eightfold_list(b: Board, num: int = 100, search: str = "") -> list[Job]:
    sub, _, domain = b.board.partition("|")
    host = sub if "." in sub else f"{sub}.eightfold.ai"
    domain = domain or (sub.split(".")[0] + ".com")
    jobs = []
    try:
        url = f"https://{host}/api/pcsx/search"
        robots.check(url)
        seen = set()
        for start in range(0, num, 10):
            data = get_json(url, params={"domain": domain, "query": search, "location": "", "start": start,
                                         "sort_by": "relevance" if search else "timestamp"})
            rows = (data.get("data") or {}).get("positions") or []
            for p in rows:
                pid = str(p.get("id") or "")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                locs = p.get("standardizedLocations") or p.get("locations") or []
                jobs.append(_job(b, job_id=pid, title=(p.get("name") or "").strip(),
                                 url=urljoin(f"https://{host}", p.get("positionUrl") or f"/careers/job/{pid}?domain={domain}"),
                                 location="; ".join(map(str, locs[:4])),
                                 posted_at=parse_when(p.get("postedTs") or p.get("creationTs")),
                                 workplace=_wp(p.get("workLocationOption")), found_by=search,
                                 needs_detail=True, detail_ref=f"https://{host}/api/apply/v2/jobs/{pid}?domain={domain}"))
            if len(rows) < 10 or len(seen) >= int((data.get("data") or {}).get("count") or 10 ** 6):
                break
        return jobs
    except FetchError as e:
        if e.status not in (404, 400):
            raise
    url = f"https://{host}/api/apply/v2/jobs"
    robots.check(url)
    data = get_json(url, params={"domain": domain, "start": 0, "num": num,
                                 **({"query": search, "sort_by": "relevance"} if search else {"sort_by": "timestamp"})})
    for p in data.get("positions") or []:
        pid = str(p.get("id") or "")
        if not pid:
            continue
        jobs.append(_job(b, job_id=pid, title=(p.get("name") or "").strip(),
                         url=p.get("canonicalPositionUrl") or f"https://{host}/careers/job/{pid}?domain={domain}",
                         location="; ".join(map(str, (p.get("locations") or [p.get("location")])[:4])),
                         posted_at=parse_when(p.get("t_create")),
                         workplace=_wp(p.get("work_location_option")), found_by=search,
                         needs_detail=True, detail_ref=f"https://{host}/api/apply/v2/jobs/{pid}?domain={domain}"))
    return jobs


def eightfold_detail(job: Job) -> None:
    robots.check(job.detail_ref)
    d = get_json(job.detail_ref)
    job.description = html_to_text(d.get("job_description") or "")
    if d.get("t_create") and not job.posted_at:
        job.posted_at = parse_when(d["t_create"])


# ------------------------------------------------------------------ Jibe (iCIMS Attract)

def jibe_list(b: Board, limit: int = 100, max_pages: int = 3, search: str = "") -> list[Job]:
    """`search` runs the site's own keyword search (title and description) instead of listing by date."""
    host = b.board
    url = f"https://{host}/api/jobs"
    robots.check(url)
    jobs, seen = [], set()
    for page in range(1, max_pages + 1):
        params = {"page": page, "limit": limit, "internal": "false"}
        params.update({"keywords": search} if search else {"sortBy": "posted_date", "descending": "true"})
        data = get_json(url, params=params)
        rows = data.get("jobs") or []
        for row in rows:
            j = row.get("data") or row
            slug = str(j.get("slug") or j.get("req_id") or "")
            if not slug or slug in seen:
                continue
            seen.add(slug)
            jobs.append(_job(b, job_id=slug, title=(j.get("title") or "").strip(),
                             url=f"https://{host}/jobs/{slug}?lang=en-us",
                             location=j.get("full_location") or j.get("location_name") or _loc(j.get("city"), j.get("state"), j.get("country")),
                             countries=[j["country_code"]] if j.get("country_code") else ([j["country"]] if j.get("country") else []),
                             posted_at=parse_when(j.get("posted_date") or j.get("create_date")),
                             workplace=_wp(j.get("location_type") or ""),
                             found_by=search,
                             description=html_to_text(j.get("description") or "") +
                                         "\n" + html_to_text(j.get("qualifications") or "")))
        if not rows or len(seen) >= int(data.get("totalCount") or 0):
            break
    return jobs


# ------------------------------------------------------------------ Breezy

def breezy_list(b: Board) -> list[Job]:
    url = f"https://{b.board}.breezy.hr/json"
    robots.check(url)
    data = get_json(url)
    jobs = []
    for j in data if isinstance(data, list) else []:
        jid = str(j.get("id") or j.get("friendly_id") or "")
        if not jid:
            continue
        loc = j.get("location") or {}
        country = (loc.get("country") or {})
        state = loc.get("state") or {}
        cname = country.get("name") if isinstance(country, dict) else country
        jobs.append(_job(b, job_id=jid, title=(j.get("name") or "").strip(),
                         url=j.get("url") or f"https://{b.board}.breezy.hr/p/{j.get('friendly_id')}",
                         location=loc.get("name") or _loc(loc.get("city"), state.get("name") if isinstance(state, dict) else state, cname),
                         countries=[country.get("id") or cname] if isinstance(country, dict) and (country.get("id") or cname) else [],
                         workplace="remote" if loc.get("is_remote") else _wp(j.get("location_type")),
                         posted_at=parse_when(j.get("published_date") or j.get("updated_date")),
                         needs_detail=True, detail_ref=j.get("url") or ""))
    return jobs


# ------------------------------------------------------------------ Pinpoint

def pinpoint_list(b: Board) -> list[Job]:
    url = f"https://{b.board}.pinpointhq.com/postings.json"
    robots.check(url)
    data = get_json(url)
    jobs = []
    for j in data.get("data") or []:
        jid = str(j.get("id") or "")
        if not jid:
            continue
        loc = j.get("location") or {}
        desc = " ".join(str(j.get(k) or "") for k in ("description", "key_responsibilities",
                                                         "skills_knowledge_expertise", "benefits"))
        jobs.append(_job(b, job_id=jid, title=(j.get("title") or "").strip(),
                         url=j.get("url") or f"https://{b.board}.pinpointhq.com/en/postings/{jid}",
                         location=loc.get("name") or _loc(loc.get("city"), loc.get("province"), loc.get("country")),
                         workplace=_wp(j.get("workplace_type")), description=html_to_text(desc),
                         posted_at=parse_when(j.get("published_at") or j.get("created_at"))))
    return jobs


# ------------------------------------------------------------------ Teamtailor

def teamtailor_list(b: Board) -> list[Job]:
    host = b.board if "." in b.board else f"{b.board}.teamtailor.com"
    xml = _page(f"https://{host}/jobs.rss", accept="application/rss+xml,application/xml,*/*;q=0.5")
    jobs = []
    for it in rss_items(xml, extra=("tt:locations", "remoteStatus", "tt:remoteStatus")):
        link = it["link"]
        m = re.search(r"/jobs/(\d+)", link)
        if not m:
            continue
        locs = it.get("tt:locations") or ""
        cities = re.findall(r"<tt:city>(.*?)</tt:city>", locs, re.S)
        countries = re.findall(r"<tt:country>(.*?)</tt:country>", locs, re.S)
        loc = "; ".join(_loc(c, k) for c, k in zip(cities, countries)) if cities else ", ".join(countries)
        jobs.append(_job(b, job_id=m.group(1), title=htmllib.unescape(it["title"]), url=link, location=loc,
                         countries=countries, posted_at=parse_when(it["pubDate"]),
                         workplace=_wp(it.get("remoteStatus") or it.get("tt:remoteStatus")),
                         description=html_to_text(it["description"])))
    return jobs


# ------------------------------------------------------------------ Dover

_dover_ids: dict[str, str] = {}


def dover_list(b: Board) -> list[Job]:
    cid = _dover_ids.get(b.board)
    if not cid:
        cid = str(get_json(f"https://app.dover.com/api/v1/careers-page-slug/{b.board}").get("id") or "")
        if not cid:
            raise FetchError("Dover company not found", 404)
        _dover_ids[b.board] = cid
    data = get_json(f"https://app.dover.com/api/v1/careers-page/{cid}/jobs", params={"limit": 300, "offset": 0})
    jobs = []
    for j in data.get("results") or []:
        if j.get("is_sample") or j.get("is_published") is False:
            continue
        jid = str(j.get("id") or "")
        locs = []
        for l in j.get("locations") or []:
            o = l.get("location_option") or l
            locs.append(o.get("display_name") or _loc(o.get("city"), o.get("state"), o.get("country")))
        jobs.append(_job(b, job_id=jid, title=(j.get("title") or "").strip(),
                         url=f"https://app.dover.com/apply/{b.board}/{jid}", location="; ".join(x for x in locs if x),
                         workplace=_wp(j.get("workplace_type") or ""), needs_detail=True,
                         detail_ref=f"https://app.dover.com/api/v1/inbound/application-portal-job/{jid}"))
    return jobs


def dover_detail(job: Job) -> None:
    d = get_json(job.detail_ref)
    job.description = html_to_text(d.get("user_provided_description") or d.get("description") or "")
    if d.get("visa_support") is True:
        job.description += "\nVisa sponsorship available."


# ------------------------------------------------------------------ Gem

_GEM_LIST = """query JobBoardList($boardId: String!) { oatsExternalJobPostings(boardId: $boardId) {
  jobPostings { id extId title locations { name city isoCountry isRemote } job { locationType employmentType } } } }"""
_GEM_ONE = """query JobPosting($boardId: String!, $extId: String!) { oatsExternalJobPosting(boardId: $boardId, extId: $extId) {
  descriptionHtml firstPublishedTsSec } }"""


def gem_list(b: Board) -> list[Job]:
    data = post_json("https://jobs.gem.com/api/public/graphql",
                     {"operationName": "JobBoardList", "query": _GEM_LIST, "variables": {"boardId": b.board}})
    rows = (((data.get("data") or {}).get("oatsExternalJobPostings")) or {}).get("jobPostings") or []
    jobs = []
    for j in rows:
        ext = str(j.get("extId") or "")
        if not ext:
            continue
        locs = j.get("locations") or []
        jobs.append(_job(b, job_id=ext, title=(j.get("title") or "").strip(), url=f"https://jobs.gem.com/{b.board}/{ext}",
                         location="; ".join(l.get("name") or _loc(l.get("city"), l.get("isoCountry")) for l in locs),
                         countries=[l["isoCountry"] for l in locs if l.get("isoCountry")],
                         workplace="remote" if any(l.get("isRemote") for l in locs) else _wp((j.get("job") or {}).get("locationType")),
                         needs_detail=True, detail_ref=ext))
    return jobs


def gem_detail(job: Job) -> None:
    data = post_json("https://jobs.gem.com/api/public/graphql",
                     {"operationName": "JobPosting", "query": _GEM_ONE,
                      "variables": {"boardId": job.board, "extId": job.detail_ref}})
    d = (data.get("data") or {}).get("oatsExternalJobPosting") or {}
    job.description = html_to_text(d.get("descriptionHtml") or "")
    if d.get("firstPublishedTsSec"):
        job.posted_at = parse_when(d["firstPublishedTsSec"])


# ------------------------------------------------------------------ UKG Pro (UltiPro)

def ukg_list(b: Board, top: int = 100, max_pages: int = 4) -> list[Job]:
    host, tenant, guid = b.board.split("|")
    base = f"https://{host}/{tenant}/JobBoard/{guid}"
    url = f"{base}/JobBoardView/LoadSearchResults"
    robots.check(url)
    jobs, seen = [], set()
    for page in range(max_pages):
        data = post_json(url, {"opportunitySearch": {"Top": top, "Skip": page * top, "QueryString": "",
                                                     "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate",
                                                                  "Ascending": False}], "Filters": []},
                               "matchCriteria": {"PreferredJobs": [], "Educations": [], "LicenseAndCertifications": [],
                                                 "Skills": [], "hasNoLicenses": False, "SkippedSkills": []}})
        rows = data.get("opportunities") or []
        for o in rows:
            oid = str(o.get("Id") or "")
            if not oid or oid in seen:
                continue
            seen.add(oid)
            locs, ctry = [], []
            for l in o.get("Locations") or []:
                a = l.get("Address") or {}
                st = (a.get("State") or {}).get("Name") if isinstance(a.get("State"), dict) else a.get("State")
                co = (a.get("Country") or {}).get("Name") if isinstance(a.get("Country"), dict) else a.get("Country")
                locs.append(l.get("LocalizedName") or _loc(a.get("City"), st, co))
                if co:
                    ctry.append(co)
            jobs.append(_job(b, job_id=oid, title=(o.get("Title") or "").strip(),
                             url=f"{base}/OpportunityDetail?opportunityId={oid}", location="; ".join(x for x in locs if x),
                             countries=ctry, posted_at=parse_when(o.get("PostedDate")),
                             workplace=_wp(o.get("JobLocationType")),
                             description=html_to_text(o.get("BriefDescription") or ""),
                             needs_detail=True, detail_ref=f"{base}/OpportunityDetail?opportunityId={oid}"))
        if not rows or len(seen) >= int(data.get("totalCount") or 0):
            break
    return jobs


def ukg_detail(job: Job) -> None:
    page = _page(job.detail_ref)
    m = re.search(r'"Description"\s*:\s*"((?:[^"\\]|\\.)*)"', page)
    if m:
        import json as _j
        try:
            job.description = html_to_text(_j.loads(f'"{m.group(1)}"'))
        except ValueError:
            pass


# ------------------------------------------------------------------ UKG Ready (Kronos)

def ukgready_list(b: Board) -> list[Job]:
    host, cid = b.board.split("|")
    url = f"https://{host}/ta/rest/ui/recruitment/companies/%7C{cid}/job-requisitions"
    robots.check(url)
    data = get_json(url, params={"offset": 0, "size": 100, "sort": "desc", "ein_id": "", "lang": "en-US"})
    jobs = []
    for j in data.get("job_requisitions") or []:
        jid = str(j.get("id") or "")
        if not jid:
            continue
        l = j.get("location") or {}
        jobs.append(_job(b, job_id=jid, title=(j.get("job_title") or "").strip(),
                         url=f"https://{host}/ta/{cid}.careers?ShowJob={jid}",
                         location=_loc(l.get("city"), l.get("state"), l.get("country")),
                         countries=[l["country"]] if l.get("country") else [],
                         workplace="remote" if j.get("is_remote_job") else "",
                         description=html_to_text(j.get("job_description") or ""),
                         posted_at=parse_when(j.get("published_date") or j.get("created_date"))))
    return jobs


# ------------------------------------------------------------------ Dayforce

def dayforce_list(b: Board, max_pages: int = 4) -> list[Job]:
    tenant, site = b.board.split("|")
    tok = get_json("https://jobs.dayforcehcm.com/api/auth/csrf")
    csrf = tok.get("csrfToken") or tok.get("token") or ""
    jobs, seen = [], set()
    start = 0
    for _ in range(max_pages):
        data = post_json(f"https://jobs.dayforcehcm.com/api/geo/{tenant}/jobposting/search",
                         {"clientNamespace": tenant, "jobBoardCode": site, "cultureCode": "en-US",
                          "distanceUnit": 0, "paginationStart": start},
                         headers={"X-CSRF-TOKEN": csrf, "Referer": f"https://jobs.dayforcehcm.com/en-US/{tenant}/{site}"})
        rows = data.get("jobPostings") or []
        for p in rows:
            pid = str(p.get("jobPostingId") or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            locs = p.get("postingLocations") or []
            jobs.append(_job(b, job_id=pid, title=(p.get("jobTitle") or "").strip(),
                             url=f"https://jobs.dayforcehcm.com/en-US/{tenant}/{site}/jobs/{pid}",
                             location="; ".join(_loc(l.get("cityName"), l.get("stateCode"), l.get("isoCountryCode")) for l in locs),
                             countries=[l["isoCountryCode"] for l in locs if l.get("isoCountryCode")],
                             workplace="remote" if p.get("hasVirtualLocation") else "",
                             posted_at=parse_when(p.get("postingStartTimestampUTC")),
                             description=html_to_text(p.get("jobDescription") or "")))
        start += len(rows)
        if not rows or start >= int(data.get("maxCount") or 0):
            break
    return jobs


# ------------------------------------------------------------------ ADP Workforce Now

_ADP = "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"


def adp_list(b: Board, top: int = 100, max_pages: int = 4) -> list[Job]:
    cid, _, ccid = b.board.partition("|")
    ccid = ccid or "19000101_000001"
    jobs, seen = [], set()
    for page in range(max_pages):
        data = get_json(_ADP, params={"cid": cid, "ccId": ccid, "lang": "en_US", "locale": "en_US",
                                      "$top": top, "$skip": page * top})
        rows = data.get("jobRequisitions") or []
        for r in rows:
            rid = str(r.get("itemID") or "")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            locs = []
            for l in r.get("requisitionLocations") or []:
                a = l.get("address") or {}
                locs.append(_loc(a.get("cityName"), (a.get("countrySubdivisionLevel1") or {}).get("codeValue"),
                                 (a.get("country") or {}).get("codeValue") if isinstance(a.get("country"), dict) else a.get("countryCode"))
                            or (l.get("nameCode") or {}).get("shortName", ""))
            jobs.append(_job(b, job_id=rid, title=(r.get("requisitionTitle") or "").strip(),
                             url=f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={cid}&ccId={ccid}&jobId={rid}&lang=en_US",
                             location="; ".join(x for x in locs if x), posted_at=parse_when(r.get("postDate")),
                             needs_detail=True, detail_ref=rid))
        if not rows or len(seen) >= int((data.get("meta") or {}).get("totalNumber") or 0):
            break
    return jobs


def adp_detail(job: Job) -> None:
    cid, _, ccid = job.board.partition("|")
    d = get_json(f"{_ADP}/{job.detail_ref}", params={"cid": cid, "ccId": ccid or "19000101_000001",
                                                     "lang": "en_US", "locale": "en_US"})
    job.description = html_to_text(d.get("requisitionDescription") or "")


# ------------------------------------------------------------------ Manatal / Hireology / Oorwin

def manatal_list(b: Board) -> list[Job]:
    jobs, url = [], f"https://open.api.manatal.com/open/v3/career-page/{b.board}/jobs/"
    for _ in range(5):
        data = get_json(url)
        for j in data.get("results") or []:
            jid = str(j.get("hash") or j.get("id") or "")
            if not jid:
                continue
            jobs.append(_job(b, job_id=jid, title=(j.get("position_name") or "").strip(),
                             url=f"https://www.careers-page.com/{b.board}/job/{jid}",
                             location=j.get("location_display") or _loc(j.get("city"), j.get("state"), j.get("country")),
                             countries=[j["country"]] if j.get("country") else [],
                             workplace="remote" if j.get("is_remote") else "",
                             description=html_to_text(j.get("description") or ""),
                             posted_at=parse_when(j.get("created_at") or j.get("updated_at"))))
        url = data.get("next")
        if not url:
            break
    return jobs


def hireology_list(b: Board) -> list[Job]:
    data = get_json(f"https://api.hireology.com/v1/careers/{b.board}")
    jobs = []
    for j in data.get("data") or []:
        a = j.get("attributes") or j
        jid = str(j.get("id") or "")
        if not jid:
            continue
        locs = a.get("locations") or []
        loc = "; ".join(_loc(l.get("city"), l.get("state")) for l in locs if isinstance(l, dict)) or a.get("location") or ""
        jobs.append(_job(b, job_id=jid, title=(a.get("name") or "").strip(),
                         url=a.get("career-site-url") or f"https://careers.hireology.com/{b.board}/{jid}/description",
                         location=loc, countries=["US"], workplace="remote" if a.get("remote") else "",
                         description=html_to_text(a.get("job-description") or ""),
                         posted_at=parse_when(a.get("created-at") or a.get("published-at"))))
    return jobs


def oorwin_list(b: Board) -> list[Job]:
    jobs, seen = [], set()
    for page in range(1, 6):
        data = post_json("https://api.oorwin.ai/api/v2/careers/getJobList",
                         {"sub_domain": b.board, "limit": 50, "page": page, "search": "", "order": "desc"})
        block = ((data.get("data") or {}).get("list_details") or {})
        rows = block.get("data") or []
        for j in rows:
            jid = str(j.get("computed_sha1_job_id") or j.get("job_id") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            jobs.append(_job(b, job_id=jid, title=(j.get("title") or j.get("job_title") or "").strip(),
                             url=f"https://{b.board}.oorwin.com/careers/index.html?job_id={jid}",
                             location=_loc(j.get("city"), j.get("state_format_name"), j.get("country_format_name")),
                             countries=[j["country_format_name"]] if j.get("country_format_name") else [],
                             workplace=_wp(j.get("remote_status")),
                             posted_at=parse_when(j.get("cp_published_on") or j.get("created_at"))))
        if not rows or page >= int(block.get("last_page") or 1):
            break
    return jobs


# ------------------------------------------------------------------ Symphony Talent "m-cloud" career sites

def mcloud_list(b: Board, limit: int = 100, max_pages: int = 8, search: str = "") -> list[Job]:
    """Career sites built on Symphony Talent (e.g. careers.stellantis.com). The listing carries the full
    description. Only US jobs are asked for; `search` is the site's keyword search."""
    org, _, site = b.board.partition("|")
    url = "https://jobsapi-internal.m-cloud.io/api/job"
    jobs, seen = [], set()
    for page in range(max_pages):
        params = {"Organization": org, "Limit": limit, "Offset": page * limit + 1, "facet": "primary_country:US"}
        if search:
            params["SearchText"] = search
        data = get_json(url, params=params)
        rows = data.get("queryResult") or []
        for r in rows:
            jid = str(r.get("id") or "")
            if not jid or jid in seen or str(r.get("entity_status") or "Open").lower() != "open":
                continue
            seen.add(jid)
            link = r.get("url") or ""
            if site and site not in link:
                link = f"https://{site}/job/{jid}/"
            jobs.append(_job(b, job_id=jid, title=(r.get("title") or "").strip(), url=link,
                             location=_loc(r.get("primary_city"), r.get("primary_state"), r.get("primary_country")),
                             countries=[r["primary_country"]] if r.get("primary_country") else [],
                             posted_at=parse_when(r.get("open_date")), found_by=search,
                             description=html_to_text(r.get("description") or "")))
        if len(rows) < limit or page * limit + len(rows) >= int(data.get("totalHits") or 0):
            break
    return jobs


# ------------------------------------------------------------------ dSPACE (own job finder)

def dspace_list(b: Board) -> list[Job]:
    host, _, path = b.board.partition("|")
    page = _page(f"https://{host}/{path}")
    jobs, seen = [], set()
    for m in re.finditer(r'"href":"([^"]*?jid=(\d+)[^"]*)"(.{0,400}?)"title":"((?:[^"\\]|\\.)*)"', page, re.S):
        href, jid, mid, title = m.groups()
        if jid in seen:
            continue
        seen.add(jid)
        loc = re.search(r'"location":"((?:[^"\\]|\\.)*)"', mid)
        link = urljoin(f"https://{host}/", href.replace("\\/", "/"))
        location = htmllib.unescape(loc.group(1)) if loc else ""
        jobs.append(_job(b, job_id=jid, title=htmllib.unescape(title).replace("\\/", "/").strip(), url=link,
                         location=location, countries=["US"] if re.search(r",\s*[A-Z]{2}$", location) else [],
                         needs_detail=True, detail_ref=link))
    return jobs


def dspace_detail(job: Job) -> None:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(_page(job.detail_ref), "html.parser")
    blocks = soup.select("div.ds-textblock") or [soup.body or soup]
    job.description = "\n".join(x.get_text("\n", strip=True) for x in blocks)[:20000]


LISTERS = {
    "ldsite": ldsite_list,
    "icims": icims_list, "jazzhr": jazzhr_list, "jobvite": jobvite_list, "sfrmk": sfrmk_list,
    "paylocity": paylocity_list, "taleo": taleo_list, "avature": avature_list, "phenom": phenom_list,
    "eightfold": eightfold_list, "jibe": jibe_list, "breezy": breezy_list, "pinpoint": pinpoint_list,
    "teamtailor": teamtailor_list, "dover": dover_list, "gem": gem_list, "ukg": ukg_list, "ukgready": ukgready_list,
    "dayforce": dayforce_list, "adp": adp_list, "manatal": manatal_list,
    "hireology": hireology_list, "oorwin": oorwin_list, "mcloud": mcloud_list, "dspace": dspace_list,
}
DETAILERS = {
    "ldsite": _ld_detail,
    "icims": icims_detail, "jazzhr": _ld_detail, "jobvite": jobvite_detail, "sfrmk": sfrmk_detail,
    "paylocity": paylocity_detail, "taleo": taleo_detail, "avature": avature_detail, "phenom": phenom_detail,
    "eightfold": eightfold_detail, "breezy": _ld_detail, "dover": dover_detail, "gem": gem_detail,
    "ukg": ukg_detail, "adp": adp_detail, "dspace": dspace_detail,
}
# Listers whose detail call only fills gaps can skip it when the listing already had a description.
CONCURRENCY = {"icims": 6, "jazzhr": 6, "jobvite": 3, "sfrmk": 4, "paylocity": 4, "taleo": 3, "avature": 3,
               "phenom": 3, "eightfold": 3, "jibe": 4, "breezy": 6, "pinpoint": 4, "teamtailor": 4, "dover": 3,
               "gem": 3, "ukg": 4, "ukgready": 3, "dayforce": 2, "adp": 3, "manatal": 3, "hireology": 3, "oorwin": 2,
               "mcloud": 2, "dspace": 2, "ldsite": 3}
