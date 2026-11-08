"""Run one radar pass: fetch boards, find new matching jobs, alert, update report and state.

Usage:
    python -m radar.main                 # normal run (used by GitHub Actions)
    python -m radar.main --dry-run       # no notifications
    python -m radar.main --only greenhouse --limit 20
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yaml

from . import ats as ATS
from .http import FetchError
from .match import Rules, assess, classify_location, is_excluded_company, title_category
from .models import Board, Job
from .notify import Notifier
from .report import build_report, write_report
from .store import (ROOT, append_matches, load_boards, load_state, now_utc, prune_seen,
                    read_matches, save_state)

log = logging.getLogger("radar")

LIST_CONCURRENCY = {"greenhouse": 8, "lever": 6, "ashby": 6, "smartrecruiters": 4, "workday": 10,
                    "oracle": 8, "workable": 2, "rippling": 6, "bamboohr": 6, "recruitee": 4}
DETAIL_CONCURRENCY = {"greenhouse": 8, "smartrecruiters": 4, "workday": 8, "oracle": 6, "workable": 4,
                      "rippling": 6, "bamboohr": 6}
TIME_BUDGET_SECONDS = 17 * 60
THROTTLE_LIMIT = 6
REPORT_URL = "https://github.com/chandrakodali/job-radar/blob/main/REPORT.md"


def _dupe_key(company: str, title: str, location: str) -> str:
    norm = lambda x: " ".join((x or "").lower().split())
    return f"{norm(company)}|{norm(title)}|{norm(location)}"


def load_config() -> dict:
    with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def workday_slot(now: datetime, shards: int) -> int:
    return ((now.hour * 2) + (now.minute // 30)) % max(1, shards)


def workday_shard_of(board: Board, shards: int) -> int:
    # stable across runs and Python processes (no hash randomization)
    return sum(board.board.encode("utf-8")) % max(1, shards)


def resolve_auto_boards(boards: list[Board], state: dict, today: str) -> list[Board]:
    out = []
    for b in boards:
        if b.ats != "auto":
            out.append(b)
            continue
        rec = state["resolved"].get(b.board)
        stale = not rec or (not rec.get("ats") and rec.get("checked", "") <= (date.fromisoformat(today) - timedelta(days=7)).isoformat())
        if stale:
            found = ATS.resolve_auto(b.board)
            rec = {"ats": found[0] if found else None, "checked": today}
            state["resolved"][b.board] = rec
            log.info("auto-resolve %s -> %s", b.board, rec["ats"] or "not found")
        if rec.get("ats"):
            out.append(Board(ats=rec["ats"], board=b.board, company=b.company,
                             priority=b.priority, cap_exempt=b.cap_exempt))
    return out


def board_disabled(state: dict, b: Board, today: str) -> bool:
    f = state["fails"].get(b.key)
    return bool(f and f.get("disabled_until") and f["disabled_until"] > today)


def record_failure(state: dict, b: Board, err: FetchError, today: str) -> None:
    f = state["fails"].get(b.key) or {"n": 0, "since": today}
    f["n"] = int(f.get("n", 0)) + 1
    f["err"] = str(err)[:80]
    if err.status in (403, 404, 410, 422) and f["n"] >= 3:
        f["disabled_until"] = (date.fromisoformat(today) + timedelta(days=7)).isoformat()
    state["fails"][b.key] = f


def run(args) -> int:
    t0 = time.monotonic()
    cfg = load_config()
    rules = Rules.from_config(cfg)
    tz_name = cfg.get("notify", {}).get("timezone", "America/Chicago")
    tz = ZoneInfo(tz_name)
    now = now_utc()
    today = now.date().isoformat()
    state = load_state()
    bootstrap = not state.get("bootstrapped")
    notifier = Notifier(state, dry_run=args.dry_run or bootstrap)
    state.setdefault("baselined", {})
    if bootstrap:
        log.info("First run: building the baseline quietly (no per-job alerts)")

    boards = [b for b in load_boards() if not is_excluded_company(b, rules)]
    boards = resolve_auto_boards(boards, state, today)
    if args.only:
        boards = [b for b in boards if b.ats in args.only]
    total_boards = len(boards)

    wd_cfg = cfg.get("workday", {})
    shards = int(wd_cfg.get("shards", 4))
    slot = workday_slot(now, shards)
    selected = []
    for b in boards:
        if board_disabled(state, b, today):
            continue
        if b.ats == "workday" and not args.all_shards and workday_shard_of(b, shards) != slot:
            continue
        selected.append(b)
    if args.limit:
        per = Counter()
        lim = []
        for b in selected:
            if per[b.ats] < args.limit:
                lim.append(b)
                per[b.ats] += 1
        selected = lim
    log.info("Boards: %d total, %d this run (workday slot %d/%d). Notifications: %s",
             total_boards, len(selected), slot, shards, notifier.describe())

    sems = {k: threading.Semaphore(v) for k, v in LIST_CONCURRENCY.items()}
    dsems = {k: threading.Semaphore(v) for k, v in DETAIL_CONCURRENCY.items()}
    stats = Counter()
    errors = Counter()
    deadline = t0 + TIME_BUDGET_SECONDS

    throttled = Counter()  # 429s per ATS this run; past the limit we stop asking that ATS

    def fetch_board(b: Board):
        if time.monotonic() > deadline:
            return b, None, "skipped (time budget)"
        if throttled[b.ats] >= THROTTLE_LIMIT:
            return b, None, "skipped (rate limited)"
        with sems[b.ats]:
            if throttled[b.ats] >= THROTTLE_LIMIT:
                return b, None, "skipped (rate limited)"
            try:
                if b.ats == "workday":
                    jobs = ATS.workday_list(b, max_pages=int(wd_cfg.get("max_pages", 5)))
                else:
                    jobs = ATS.LISTERS[b.ats](b)
                return b, jobs, None
            except FetchError as e:
                if e.status == 429:
                    throttled[b.ats] += 1
                    return b, None, "skipped (rate limited)"
                return b, None, e
            except Exception as e:  # never let one odd board stop the run
                return b, None, FetchError(f"{type(e).__name__}: {e}"[:80])

    candidates: list[tuple[Job, Board, str, bool]] = []
    newly_baselined: set[str] = set()
    pool = ThreadPoolExecutor(max_workers=40)
    futures = {pool.submit(fetch_board, b): b for b in selected}
    done_n, last_log = 0, time.monotonic()
    try:
        for fut in as_completed(futures, timeout=max(30.0, deadline + 90 - time.monotonic())):
            done_n += 1
            if time.monotonic() - last_log > 60:
                last_log = time.monotonic()
                log.info("progress: %d/%d boards done, %d jobs scanned", done_n, len(futures), stats["jobs_scanned"])
            b, jobs, err = fut.result()
            if err is not None:
                if isinstance(err, FetchError):
                    record_failure(state, b, err, today)
                    errors[b.ats] += 1
                elif "rate limited" in err:
                    stats["skipped_throttled"] += 1
                else:
                    stats["skipped_time"] += 1
                continue
            stats[f"ok_{b.ats}"] += 1
            state["fails"].pop(b.key, None)
            first_time = b.key not in state["baselined"]
            if first_time:
                newly_baselined.add(b.key)
            for j in jobs:
                stats["jobs_scanned"] += 1
                cat = title_category(j.title, rules)
                if not cat or j.uid in state["seen"]:
                    continue
                if j.posted_at and (now - j.posted_at).days > rules.max_post_age_days:
                    continue
                if classify_location(j.location, j.countries) == "non_us":
                    continue
                candidates.append((j, b, cat, first_time))
    except FuturesTimeout:
        stuck = Counter(b.ats for f, b in futures.items() if not f.done())
        stats["skipped_time"] += sum(stuck.values())
        log.warning("Stopped waiting for %d slow boards: %s", sum(stuck.values()), dict(stuck))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    log.info("Scanned %d jobs; %d new title matches to check", stats["jobs_scanned"], len(candidates))

    def detail(item):
        j = item[0]
        if j.needs_detail and j.ats in ATS.DETAILERS and time.monotonic() < deadline + 120:
            with dsems[j.ats]:
                try:
                    ATS.DETAILERS[j.ats](j)
                except Exception as e:  # keep going; assess with what we have
                    log.debug("detail failed for %s: %s", j.uid, e)
                    stats["detail_failed"] += 1
        return item

    dpool = ThreadPoolExecutor(max_workers=24)
    dfutures = [dpool.submit(detail, c) for c in candidates]
    try:
        for _ in as_completed(dfutures, timeout=max(30.0, deadline + 240 - time.monotonic())):
            pass
    except FuturesTimeout:
        log.warning("Stopped waiting for %d slow job-detail requests", sum(1 for f in dfutures if not f.done()))
    finally:
        dpool.shutdown(wait=False, cancel_futures=True)
    # unfinished details: assess with what the listing gave
    detailed = [f.result() if f.done() and not f.cancelled() else c for f, c in zip(dfutures, candidates)]

    # Same role re-posted (new id) or listed on two boards: alert only once.
    recent_keys = {_dupe_key(r["company"], r["title"], r["location"])
                   for r in read_matches(since=now - timedelta(days=14))}

    new_rows, alerts = [], []
    for j, b, cat, first_time in detailed:
        a = assess(j, b, rules, cat, now=now)
        state["seen"][j.uid] = today
        if a.location_class == "non_us" or a.status == "old":
            continue
        age_hours = (now - j.posted_at).total_seconds() / 3600 if j.posted_at else None
        dkey = _dupe_key(j.company, j.title, j.location)
        is_dupe = dkey in recent_keys
        recent_keys.add(dkey)
        status = a.status
        if status == "alert" and is_dupe:
            status = "quiet"
            a.flags.append("repost")
        # A board seen for the first time: record its jobs quietly unless posted in the last 6 hours.
        quiet_baseline = first_time and not (age_hours is not None and age_hours <= 6)
        row = {
            "first_seen": now.isoformat(), "posted": j.posted_at.isoformat() if j.posted_at else "",
            "score": a.score, "status": status, "category": cat, "company": j.company,
            "title": j.title, "location": j.location, "workplace": j.workplace,
            "flags": "; ".join(a.flags), "url": j.url, "ats": j.ats, "uid": j.uid,
        }
        new_rows.append(row)
        stats[f"status_{status}"] += 1
        if status == "alert" and not quiet_baseline:
            alerts.append({**row, "flags": a.flags, "age_hours": age_hours})

    for k in newly_baselined:
        state["baselined"][k] = today
    append_matches(new_rows)

    # ---- notifications
    alerts.sort(key=lambda m: -m["score"])
    max_alerts = int(cfg.get("scoring", {}).get("max_alerts_per_run", 15))
    if not bootstrap and notifier.configured:
        for m in alerts[:max_alerts]:
            notifier.send_job(m)
        if len(alerts) > max_alerts:
            notifier.send_text(f"{len(alerts) - max_alerts} more new matches are in the report.", click=REPORT_URL)

    local_now = now.astimezone(tz)
    digest_hour = int(cfg.get("notify", {}).get("daily_digest_hour_local", 8))
    local_today = local_now.date().isoformat()
    channels = notifier.describe()
    if notifier.configured and not args.dry_run and state.get("channels") != channels:
        week = [r for r in read_matches(since=now - timedelta(days=7)) if r["status"] in ("alert", "quiet")]
        live = Notifier(state, dry_run=False)
        live.send_text(
            f"Job Radar is connected ({channels}). Watching {total_boards:,} job boards. "
            f"{len(week)} matching US jobs are in this week's report. New strong matches will reach you "
            "here within about 30 minutes of posting, plus a daily digest.",
            click=REPORT_URL,
        )
        state["channels"] = channels
        state["last_digest"] = local_today
    elif bootstrap:
        state["last_digest"] = local_today
    elif state.get("last_digest") != local_today and local_now.hour >= digest_hour:
        day_rows = [r for r in read_matches(since=now - timedelta(hours=24)) if r["status"] in ("alert", "quiet")]
        day_rows.sort(key=lambda r: -r["score"])
        top = "\n".join(f"• {r['title']} — {r['company']} ({r['score']})" for r in day_rows[:5])
        failing = sum(1 for f in state["fails"].values() if not f.get("disabled_until"))
        msg = (f"Daily digest: {len(day_rows)} new matching jobs in the last 24h"
               + (f". Top picks:\n{top}" if top else ".")
               + f"\nRadar health: {total_boards:,} boards, {failing} with temporary errors.")
        if notifier.configured and not args.dry_run:
            notifier.send_text(msg, click=REPORT_URL)
        else:
            log.info("digest: %s", msg.replace("\n", " | "))
        state["last_digest"] = local_today

    pruned = prune_seen(state)
    state["bootstrapped"] = True
    if not args.no_write:
        save_state(state)
        write_report(build_report(tz_name, total_boards, now=now))

    elapsed = time.monotonic() - t0
    ok = {k[3:]: v for k, v in stats.items() if k.startswith("ok_")}
    summary = (
        f"Run finished in {elapsed:.0f}s. Boards OK: {dict(ok)}; errors: {dict(errors)}. "
        f"Jobs scanned: {stats['jobs_scanned']}. New matches: {len(new_rows)} "
        f"(alert {stats['status_alert']}, quiet {stats['status_quiet']}, blocked {stats['status_blocked']}). "
        f"Alerts sent: {notifier.sent}. Details failed: {stats['detail_failed']}. Pruned: {pruned}. "
        f"Skipped for time: {stats['skipped_time']}. Skipped (rate limited): {stats['skipped_throttled']}"
        + (f" {dict(throttled)}" if throttled else "") + "."
    )
    log.info(summary)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(f"### Job Radar run\n\n{summary}\n\n")
            if new_rows:
                f.write("| Score | Status | Role | Company | Location |\n|---:|---|---|---|---|\n")
                for r in sorted(new_rows, key=lambda r: -r["score"])[:40]:
                    f.write(f"| {r['score']} | {r['status']} | [{r['title']}]({r['url']}) | {r['company']} | {r['location']} |\n")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Job Radar")
    p.add_argument("--dry-run", action="store_true", help="don't send notifications")
    p.add_argument("--no-write", action="store_true", help="don't write state or report")
    p.add_argument("--only", nargs="*", help="only these ATS types")
    p.add_argument("--limit", type=int, default=0, help="max boards per ATS (testing)")
    p.add_argument("--all-shards", action="store_true", help="check every Workday board this run")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    rc = run(args)
    # A worker stuck on a slow server must not keep the process (and the Actions job) alive.
    if any(t.is_alive() and not t.daemon for t in threading.enumerate() if t is not threading.main_thread()):
        log.warning("Exiting with slow requests still open")
        sys.stdout.flush()
        os._exit(rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
