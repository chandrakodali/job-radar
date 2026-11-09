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
    cc = CC(args.crawls, args.max_pages, args.budget_min * 60 * 0.5)
    known = known_keys()
    found: dict[str, set[str]] = defaultdict(set)
    for ats, patterns, ex in SOURCES:
        if only and ats not in only:
            continue
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
            log.warning("Index phase stopped at %s: %s", ats, e)
            log.info("%s: %d new candidate boards", ats, len(found[ats]))
            break
        log.info("%s: %d new candidate boards", ats, len(found[ats]))

    # Check each candidate once with the radar's own reader.
    sems = defaultdict(lambda: threading.Semaphore(6))
    sems["workable"] = threading.Semaphore(1)
    sems["workday"] = threading.Semaphore(4)
    deadline = time.monotonic() + args.budget_min * 60 * 0.45

    def run(ats, board):
        if time.monotonic() > deadline:
            return ats, None
        with sems[ats]:
            if ats == "workable":
                time.sleep(1.5)
            return ats, check(ats, board)

    todo = []
    for ats, boards in found.items():
        boards = sorted(boards)
        random.shuffle(boards)
        todo += [(ats, b) for b in boards[: args.max_new]]
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
