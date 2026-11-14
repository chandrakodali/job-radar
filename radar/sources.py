"""Jobs from outside the employers' own job boards, all through allowed routes.

1. Job-alert emails (LinkedIn, Indeed, Dice, ZipRecruiter, Glassdoor, Monster, ...). You set up job alerts
   on those sites, sent to a mailbox used only for that; each run reads the new alert emails over IMAP
   and lists the jobs in them. Nothing is scraped from those sites. Turned on by two repository secrets:
     JOB_ALERTS_EMAIL          the mailbox address (e.g. a Gmail made for job alerts)
     JOB_ALERTS_APP_PASSWORD   its app password (Gmail: 2-Step Verification on, then an app password)
   Optional: JOB_ALERTS_IMAP_HOST (default imap.gmail.com).

2. Adzuna, a job search engine with a free official API. Turned on by ADZUNA_APP_ID and ADZUNA_APP_KEY.
   The free key allows 25 calls a minute, 250 a day, 1,000 a week and 2,500 a month; the radar stays well
   under those and counts its calls. Listings must be credited "Jobs by Adzuna".
"""
from __future__ import annotations

import email
import email.utils
import hashlib
import html as htmllib
import imaplib
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from .http import FetchError, get_json, get_text
from .models import Board, Job

log = logging.getLogger("radar.sources")

# ---------------------------------------------------------------- job-alert emails

SENDERS = re.compile(r"linkedin\.com|indeed\.com|dice\.com|ziprecruiter\.com|glassdoor\.com|monster\.com|"
                     r"simplyhired\.com|careerbuilder\.com|builtin\.com|wellfound\.com|jobright|handshake", re.I)
# job links in alert emails: (source, pattern with the job id as group 1 when there is one)
LINKS = [
    ("linkedin", re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", re.I)),
    ("indeed", re.compile(r"indeed\.com/(?:rc/clk|viewjob|pagead/clk|m/viewjob)[^\"'\s>]*?[?&](?:jk|ad)=([\w-]+)", re.I)),
    ("dice", re.compile(r"dice\.com/job-detail/([\w-]+)", re.I)),
    ("ziprecruiter", re.compile(r"ziprecruiter\.com/(?:c/[^\"'\s>]*?/job/|jobs/|k/l/|ekm/)([\w-]{6,})", re.I)),
    ("glassdoor", re.compile(r"glassdoor\.com/(?:partner/jobListing\.htm|job-listing/)[^\"'\s>]*?(?:jobListingId=|jl=|-JV_)(\d+)", re.I)),
    ("monster", re.compile(r"monster\.com/job-openings/[^\"'\s>]*?/([\w-]{8,})", re.I)),
]
_A = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")
_NOISE = re.compile(r"^(view job|apply( now)?|easy apply|see (all|more).*|unsubscribe.*|new|actively recruiting|promoted|"
                     r"\d+ (applicants?|connections?)|be an early applicant|.*ago|viewed|save|top applicant)$", re.I)


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(_TAGS.sub(" ", s or ""))).strip()


def parse_alert(html: str, sent: datetime | None = None) -> list[Job]:
    """Jobs listed in one alert email: title from the link text, then company and location from the next
    short lines of text."""
    if not html:
        return []
    # split the email at every job link, keeping the link text as the title
    marks = []
    for m in _A.finditer(html):
        href = htmllib.unescape(m.group(1))
        for source, rx in LINKS:
            lm = rx.search(href)
            if lm:
                marks.append((m.start(), m.end(), source, lm.group(1), href, _text(m.group(2))))
                break
    jobs: dict[str, Job] = {}
    for i, (start, end, source, jid, href, title) in enumerate(marks):
        nxt = marks[i + 1][0] if i + 1 < len(marks) else min(len(html), end + 3000)
        after = re.sub(r"(?i)<\s*(br|/p|/div|/td|/tr|/li|/h\d|/span)\s*/?>", "\n", html[end:nxt])
        lines = [ln for ln in (_text(x) for x in after.split("\n")) if ln and not _NOISE.match(ln) and len(ln) <= 90]
        key = f"{source}:{jid}"
        have = jobs.get(key)
        if have and have.title:        # the same job is linked twice (logo and title): keep the titled one
            continue
        if not title or len(title) < 4 or _NOISE.match(title):
            if have is None:
                jobs[key] = Job(ats="email", board=source, job_id=jid, company="", title="", url=_clean_url(href))
            continue
        company = lines[0] if lines else ""
        location = lines[1] if len(lines) > 1 else ""
        if "·" in company:                       # "Company · City, ST" on one line
            company, _, location = (x.strip() for x in company.partition("·"))
        jobs[key] = Job(ats="email", board=source, job_id=jid, company=company[:80], title=title[:150],
                        location=location[:80], url=_clean_url(href), posted_at=sent)
    return [j for j in jobs.values() if j.title]


