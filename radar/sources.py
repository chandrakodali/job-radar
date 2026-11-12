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
from datetime import datetime, timedelta, timezone

from .http import FetchError, get_json
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
    c = _budget(state, now)
    start = int(state.get("adzuna_cursor", 0)) % len(terms)
    out: dict[str, Job] = {}
    calls = 0
    for i in range(len(terms)):
        if calls >= per_run or c["d"] >= LIMITS["d"] or c["w"] >= LIMITS["w"] or c["m"] >= LIMITS["m"]:
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
