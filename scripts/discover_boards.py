"""Find more employers' job boards on the systems the radar reads, using the Common Crawl URL index.

For each job system, ask Common Crawl's public index which company boards it has seen
(e.g. every jobs.lever.co/<company>), then check each new board once with the radar's own
reader and keep it only if it currently lists US (or remote/unspecified) technical jobs.
Results go to discovered.csv, which the radar scans on a rotation.

Run in GitHub Actions (weekly): python scripts/discover_boards.py [--max-pages N] [--max-new N]
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import parse_qs, unquote, urlsplit

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from radar import ats as ATS  # noqa: E402
from radar.http import FetchError  # noqa: E402
from radar.match import classify_location  # noqa: E402
from radar.models import Board  # noqa: E402
from radar.store import COMPANIES_PATH, CUSTOM_PATH  # noqa: E402

DISCOVERED_PATH = os.path.join(ROOT, "discovered.csv")
FIELDS = ["ats", "board", "company", "us_tech_jobs", "jobs", "checked"]
UA = "job-radar-discovery/1.0 (personal job search; +https://github.com/chandrakodali/job-radar)"
log = logging.getLogger("discover")

TECH = re.compile(r"engineer|developer|devops|\bsre\b|site reliability|cloud|infrastructure|platform|"
                  r"systems? admin|sysadmin|\bit\b|network|security|technical|software|data|linux|kubernetes", re.I)

SKIP_SUB = {"www", "app", "api", "help", "support", "status", "blog", "docs", "go", "partners", "marketplace",
            "careers", "jobs", "info", "static", "assets", "cdn", "mail", "login", "secure", "demo", "community"}
LOCALE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2})?$")


def _seg(path: str, n: int = 0) -> str:
    parts = [unquote(p) for p in path.split("/") if p]
    return parts[n] if len(parts) > n else ""


def _ok_slug(s: str) -> bool:
    return bool(s) and len(s) <= 80 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", s) is not None


def ex_greenhouse(u):
    if u.netloc not in ("boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io",
                        "job-boards.eu.greenhouse.io"):
        return None
    s = _seg(u.path)
    if s == "embed":
        s = (parse_qs(u.query).get("for") or [""])[0]
    return s.lower() if _ok_slug(s) and s not in ("embed", "v1", "jobs") else None


def ex_lever(u):
    if u.netloc not in ("jobs.lever.co", "jobs.eu.lever.co"):
        return None
    s = _seg(u.path)
    return s.lower() if _ok_slug(s) else None


def ex_ashby(u):
    if u.netloc != "jobs.ashbyhq.com":
        return None
    s = _seg(u.path)
    return s if _ok_slug(s) and s.lower() not in ("api", "embed") else None


def ex_workable(u):
    if u.netloc != "apply.workable.com":
        return None
    s = _seg(u.path)
    return s.lower() if _ok_slug(s) and s.lower() not in ("api", "j", "careers", "oauth", "static") else None


def ex_smartrecruiters(u):
    if u.netloc not in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com"):
        return None
    s = _seg(u.path)
    return s if _ok_slug(s) and s.lower() not in ("oneclick-ui", "sr-jobs", "ni", "api", "static", "embed") else None


def ex_rippling(u):
    if u.netloc != "ats.rippling.com":
        return None
    s = _seg(u.path)
    if LOCALE.match(s):
        s = _seg(u.path, 1)
    return s.lower() if _ok_slug(s) and s.lower() not in ("api", "static") else None


def _sub(u, domain):
    host = u.netloc.lower().split(":")[0]
    if not host.endswith("." + domain):
        return None
    sub = host[: -len(domain) - 1]
    return sub if sub and "." not in sub and sub not in SKIP_SUB and _ok_slug(sub) else None


def ex_bamboohr(u):
    sub = _sub(u, "bamboohr.com")
    return sub if sub and (u.path.startswith("/careers") or u.path.startswith("/jobs")) else None


def ex_recruitee(u):
    return _sub(u, "recruitee.com")


def ex_workday(u):
    host = u.netloc.lower()
    m = re.fullmatch(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com", host)
    if not m:
        return None
    s = _seg(u.path)
    if LOCALE.match(s):
        s = _seg(u.path, 1)
    if not _ok_slug(s) or s.lower() in ("wday", "api", "static"):
        return None
    return f"{m.group(1)}|{m.group(2)}|{s}"


def _sub_any(u, domain, skip=()):
    sub = _sub(u, domain)
    return sub if sub and sub not in skip else None


def ex_jazzhr(u):
    sub = _sub_any(u, "applytojob.com")
    return sub.lower() if sub and not re.match(r"^\d{8,}", sub) else None


def ex_jobvite(u):
    if u.netloc.lower() != "jobs.jobvite.com":
        return None
    s = _seg(u.path)
    return s.lower() if _ok_slug(s) and s.lower() not in ("careers", "api", "static", "jobs", "search", "assets") else None


def ex_icims(u):
    host = u.netloc.lower().split(":")[0]
    if not host.endswith(".icims.com") or re.match(r"^(www\d*|cdn|media|developer|community|login|ats)\.", host):
        return None
    return host if u.path.lower().startswith("/jobs") else None


def ex_pinpoint(u):
    return _sub_any(u, "pinpointhq.com", {"developers", "support"})


def ex_breezy(u):
    return _sub_any(u, "breezy.hr", {"marketing", "attract", "developer"})


def ex_teamtailor(u):
    return _sub_any(u, "teamtailor.com", {"career", "app-eu", "app-na", "partner"})


def ex_gem(u):
    if u.netloc.lower() != "jobs.gem.com":
        return None
    s = _seg(u.path)
    return s if _ok_slug(s) and s.lower() not in ("api", "static", "assets") else None


def ex_dover(u):
    if u.netloc.lower() != "app.dover.com" or _seg(u.path) != "jobs":
        return None
    s = _seg(u.path, 1)
    return s if _ok_slug(s) else None


def ex_dayforce(u):
    if u.netloc.lower() != "jobs.dayforcehcm.com":
        return None
    parts = [p for p in u.path.split("/") if p]
    if parts and LOCALE.match(parts[0]):
        parts = parts[1:]
    if len(parts) < 2 or not _ok_slug(parts[0]) or not _ok_slug(parts[1]) or parts[0].lower() == "api":
        return None
    return f"{parts[0]}|{parts[1]}"


def ex_avature(u):
    host = u.netloc.lower()
    if not host.endswith(".avature.net") or host.startswith("www."):
        return None
    parts = [p for p in u.path.split("/") if p]
    if parts and re.fullmatch(r"[a-z]{2}_[A-Z]{2}", parts[0]):
        parts = parts[1:]
    if not parts or not _ok_slug(parts[0]) or parts[0].lower() in ("docusign", "error", "static", "portal"):
        return None
    return f"{host}|{parts[0]}"


def ex_eightfold(u):
    return _sub_any(u, "eightfold.ai", {"docs", "learn", "trust", "careers", "events", "engage"})


def ex_paylocity(u):
    if u.netloc.lower() != "recruiting.paylocity.com":
        return None
    m = re.search(r"/recruiting/jobs/(?:all|list)/([0-9a-f-]{36})", u.path, re.I)
    return m.group(1).lower() if m else None


def ex_oracle(u):
    host = u.netloc.lower()
    m = re.search(r"/hcmUI/CandidateExperience/[a-z]{2}(?:-[A-Z]{2})?/sites/([A-Za-z0-9_]+)", u.path)
    return f"{host}|{m.group(1)}" if m and host.endswith(".oraclecloud.com") else None


def ex_adp(u):
    if u.netloc.lower() != "workforcenow.adp.com":
        return None
    q = parse_qs(u.query)
    cid = (q.get("cid") or [""])[0]
    if not re.fullmatch(r"[0-9a-f-]{36}", cid or "", re.I):
        return None
    cc = (q.get("ccId") or q.get("ccid") or [""])[0]
    return f"{cid}|{cc}" if cc else cid


def ex_zoho(u):
    return _sub_any(u, "zohorecruit.com")


def ex_personio(u):
    host = u.netloc.lower().split(":")[0]
    m = re.fullmatch(r"([a-z0-9-]+)\.jobs\.personio\.(de|com)", host)
    return host if m and m.group(1) not in SKIP_SUB else None


def ex_catsone(u):
    return _sub_any(u, "catsone.com", {"sitemap", "cp", "secure"})


def ex_careerpuck(u):
    if u.netloc.lower() != "app.careerpuck.com" or _seg(u.path) != "job-board":
        return None
    s = _seg(u.path, 1)
    return s if _ok_slug(s) else None


def ex_join(u):
    if u.netloc.lower() not in ("join.com", "www.join.com") or _seg(u.path) != "companies":
        return None
    s = _seg(u.path, 1)
    return s if _ok_slug(s) else None


def ex_homerun(u):
    return _sub_any(u, "homerun.co", {"feedback", "academy"})


ORACLE_REGIONS = ["us2", "us6", "us3", "us1", "us4", "em2", "em3", "em5", "ca2", "ca3", "ap1", "ocs"]

SOURCES = [
    ("greenhouse", ["boards.greenhouse.io/*", "job-boards.greenhouse.io/*"], ex_greenhouse),
    ("lever", ["jobs.lever.co/*"], ex_lever),
    ("ashby", ["jobs.ashbyhq.com/*"], ex_ashby),
    ("workable", ["apply.workable.com/*"], ex_workable),
    ("smartrecruiters", ["jobs.smartrecruiters.com/*", "careers.smartrecruiters.com/*"], ex_smartrecruiters),
    ("rippling", ["ats.rippling.com/*"], ex_rippling),
    ("bamboohr", ["*.bamboohr.com"], ex_bamboohr),
    ("recruitee", ["*.recruitee.com"], ex_recruitee),
    ("workday", ["*.myworkdayjobs.com"], ex_workday),
    ("jazzhr", ["*.applytojob.com"], ex_jazzhr),
    ("jobvite", ["jobs.jobvite.com/*"], ex_jobvite),
    ("icims", ["*.icims.com"], ex_icims),
    ("pinpoint", ["*.pinpointhq.com"], ex_pinpoint),
    ("breezy", ["*.breezy.hr"], ex_breezy),
    ("teamtailor", ["*.teamtailor.com"], ex_teamtailor),
    ("gem", ["jobs.gem.com/*"], ex_gem),
    ("dover", ["app.dover.com/jobs/*"], ex_dover),
    ("dayforce", ["jobs.dayforcehcm.com/*"], ex_dayforce),
    ("avature", ["*.avature.net"], ex_avature),
    ("eightfold", ["*.eightfold.ai"], ex_eightfold),
    ("paylocity", ["recruiting.paylocity.com/recruiting/jobs/*"], ex_paylocity),
    ("oracle", [f"*.fa.{r}.oraclecloud.com" for r in ORACLE_REGIONS], ex_oracle),
    ("adp", ["workforcenow.adp.com/mascsr/*"], ex_adp),
    ("zoho", ["*.zohorecruit.com"], ex_zoho),
    ("personio", ["*.jobs.personio.de", "*.jobs.personio.com"], ex_personio),
    ("catsone", ["*.catsone.com"], ex_catsone),
    ("careerpuck", ["app.careerpuck.com/job-board/*"], ex_careerpuck),
    ("join", ["join.com/companies/*"], ex_join),
    ("homerun", ["*.homerun.co"], ex_homerun),
]


# ------------------------------------------------------------------ Common Crawl index

class CC:
    def __init__(self, crawls: int, max_pages: int, budget_s: float):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.failures_in_row = 0
        self.max_pages = max_pages
        self.deadline = time.monotonic() + budget_s
        try:
            info = self._get("https://index.commoncrawl.org/collinfo.json").json()
            self.apis = [c["cdx-api"] for c in info[:crawls]]
        except (RuntimeError, TimeoutError, ValueError) as e:
            log.warning("Common Crawl index list unavailable: %s", e)
            self.apis = []
        log.info("Common Crawl indexes: %s", [a.rsplit("/", 1)[-1] for a in self.apis])

    def _get(self, url, params=None):
        if self.failures_in_row >= 12:
            raise TimeoutError("Common Crawl index is overloaded; moving on with what was found")
        for attempt in range(3):
            if time.monotonic() > self.deadline:
                raise TimeoutError("discovery time budget used up")
            try:
                r = self.s.get(url, params=params, timeout=60)
            except requests.RequestException as e:
                log.warning("CC %s: %s", type(e).__name__, url)
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code in (200, 404):
                self.failures_in_row = 0
                time.sleep(1.0)  # be gentle with a free public service
                return r
            log.warning("CC HTTP %s (attempt %d)", r.status_code, attempt + 1)
            time.sleep(8 * (attempt + 1))
        self.failures_in_row += 1
        raise RuntimeError("Common Crawl index unavailable")

    def urls(self, pattern: str):
        for api in self.apis:
            base = {"url": pattern, "output": "json", "fl": "url", "pageSize": "1"}
            try:
                r = self._get(api, {**base, "showNumPages": "true"})
                if r.status_code == 404:
                    continue
                pages = int(json.loads(r.text.strip().splitlines()[0]).get("pages", 0))
            except (ValueError, KeyError, RuntimeError, IndexError) as e:
                log.warning("CC page count failed for %s: %s", pattern, e)
                continue
            todo = list(range(pages))
            if pages > self.max_pages:  # sample different pages each week so coverage grows over time
                todo = sorted(random.sample(todo, self.max_pages))
            log.info("%s in %s: %d pages, reading %d", pattern, api.rsplit("/", 1)[-1], pages, len(todo))
            for p in todo:
                try:
                    r = self._get(api, {**base, "page": str(p)})
                except RuntimeError as e:
                    log.warning("skipping a page of %s: %s", pattern, e)
                    continue
                for line in r.text.splitlines():
                    try:
                        yield json.loads(line)["url"]
                    except (ValueError, KeyError):
                        continue


# ------------------------------------------------------------------ employers seen on other sources

SLUG_SYSTEMS = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters")
_SUFFIX = re.compile(r"\b(inc|llc|ltd|corp(oration)?|co|company|group|holdings|technologies|usa|us)\b\.?", re.I)


def missed_company_slugs(known: set[str]):
    names = set()
    for path in (os.path.join(ROOT, "data", "state.json"), os.path.join(ROOT, "data", "automotive", "state.json")):
        try:
            with open(path, encoding="utf-8") as f:
                names |= set((json.load(f).get("missed_companies") or {}).keys())
        except (OSError, ValueError):
            continue
    lca = []    # employers that filed for DevOps/SRE/cloud titles (scripts/lca_employers.py --devops)
    path = os.path.join(ROOT, "lca_employers_devops.csv")
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            lca = [r["employer"] for r in csv.DictReader(f) if not r.get("watched")][:2000]
    for name, systems in [(n, SLUG_SYSTEMS) for n in sorted(names)] + [(n, SLUG_SYSTEMS[:3]) for n in lca]:
        base = re.sub(r"[^a-z0-9 ]+", " ", _SUFFIX.sub(" ", name.lower())).split()
        if not base:
            continue
        slugs = ["".join(base), "-".join(base)] if systems is SLUG_SYSTEMS else ["".join(base)]
        for slug in dict.fromkeys(slugs):
            if len(slug) < 3:
                continue
            for ats in systems:
                if f"{ats}:{slug}" not in known:
                    yield ats, slug


# ------------------------------------------------------------------ validation

def known_keys() -> set[str]:
    keys = set()
    for path in (COMPANIES_PATH, CUSTOM_PATH, DISCOVERED_PATH):
        if os.path.exists(path):
            with open(path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    keys.add(f"{(r.get('ats') or '').strip().lower()}:{(r.get('board') or '').strip().lower()}")
    return keys


def pretty(board: str) -> str:
    s = board.split("|")[0]
    s = re.sub(r"[-_.]+", " ", s)
    return s.title() if s.islower() or s.isupper() else s


def check(ats: str, board: str) -> dict | None:
    b = Board(ats=ats, board=board, company=pretty(board))
    try:
        jobs = ATS.LISTERS[ats](b)
    except FetchError:
        return None
    except Exception:
        return None
    us_tech = sum(1 for j in jobs if TECH.search(j.title or "")
                  and classify_location(j.location, j.countries) != "non_us")
    company = b.company
    for j in jobs:
        if getattr(j, "company", "") and j.company != b.company and j.company != board:
            company = j.company
            break
    return {"ats": ats, "board": board, "company": company, "us_tech_jobs": us_tech, "jobs": len(jobs),
            "checked": date.today().isoformat()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--crawls", type=int, default=2)
    ap.add_argument("--max-pages", type=int, default=30, help="index pages per pattern per crawl")
    ap.add_argument("--max-new", type=int, default=4000, help="new boards to check per system")
    ap.add_argument("--budget-min", type=float, default=100)
    ap.add_argument("--only", default="", help="comma list of systems")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    index_budget = args.budget_min * 60 * 0.45
    index_end = time.monotonic() + index_budget
    cc = CC(args.crawls, args.max_pages, index_budget)
    known = known_keys()
    found: dict[str, set[str]] = defaultdict(set)
    systems = [s for s in SOURCES if not only or s[0] in only]
    # Each system gets an equal share of the index time (unused time passes to the next), so a slow
    # system can no longer use it all up and leave the others unread.
    for i, (ats, patterns, ex) in enumerate(systems):
        cc.deadline = time.monotonic() + max(0.0, index_end - time.monotonic()) / (len(systems) - i)
        cc.failures_in_row = 0
        seen_lower = set()
        try:
            for pat in patterns:
                for url in cc.urls(pat):
                    try:
                        b = ex(urlsplit(url))
                    except ValueError:
                        b = None
                    if b and b.lower() not in seen_lower and f"{ats}:{b.lower()}" not in known:
                        seen_lower.add(b.lower())
                        found[ats].add(b)
        except TimeoutError as e:
            log.warning("Index phase for %s stopped: %s", ats, e)
        log.info("%s: %d new candidate boards", ats, len(found[ats]))

    # Employers other job sources listed whose own board the radar does not read yet (kept by the radar
    # in its state files): try their name as a board on the systems that use plain company slugs.
    for ats, b in missed_company_slugs(known):
        found[ats].add(b)
    log.info("Candidates from employers seen on other job sources: %d", sum(1 for _ in missed_company_slugs(known)))

    # Check each candidate once with the radar's own reader.
    sems = defaultdict(lambda: threading.Semaphore(6))
    sems["workable"] = threading.Semaphore(1)
    sems["workday"] = threading.Semaphore(4)
    for slow in ("jobvite", "avature", "eightfold", "dayforce", "adp", "oracle", "join", "zoho", "catsone", "homerun"):
        sems[slow] = threading.Semaphore(3)
    deadline = time.monotonic() + args.budget_min * 60 * 0.45

    def run(ats, board):
        if time.monotonic() > deadline:
            return ats, None
        with sems[ats]:
            if ats == "workable":
                time.sleep(1.5)
            return ats, check(ats, board)

    lists = []
    for ats, boards in found.items():
        boards = sorted(boards)
        random.shuffle(boards)
        lists.append([(ats, b) for b in boards[: args.max_new]])
    todo = []    # round-robin across systems, so every system gets checked before time runs out
    for i in range(max((len(x) for x in lists), default=0)):
        todo += [x[i] for x in lists if i < len(x)]
    kept, stats = [], Counter()
    with ThreadPoolExecutor(max_workers=24) as pool:
        for fut in as_completed([pool.submit(run, a, b) for a, b in todo]):
            ats, res = fut.result()
            stats[f"{ats}_checked"] += 1
            if res and res["us_tech_jobs"] > 0:
                kept.append(res)
                stats[f"{ats}_kept"] += 1
    existing = []
    if os.path.exists(DISCOVERED_PATH):
        with open(DISCOVERED_PATH, newline="", encoding="utf-8") as f:
            existing = list(csv.DictReader(f))
    rows = {f"{r['ats']}:{r['board'].lower()}": r for r in existing}
    for r in kept:
        rows[f"{r['ats']}:{r['board'].lower()}"] = r
    with open(DISCOVERED_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows.values(), key=lambda r: (r["ats"], r["board"].lower())):
            w.writerow(r)
    log.info("Checked/kept per system: %s", dict(stats))
    log.info("discovered.csv now holds %d boards (%d added this run)", len(rows), len(kept))
    return 0


if __name__ == "__main__":
    sys.exit(main())
