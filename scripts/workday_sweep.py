"""Shared Workday sweep: reads every Workday job board once, slowly, for all radars.

Workday sits behind Cloudflare, which turns a machine away when it asks too much. Six radars each reading
~2,700 Workday boards (many of them 10-20 pages) was far too much, so this one job reads them all at about
one request a second, oldest-read boards first, and keeps each board's recent jobs (posted in the last
`--days` days, or with no date). The radars read the listings from this file and only open the job pages
they need.

    python scripts/workday_sweep.py --cache data/shared/workday_jobs.json.gz --budget-min 50

The cache is kept between runs as a workflow artifact (see .github/workflows/workday-sweep.yml).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radar import ats as ATS  # noqa: E402
from radar import http  # noqa: E402
from radar import store  # noqa: E402
from radar.http import FetchError  # noqa: E402
from radar.models import Board  # noqa: E402
from radar.workday_shared import job_to_dict, load_cache, save_cache  # noqa: E402

log = logging.getLogger("workday_sweep")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def all_workday_boards() -> list[Board]:
    boards = {b.key.lower(): b for b in store.load_boards() if b.ats == "workday"}
    for b in store.load_discovered(set(boards)) + store.load_discovered(set(boards), store.CAREERS_BOARDS_PATH, named=True):
        if b.ats == "workday":
            boards.setdefault(b.key.lower(), b)
    # the profiles' own employer lists (e.g. the Auto Radar's)
    prof_dir = os.path.join(ROOT, "profiles")
    for name in sorted(os.listdir(prof_dir)) if os.path.isdir(prof_dir) else []:
        path = os.path.join(prof_dir, name, "companies.csv")
        if os.path.exists(path):
            for b in store.load_focus_boards(path):
                if b.ats == "workday":
                    boards.setdefault(b.key.lower(), Board(ats=b.ats, board=b.board, company=b.company))
    return list(boards.values())


def sweep(cache: dict, boards: list[Board], budget_s: float, days: int, max_pages: int) -> dict:
    now = datetime.now(timezone.utc)
    end = time.monotonic() + budget_s
    entries = cache.setdefault("boards", {})
    keep = {b.key for b in boards}
    for k in [k for k in entries if k not in keep]:   # boards no longer listed
        del entries[k]
    boards.sort(key=lambda b: (entries.get(b.key) or {}).get("at", ""))
    stats = {"ok": 0, "failed": 0, "rate_limited": 0, "jobs": 0}
    limited_in_row = 0
    for b in boards:
        if time.monotonic() > end:
            break
        try:
            jobs = ATS.workday_list(b, max_pages=max_pages, max_age_days=days, us_only=True)
        except FetchError as e:
            if stats["rate_limited"] + stats["failed"] < 10:
                log.warning("%s: %s | cause: %r", b.board, e, getattr(e, "__cause__", None))
            if e.status == 429:
                stats["rate_limited"] += 1
                limited_in_row += 1
                if stats["ok"] == 0 and limited_in_row >= 2:
                    log.warning("Workday refuses this machine from the start (it blocks many GitHub machines): "
                                "stopping without asking further")
                    stats["refused"] = True
                    break
                if limited_in_row >= 8:
                    log.warning("Workday keeps answering 'too many requests': stopping this sweep early")
                    break
                time.sleep(min(60, 10 * limited_in_row))
                continue
            stats["failed"] += 1
            entries[b.key] = {"at": now.isoformat(timespec="minutes"), "error": str(e)[:60],
                              "jobs": (entries.get(b.key) or {}).get("jobs", [])}
            continue
        limited_in_row = 0
        cutoff = now - timedelta(days=days + 3)
        recent = [j for j in jobs if not j.posted_at or j.posted_at >= cutoff]
        entries[b.key] = {"at": datetime.now(timezone.utc).isoformat(timespec="minutes"), "company": b.company,
                          "jobs": [job_to_dict(j) for j in recent]}
        stats["ok"] += 1
        stats["jobs"] += len(recent)
    fresh = sum(1 for e in entries.values() if e.get("at", "") >= (now - timedelta(hours=24)).isoformat())
    stats.update(boards=len(boards), read_in_24h=fresh)
    cache["updated"] = datetime.now(timezone.utc).isoformat(timespec="minutes")
    return stats


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--cache", default=os.path.join(ROOT, "data", "shared", "workday_jobs.json.gz"))
    p.add_argument("--budget-min", type=float, default=50)
    p.add_argument("--days", type=int, default=10)
    p.add_argument("--max-pages", type=int, default=20)
    p.add_argument("--gap", type=float, default=1.0, help="seconds between Workday requests")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    http.set_pace("myworkdayjobs.com", a.gap)
    shown = []

    def note(r, *args, **kw):   # log the first answers that aren't OK (status, wait asked, message)
        if r.status_code != 200 and len(shown) < 15:
            shown.append(1)
            log.warning("Workday answered %s %s (retry-after %s): %s", r.status_code, r.url[:80],
                        r.headers.get("retry-after"), r.text[:120].replace("\n", " "))
    http.session().hooks["response"].append(note)
    cache = load_cache(a.cache)
    boards = all_workday_boards()
    stats = sweep(cache, boards, a.budget_min * 60, a.days, a.max_pages)
    save_cache(a.cache, cache)
    log.info("Workday sweep: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