def _clean_url(href: str) -> str:
    m = re.search(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", href)
    if m:
        return f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
    return href.split("&trk")[0]


def read_alert_emails(state: dict, days: int = 4, max_messages: int = 200, env=os.environ) -> list[Job]:
    user, pw = (env.get("JOB_ALERTS_EMAIL") or "").strip(), (env.get("JOB_ALERTS_APP_PASSWORD") or "").replace(" ", "")
    if not user or not pw:
        return []
    host = (env.get("JOB_ALERTS_IMAP_HOST") or "imap.gmail.com").strip()
    seen = state.setdefault("email_seen", {})
    out: list[Job] = []
    try:
        box = imaplib.IMAP4_SSL(host, 993, timeout=60)
        box.login(user, pw)
        box.select("INBOX", readonly=True)
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = box.uid("search", None, f"(SINCE {since})")
        uids = (data[0] or b"").split()[-max_messages:] if typ == "OK" else []
        for uid in uids:
            u = uid.decode()
            if u in seen:
                continue
            typ, msg_data = box.uid("fetch", uid, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            seen[u] = datetime.now(timezone.utc).date().isoformat()
            if not SENDERS.search(msg.get("From", "")):
                continue
            sent = None
            try:
                sent = email.utils.parsedate_to_datetime(msg.get("Date"))
            except (TypeError, ValueError):
                pass
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    body = part.get_payload(decode=True) or b""
                    out.extend(parse_alert(body.decode(part.get_content_charset() or "utf-8", "replace"), sent))
        box.logout()
    except (imaplib.IMAP4.error, OSError) as e:
        log.warning("Job-alert mailbox: %s", str(e)[:160])
    # forget message ids older than two weeks
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).date().isoformat()
    for k in [k for k, d in seen.items() if d < cutoff]:
        del seen[k]
    return out


# ---------------------------------------------------------------- Adzuna

ADZUNA = "https://api.adzuna.com/v1/api/jobs/us/search/{page}"
LIMITS = {"d": 240, "w": 950, "m": 2400}     # under Adzuna's free 250 / 1,000 / 2,500
# Two profiles share one free key, so each profile's config sets its own part, e.g. limits: {d: 50, w: 350, m: 1400}


def _budget(state: dict, now: datetime) -> dict:
    c = state.setdefault("adzuna_calls", {})
    day, week, month = now.date().isoformat(), now.strftime("%G-W%V"), now.strftime("%Y-%m")
    if c.get("day") != day:
        c.update(day=day, d=0)
    if c.get("week") != week:
        c.update(week=week, w=0)
    if c.get("month") != month:
        c.update(month=month, m=0)
    return c


def adzuna_jobs(state: dict, cfg: dict, now: datetime | None = None, env=os.environ, stats=None) -> list[Job]:
    app_id, app_key = (env.get("ADZUNA_APP_ID") or "").strip(), (env.get("ADZUNA_APP_KEY") or "").strip()
    if not app_id or not app_key:
        return []
    now = now or datetime.now(timezone.utc)
    terms = [str(t) for t in cfg.get("terms") or []]
    if not terms:
        return []
    per_run = int(cfg.get("calls_per_run", 10))
    lim = {**LIMITS, **{k: int(v) for k, v in (cfg.get("limits") or {}).items() if k in LIMITS}}
    lim = {k: min(v, LIMITS[k]) for k, v in lim.items()}
    c = _budget(state, now)
    start = int(state.get("adzuna_cursor", 0)) % len(terms)
    out: dict[str, Job] = {}
    calls = 0
    for i in range(len(terms)):
        if calls >= per_run or c["d"] >= lim["d"] or c["w"] >= lim["w"] or c["m"] >= lim["m"]:
            break
        term = terms[(start + i) % len(terms)]
        try:
            data = get_json(ADZUNA.format(page=1), params={
                "app_id": app_id, "app_key": app_key, "what": term, "results_per_page": 50,
                "max_days_old": int(cfg.get("max_days_old", 7)), "sort_by": "date", "content-type": "application/json"})
        except FetchError as e:
            log.warning("Adzuna search %r failed: %s", term, e)
            if e.status in (401, 403, 429):
                break
            continue
        finally:
            calls += 1
            c["d"] += 1
            c["w"] += 1
            c["m"] += 1
        state["adzuna_cursor"] = (start + i + 1) % len(terms)
        for r in data.get("results") or []:
            jid = str(r.get("id") or "")
            if not jid:
                continue
            created = None
            try:
                created = datetime.fromisoformat(str(r.get("created", "")).replace("Z", "+00:00"))
            except ValueError:
                pass
            loc = (r.get("location") or {}).get("display_name") or ""
            desc = _text(r.get("description") or "")
            out[jid] = Job(ats="adzuna", board="us", job_id=jid, company=((r.get("company") or {}).get("display_name") or "")[:80],
                           title=_text(r.get("title") or "")[:150], location=loc, url=r.get("redirect_url") or "",
                           posted_at=created, description=desc, countries=["United States"],
                           needs_detail=bool(r.get("redirect_url")), detail_ref=r.get("redirect_url") or "",
                           found_by=term)
    if stats is not None:
        stats["adzuna_calls"] += calls
    return list(out.values())


def norm_key(company: str, title: str) -> str:
    """Company + title, simplified, to spot the same job on two sources."""
    c = re.sub(r"\b(inc|llc|ltd|corp(oration)?|co|company|group|usa|us|na|north america|americas?)\b\.?", " ",
               (company or "").lower())
    c = re.sub(r"[^a-z0-9]+", " ", c).strip()
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", (title or "").lower())
    t = re.sub(r"\b(sr|senior|jr|junior|i{1,3}|iv|[1-4])\b\.?", " ", t)
    t = re.sub(r"[^a-z0-9+#]+", " ", t).strip()
    return hashlib.sha1(f"{c}|{t}".encode()).hexdigest()[:16]


def source_board(job: Job) -> Board:
    names = {"adzuna": "Jobs by Adzuna"}
    return Board(ats=job.ats, board=job.board, company=job.company or names.get(job.ats, job.board.title() + " alert"))


# ---------------------------------------------------------------- shared helpers

def _iso(s) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _fresh(d: datetime | None, now: datetime, days: int) -> bool:
    return d is None or now - d <= timedelta(days=days)


# ---------------------------------------------------------------- Workable job search (all Workable employers)
# jobs.workable.com is Workable's public job search across every company that uses it; its robots.txt
# disallows only /search?, not this API. One query returns jobs in order of relevance, so each query is
# read to the end (or `max_pages`) and kept to jobs created in the last `days`.

WORKABLE_SEARCH = "https://jobs.workable.com/api/v1/jobs"


def workable_search_jobs(cfg: dict, now: datetime | None = None, stats=None) -> list[Job]:
    now = now or datetime.now(timezone.utc)
    days = int(cfg.get("days", 7))
    out: dict[str, Job] = {}
    for term in cfg.get("terms") or []:
        token, pages, first_ids = None, 0, set()
        while pages < int(cfg.get("max_pages", 40)):
            params = {"query": term, "location": "United States"}
            if token:
                params["pageToken"] = token
            try:
                data = get_json(WORKABLE_SEARCH, params=params)
            except FetchError as e:
                log.warning("Workable search %r: %s", term, e)
                break
            pages += 1
            if stats is not None:
                stats["workable_search_calls"] += 1
            jobs = data.get("jobs") or []
            ids = {str(r.get("id")) for r in jobs}
            if not jobs or ids <= first_ids:      # the token was ignored: stop instead of looping
                break
            first_ids |= ids
            for r in jobs:
                created = _iso(r.get("created"))
                if not _fresh(created, now, days):
                    continue
                loc = r.get("location") or {}
                if (loc.get("countryName") or "United States") != "United States":
                    continue
                place = ", ".join(x for x in (loc.get("city"), loc.get("subregion")) if x)
                remote = "TELECOMMUTE" in (r.get("locations") or []) or r.get("workplace") == "remote"
                desc = _text(" ".join(str(r.get(k) or "") for k in ("description", "requirementsSection", "benefitsSection")))
                jid = str(r.get("id"))
                out[jid] = Job(ats="workable_search", board="all", job_id=jid,
                               company=((r.get("company") or {}).get("title") or "")[:80],
                               title=_text(r.get("title") or "")[:150],
                               location=(place + (" (remote)" if remote and place else "") or ("Remote, US" if remote else "")),
                               url=r.get("url") or "", posted_at=created, description=desc,
                               countries=["United States"],
                               workplace={"remote": "remote", "hybrid": "hybrid", "on_site": "onsite"}.get(r.get("workplace") or "", ""),
                               found_by=term)
            token = data.get("nextPageToken")
            if not token:
                break
            time.sleep(0.3)
    return list(out.values())


# ---------------------------------------------------------------- Hacker News "Who is hiring?"
# The monthly thread by the whoishiring account, read through the official Algolia HN API. Each top-level
# comment is one employer's post: "Company | Role(s) | Location | Remote/Onsite | ...". Only comments from
# the last `days` days are read.

HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEM = "https://hn.algolia.com/api/v1/items/{id}"


def hn_jobs(cfg: dict, title_ok, now: datetime | None = None) -> list[Job]:
    """`title_ok(text) -> bool` says whether a role name is one we want."""
    now = now or datetime.now(timezone.utc)
    days = int(cfg.get("days", 7))
    try:
        stories = get_json(HN_SEARCH, params={"tags": "story,author_whoishiring", "hitsPerPage": 6}).get("hits") or []
    except FetchError as e:
        log.warning("HN search: %s", e)
        return []
    out = []
    for st in stories:
        if "who is hiring" not in (st.get("title") or "").lower():
            continue
        if now - (_iso(st.get("created_at")) or now) > timedelta(days=40):
            continue
        try:
            item = get_json(HN_ITEM.format(id=st["objectID"]))
        except FetchError as e:
            log.warning("HN thread: %s", e)
            continue
        for c in item.get("children") or []:
            posted = _iso(c.get("created_at"))
            if not c.get("text") or not _fresh(posted, now, days):
                continue
            out.extend(_hn_post(c, posted, title_ok))
    return out


_ROLE_SPLIT = re.compile(r"\s*(?:;|,|/| - | – |\band\b|&)\s*", re.I)


def _hn_post(c: dict, posted, title_ok) -> list[Job]:
    paras = [_text(p) for p in re.split(r"<p>|<br\s*/?>", c["text"])]
    paras = [p for p in paras if p]
    if not paras:
        return []
    first, text = paras[0], "\n".join(paras)
    parts = [p.strip() for p in first.split("|") if p.strip()]
    if len(parts) < 2:
        return []
    company = re.sub(r"\s*\(.*?\)", "", parts[0])[:80]
    loc_part = next((p for p in parts[1:] if re.search(r"\b(remote|onsite|on-site|hybrid|us|usa|united states|"
                                                         r"[A-Z][a-z]+,\s*[A-Z]{2}|new york|nyc|sf|san francisco|"
                                                         r"seattle|austin|boston|chicago)\b", p, re.I)), "")
    roles = []
    for p in parts[1:]:
        if not title_ok(p):
            continue
        pieces = [r.strip() for r in _ROLE_SPLIT.split(p) if r.strip()]
        good = [r for r in pieces if title_ok(r) and len(r) <= 90]
        roles += good or ([p] if len(p) <= 90 else [])
    if not roles:   # roles listed further down ("We're hiring: - SRE ...")
        for line in paras[1:25]:
            line = re.sub(r"^[\s*•\-–]+", "", line).strip()
            if 4 <= len(line) <= 90 and title_ok(line):
                roles.append(line)
    jobs = []
    for i, r in enumerate(dict.fromkeys(roles)):
        jobs.append(Job(ats="hn", board="whoishiring", job_id=f"{c['id']}-{i}", company=company, title=r[:150],
                        location=loc_part[:80], url=f"https://news.ycombinator.com/item?id={c['id']}",
                        posted_at=posted, description=text[:20000],
                        workplace="remote" if re.search(r"\bremote\b", loc_part, re.I) else ""))
    return jobs


# ---------------------------------------------------------------- Remote OK
# Public API; its terms ask for a link back to Remote OK and a mention of it as the source (both in the
# report). The first element of the list is the legal notice, not a job.

REMOTEOK = "https://remoteok.com/api"
_WORLDWIDE = re.compile(r"worldwide|anywhere|global|remote|united states|\busa?\b|north america|americas|\bus[- ]only", re.I)


def _remote_place(loc: str) -> str:
    """'' when a remote job is not open to people in the US; otherwise a location the US filter accepts."""
    loc = (loc or "").strip()
    if not loc or _WORLDWIDE.search(loc):
        return f"Remote, US ({loc})" if loc and not re.search(r"\bus|usa|united states", loc, re.I) else (loc or "Remote, US")
    return loc


def remoteok_jobs(cfg: dict, now: datetime | None = None) -> list[Job]:
    now = now or datetime.now(timezone.utc)
    days = int(cfg.get("days", 7))
    try:
        data = get_json(REMOTEOK, params={"tag": cfg["tag"]} if cfg.get("tag") else None)
    except FetchError as e:
        log.warning("Remote OK: %s", e)
        return []
    out = []
    for r in data if isinstance(data, list) else []:
        if not isinstance(r, dict) or not r.get("id") or not r.get("position"):
            continue
        posted = _iso(r.get("date"))
        if not _fresh(posted, now, days):
            continue
        out.append(Job(ats="remoteok", board="remoteok", job_id=str(r["id"]), company=(r.get("company") or "")[:80],
                       title=_text(r.get("position"))[:150], location=_remote_place(r.get("location") or ""),
                       url=r.get("url") or f"https://remoteok.com/remote-jobs/{r['id']}", posted_at=posted,
                       description=_text(r.get("description") or "") + "\nTags: " + ", ".join(r.get("tags") or []),
                       workplace="remote"))
    return out


# ---------------------------------------------------------------- We Work Remotely (RSS)
# The category RSS feed; each item links back to We Work Remotely, which is credited in the report.

def wwr_jobs(cfg: dict, now: datetime | None = None) -> list[Job]:
    now = now or datetime.now(timezone.utc)
    days = int(cfg.get("days", 7))
    out = []
    for feed in cfg.get("feeds") or ["https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss"]:
        try:
            root = ET.fromstring(get_text(feed, accept="application/rss+xml, application/xml, text/xml"))
        except (FetchError, ET.ParseError) as e:
            log.warning("We Work Remotely feed: %s", e)
            continue
        for it in root.iter("item"):
            g = lambda tag: (it.findtext(tag) or "").strip()
            posted = None
            try:
                posted = email.utils.parsedate_to_datetime(g("pubDate"))
            except (TypeError, ValueError):
                pass
            if not _fresh(posted, now, days):
                continue
            company, _, title = g("title").partition(":")
            if not title:
                company, title = "", company
            link = g("link") or g("guid")
            jid = hashlib.sha1(link.encode()).hexdigest()[:16]
            out.append(Job(ats="wwr", board="weworkremotely", job_id=jid, company=company.strip()[:80],
                           title=title.strip()[:150], location=_remote_place(g("region")), url=link,
                           posted_at=posted, description=_text(g("description")), workplace="remote"))
    return out


# ---------------------------------------------------------------- USAJOBS (federal jobs)
# Official API with a free key (developer.usajobs.gov). Secrets: USAJOBS_API_KEY and USAJOBS_EMAIL (the
# email the key was requested with; sent as the User-Agent, as the API requires). Most federal jobs require
# US citizenship; who may apply and the clearance are added to the job text so the usual citizenship and
# clearance rules leave those out.

USAJOBS = "https://data.usajobs.gov/api/search"


def usajobs_jobs(cfg: dict, env=os.environ, stats=None) -> list[Job]:
    key, mail = (env.get("USAJOBS_API_KEY") or "").strip(), (env.get("USAJOBS_EMAIL") or "").strip()
    if not key or not mail:
        return []
    headers = {"Host": "data.usajobs.gov", "User-Agent": mail, "Authorization-Key": key}
    out: dict[str, Job] = {}
    queries = [{"Keyword": k} for k in cfg.get("keywords") or []]
    if cfg.get("series"):
        queries.append({"JobCategoryCode": ";".join(str(x) for x in cfg["series"])})
    for q in queries:
        page = 1
        while page <= 5:
            try:
                data = get_json(USAJOBS, params={**q, "DatePosted": int(cfg.get("days", 7)), "ResultsPerPage": 500,
                                                 "Page": page}, headers=headers)
            except FetchError as e:
                log.warning("USAJOBS %s: %s", q, e)
                break
            if stats is not None:
                stats["usajobs_calls"] += 1
            res = (data.get("SearchResult") or {})
            items = res.get("SearchResultItems") or []
            for it in items:
                d = it.get("MatchedObjectDescriptor") or {}
                pid = str(d.get("PositionID") or it.get("MatchedObjectId") or "")
                if not pid:
                    continue
                locs = d.get("PositionLocation") or []
                if locs and not any((l.get("CountryCode") or "").lower() in ("united states", "us", "usa") or
                                    (l.get("CountrySubDivisionCode") or "") for l in locs):
                    continue
                det = (d.get("UserArea") or {}).get("Details") or {}
                who = (det.get("WhoMayApply") or {}).get("Name") or ""
                clearance = str(det.get("SecurityClearance") or "")
                hiring = ", ".join(det.get("HiringPath") or [])
                extra = []
                if re.search(r"citizen|public", f"{who} {hiring}", re.I) and not re.search(r"non-?citizen", who, re.I):
                    extra.append("Requires U.S. citizenship.")
                if clearance and not re.search(r"not (required|applicable)|^none$|^$", clearance, re.I):
                    extra.append(f"Requires a {clearance} security clearance.")
                desc = _text(" ".join([*extra, f"Who may apply: {who}.", f"Hiring paths: {hiring}.",
                                       str(det.get("MajorDuties") or ""), str(d.get("QualificationSummary") or ""),
                                       str(det.get("Requirements") or ""), str(det.get("Education") or "")]))
                place = d.get("PositionLocationDisplay") or ""
                out[pid] = Job(ats="usajobs", board="federal", job_id=pid,
                               company=(d.get("OrganizationName") or d.get("DepartmentName") or "")[:80],
                               title=_text(d.get("PositionTitle") or "")[:150], location=place[:120],
                               url=d.get("PositionURI") or "", posted_at=_iso(d.get("PublicationStartDate")),
                               description=desc, countries=["United States"])
            total = int(res.get("SearchResultCountAll") or 0)
            if page * 500 >= total or not items:
                break
            page += 1
    return list(out.values())


# ---------------------------------------------------------------- Y Combinator startup jobs (Work at a Startup)
# ycombinator.com/jobs lists the jobs YC companies post on Work at a Startup (robots.txt allows /jobs).
# Each role page carries its listings as JSON in a data-page attribute.

YC_JOBS = "https://www.ycombinator.com/jobs/role/{role}"


def yc_jobs(cfg: dict, now: datetime | None = None) -> list[Job]:
    import html as htmllib
    import json
    out: dict[str, Job] = {}
    for role in cfg.get("roles") or ["software-engineer"]:
        try:
            page = get_text(YC_JOBS.format(role=role))
        except FetchError as e:
            log.warning("YC jobs %s: %s", role, e)
            continue
        for m in re.finditer(r'data-page="([^"]+)"', page):
            try:
                props = json.loads(htmllib.unescape(m.group(1))).get("props") or {}
            except ValueError:
                continue
            for r in props.get("jobPostings") or []:
                jid, title, path = str(r.get("id") or ""), (r.get("title") or "").strip(), r.get("url") or ""
                if not jid or not title or jid in out:
                    continue
                slug = (re.search(r"/companies/([^/]+)/", path) or [None, ""])[1]
                co = r.get("company")
                company = r.get("companyName") or (co.get("name") if isinstance(co, dict) else co) or \
                    slug.replace("-", " ").title()
                url = "https://www.ycombinator.com" + path if path.startswith("/") else path
                loc = r.get("location") or ""
                out[jid] = Job(ats="yc", board="workatastartup", job_id=jid, company=(company or "")[:80],
                               title=title[:150], location=loc, url=url,
                               posted_at=_iso(r.get("createdAt") or r.get("publishedAt")),
                               description=" ".join(str(r.get(k) or "") for k in ("roleSpecificType", "type", "salaryRange", "minExperience")),
                               workplace="remote" if re.search(r"\bremote\b", loc, re.I) else "",
                               needs_detail=True, detail_ref=url)
        time.sleep(1)
    return list(out.values())
