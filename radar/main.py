"""Run one radar pass: fetch boards, find new matching jobs, alert, update report and state.

Usage:
    python -m radar.main                 # normal run (used by GitHub Actions)
    python -m radar.main --dry-run       # no notifications
    python -m radar.main --only greenhouse --limit 20
    python -m radar.main --profile automotive   # another search with its own rules and files (radar/profile.py)
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import os
import re
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
from .match import (_any, Rules, assess, classify_location, has_evidence, is_excluded_company, job_category,
                    row_still_fits, title_category)
from . import deep
from .models import Board, Job
from .notify import Notifier
from .dashboard import build_dashboard, write_dashboard
from .report import build_newest_report, build_report, write_report
from . import ats_more, robots
from .ai import Reviewer
from . import ai_fit
from . import rank as _rank
from . import sources as _sources
from . import store as _store
from .store import (ROOT, append_matches, append_rejected, load_boards, load_discovered, load_focus_boards,
                    load_state, now_utc, prune_seen, read_matches, read_rejected, remove_rejected, save_state,
                    update_matches)

log = logging.getLogger("radar")

LIST_CONCURRENCY = {"greenhouse": 8, "lever": 6, "ashby": 6, "smartrecruiters": 4, "workday": 10,
                    "oracle": 8, "workable": 2, "rippling": 6, "bamboohr": 6, "recruitee": 4}
DETAIL_CONCURRENCY = {"greenhouse": 8, "smartrecruiters": 4, "workday": 8, "oracle": 6, "workable": 4,
                      "rippling": 6, "bamboohr": 6}
for _ats, _n in ats_more.CONCURRENCY.items():
    LIST_CONCURRENCY.setdefault(_ats, _n)
    DETAIL_CONCURRENCY.setdefault(_ats, _n)
DETAIL_CONCURRENCY.setdefault("adzuna", 3)     # Adzuna links lead to the original posting (read via JSON-LD)
DETAIL_CONCURRENCY.setdefault("email", 1)
ATS.DETAILERS.setdefault("adzuna", ats_more._ld_detail)
TIME_BUDGET_SECONDS = 17 * 60
THROTTLE_LIMIT = 6
REPORT_URL = "https://github.com/chandrakodali/job-radar/blob/main/REPORT.md"
CONFIG_PATH = os.path.join(ROOT, "config.yaml")
FOCUS_PATH = ""   # a profile's own employer list; set by --profile
# Job systems whose listing is every open job: a saved match missing from it has been taken down.
COMPLETE_LISTINGS = {"greenhouse", "lever", "ashby"}
MAX_TEXT_RETRIES = 3


def _dupe_key(company: str, title: str, location: str) -> str:
    norm = lambda x: " ".join((x or "").lower().split())
    return f"{norm(company)}|{norm(title)}|{norm(location)}"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
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
            out.append(dataclasses.replace(b, ats=rec["ats"]))
    return out


def board_disabled(state: dict, b: Board, today: str) -> bool:
    f = state["fails"].get(b.key)
    return bool(f and f.get("disabled_until") and f["disabled_until"] > today)


def record_failure(state: dict, b: Board, err: FetchError, today: str) -> None:
    f = state["fails"].get(b.key) or {"n": 0, "since": today}
    f["n"] = int(f.get("n", 0)) + 1
    f["err"] = str(err)[:80]
    if err.status in (202, 403, 404, 410, 422, 999) and f["n"] >= 3:
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
    prof = cfg.get("profile") or {}
    label = prof.get("label", "Job Radar")
    report_url = prof.get("report_url", REPORT_URL)
    budget = int(cfg.get("run", {}).get("time_budget_minutes", TIME_BUDGET_SECONDS // 60)) * 60
    state = load_state()
    # Rule-based fit score from the full job text (profiles with a skills.yaml). Job texts are kept so every
    # saved match can be scored again whenever the rules change.
    texts = _rank.TextStore(os.path.join(_store.DATA, "texts"),
                            keep_days=int((cfg.get("ranking") or {}).get("keep_text_days", 150)))
    if os.path.exists(os.path.join(os.path.dirname(CONFIG_PATH), (cfg.get("ranking") or {}).get("taxonomy", "skills.yaml"))):
        texts.load()
        rules.ranker = _rank.Ranker.load(cfg, os.path.dirname(CONFIG_PATH), texts.texts.values())
    bootstrap = not state.get("bootstrapped")
    notifier = Notifier(state, dry_run=args.dry_run or bootstrap)
    state.setdefault("baselined", {})
    if bootstrap:
        log.info("First run: building the baseline quietly (no per-job alerts)")

    general = bool(cfg.get("boards", {}).get("include_general", True))
    boards = [b for b in (load_boards() if general else []) if not is_excluded_company(b, rules)]
    focus = [b for b in load_focus_boards(FOCUS_PATH) if not is_excluded_company(b, rules)]
    focus_boards_map = {b.key.lower(): b for b in focus}
    if focus:  # the profile's own employers come first and replace the same board in the general list
        focus_keys = {b.key.lower() for b in focus}
        boards = focus + [b for b in boards if b.key.lower() not in focus_keys]
    boards = resolve_auto_boards(boards, state, today)
    if args.only:
        boards = [b for b in boards if b.ats in args.only]
    known_keys = {b.key.lower() for b in boards}
    found = [b for b in (load_discovered(known_keys) + load_discovered(known_keys, _store.CAREERS_BOARDS_PATH, named=True)
                         if general else [])
             if not is_excluded_company(b, rules)]
    if args.only:
        found = [b for b in found if b.ats in args.only]
    total_boards = len(boards) + len(found)

    # Keyword searches of the focus boards' full job text. A full sweep on the first run (or on request);
    # otherwise a rotating slice of the keywords, so every keyword is searched every few runs.
    search_cfg = cfg.get("search", {})
    core_terms = [str(t) for t in search_cfg.get("terms_core") or []]      # searched every run
    all_terms = [str(t) for t in search_cfg.get("terms") or [] if str(t) not in core_terms]
    per_run = int(search_cfg.get("terms_per_run", 0)) or len(all_terms)
    if all_terms and not (bootstrap or args.full_sweep) and per_run < len(all_terms):
        start = int(state.get("term_cursor", 0)) % len(all_terms)
        terms = core_terms + (all_terms + all_terms)[start:start + per_run]
        state["term_cursor"] = (start + per_run) % len(all_terms)
    else:
        terms = core_terms + all_terms
    all_terms = core_terms + all_terms

    wd_cfg = cfg.get("workday", {})
    shards = int(wd_cfg.get("shards", 4))
    slot = workday_slot(now, shards)
    selected = []
    for b in boards:
        if board_disabled(state, b, today):
            continue
        if b.ats == "workday" and not b.focus and not args.all_shards and workday_shard_of(b, shards) != slot:
            continue
        selected.append(b)
    # Discovered boards: a rotating slice each run, so all of them are checked every few hours.
    active_found = sorted((b for b in found if not board_disabled(state, b, today)), key=lambda b: b.key.lower())
    per_run = int(cfg.get("discovery", {}).get("boards_per_run", 1500))
    if active_found:
        start = int(state.get("found_cursor", 0)) % len(active_found)
        take = active_found[start:start + per_run]
        if len(take) < per_run:
            take += active_found[:min(per_run - len(take), start)]
        selected += take
        state["found_cursor"] = (start + len(take)) % len(active_found)
    if args.limit:
        per = Counter()
        lim = []
        for b in selected:
            if per[b.ats] < args.limit:
                lim.append(b)
                per[b.ats] += 1
        selected = lim
    log.info("Boards: %d total (%d discovered), %d this run (workday slot %d/%d). Notifications: %s",
             total_boards, len(found), len(selected), slot, shards, notifier.describe())
    if focus:
        log.info("Focus boards: %d, each searched for %d of %d keywords this run",
                 sum(1 for b in selected if b.focus), len(terms), len(all_terms))

    # AI check against the resume (profiles with `ai: mode: resume`): saved matches without an AI verdict
    # are checked when their board lists them again, so the whole history gets checked over a few runs.
    ai_cfg = cfg.get("ai", {}) or {}
    fit_mode = ai_cfg.get("mode") == "resume"
    ai_off = str(ai_cfg.get("mode", "")).lower() in ("off", "false", "none")
    fit = ai_fit.FitReviewer(ai_cfg) if fit_mode else None
    # Saved matches still missing their job text (ranker) or an AI verdict: read again when listed again.
    pending: dict[str, dict] = {}
    want_ai = fit is not None and fit.cfg_ok
    if rules.ranker is not None or want_ai:
        closed_before = state.get("closed", {})
        for r in read_matches(since=now - timedelta(days=int(prof.get("report_days", 60)))):
            if r["status"] not in ("alert", "quiet") or r["uid"] in closed_before:
                continue
            if (rules.ranker is not None and not texts.get(r["uid"])) or (want_ai and not r.get("ai")):
                pending[r["uid"]] = r
    ai_backlog: list[tuple] = []
    backlog_seen: set[str] = set()

    robot = robots.Robots(state.setdefault("robots", {}), today)
    robots.install(robot)
    sems = {k: threading.Semaphore(v) for k, v in LIST_CONCURRENCY.items()}
    dsems = {k: threading.Semaphore(v) for k, v in DETAIL_CONCURRENCY.items()}
    stats = Counter()
    errors = Counter()
    deadline = t0 + budget

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
                if b.focus:
                    jobs = deep.deep_list(b, terms, search_cfg, stats)
                elif b.ats == "workday":
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
    src_cfg = cfg.get("sources") or {}
    missed_companies: Counter = Counter()   # employers other sources list whose own board we don't read
    want_sources = bool(src_cfg)
    scanned_keys: set[str] = set()   # company + title of every job the boards list (to skip the same job from alerts)
    coverage: dict[str, dict] = {}   # focus board -> jobs it listed this run (or its error)
    live: dict[str, set[str]] = {}   # board key -> every job it lists right now (complete listings only)
    track_closed = bool(cfg.get("report", {}).get("track_closed"))
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
                if b.focus:
                    coverage[b.key] = {"listed": 0, "error": str(err)[:80]}
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
            if track_closed and jobs and (b.ats in COMPLETE_LISTINGS or getattr(jobs, "complete", False)):
                live[b.key] = {j.uid for j in jobs}
            if b.focus:
                coverage[b.key] = {"listed": len(jobs), "complete": bool(getattr(jobs, "complete", False))}
            for j in jobs:
                stats["jobs_scanned"] += 1
                if want_sources:
                    scanned_keys.add(_sources.norm_key(j.company, j.title))
                if j.uid in state["seen"]:
                    if j.uid in pending and j.uid not in backlog_seen:
                        backlog_seen.add(j.uid)
                        ai_backlog.append((j, b, "_ai", False))
                    continue
                cat = job_category(j, b, rules)
                if not cat:
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
    # Jobs from job-alert emails and Adzuna: kept only when the employer's own board did not list them.
    if want_sources:
        extra: list[Job] = []
        if src_cfg.get("email_alerts", True):
            got = _sources.read_alert_emails(state, days=int(src_cfg.get("email_days", 4)))
            stats["source_email"] = len(got)
            extra += got
        if src_cfg.get("adzuna"):
            got = _sources.adzuna_jobs(state, src_cfg["adzuna"], now, stats=stats)
            stats["source_adzuna"] = len(got)
            extra += got
        more = [("workable_search", lambda c: _sources.workable_search_jobs(c, now, stats=stats)),
                ("hn", lambda c: _sources.hn_jobs(c, lambda t: title_category(t, rules) in ("core", "bridge"), now)),
                ("remoteok", lambda c: _sources.remoteok_jobs(c, now)),
                ("wwr", lambda c: _sources.wwr_jobs(c, now)),
                ("usajobs", lambda c: _sources.usajobs_jobs(c, stats=stats))]
        last_read = state.setdefault("source_last", {})
        for name, fetch in more:
            c = src_cfg.get(name)
            if not c or time.monotonic() > deadline + 60:
                continue
            every = int(c.get("every_minutes", 0)) if isinstance(c, dict) else 0
            if every and last_read.get(name) and \
                    now - datetime.fromisoformat(last_read[name]) < timedelta(minutes=every - 5):
                continue
            last_read[name] = now.isoformat()
            if True:
                try:
                    got = fetch(src_cfg[name])
                except Exception as e:  # one odd source must not stop the run
                    log.warning("Source %s failed: %s", name, str(e)[:120])
                    got = []
                stats[f"source_{name}"] = len(got)
                extra += got
        saved_keys = {_sources.norm_key(r["company"], r["title"]) for r in read_matches(since=now - timedelta(days=60))}
        taken = set()
        for j in extra:
            k = _sources.norm_key(j.company, j.title)
            relevant = title_category(j.title, rules) in ("core", "bridge") and \
                classify_location(j.location, j.countries) != "non_us"
            if relevant:        # how many of the jobs other sources list the employer boards already had
                stats["recall_total"] += 1
                if k in scanned_keys:
                    stats["recall_on_boards"] += 1
                else:
                    missed_companies[j.company] += 1
            if k in scanned_keys or k in saved_keys or k in taken:
                stats["source_dupes"] += 1
                continue
            taken.add(k)
            if j.uid in state["seen"] or is_excluded_company(Board(j.ats, j.board, j.company), rules):
                continue
            cat = title_category(j.title, rules)
            # no job text in an alert email: only clear target titles; Adzuna's snippet may show the tools
            if cat == "wide" and not (j.description and has_evidence(f"{j.title}\n{j.description}", rules)):
                cat = None
            if not cat or classify_location(j.location, j.countries) == "non_us":
                continue
            if j.posted_at and (now - j.posted_at).days > rules.max_post_age_days:
                continue
            candidates.append((j, _sources.source_board(j), cat, False))
            stats["source_candidates"] += 1

    # Generic titles need their description read before we know if they fit. Cap how many we read per
    # run; the rest stay unseen and are picked up by the next runs (focus boards and search hits first).
    wide_cap = int(cfg.get("scoring", {}).get("max_wide_per_run", 0))
    if wide_cap:
        wide = [c for c in candidates if c[2] == "wide" and not c[0].description]
        if len(wide) > wide_cap:
            wide.sort(key=lambda c: (not c[1].focus, not c[0].found_by,
                                     classify_location(c[0].location, c[0].countries) != "us"))
            later = {id(c[0]) for c in wide[wide_cap:]}
            candidates = [c for c in candidates if id(c[0]) not in later]
            stats["deferred"] = len(later)
    if rules.require_entry:   # entry-level titles and the newest jobs get their text read first
        candidates.sort(key=lambda c: (not _any(rules.entry, " " + c[0].title + " "),
                                       -(c[0].posted_at.timestamp() if c[0].posted_at else 0)))
    log.info("Scanned %d jobs; %d new title matches to check%s", stats["jobs_scanned"], len(candidates),
             f" ({stats['deferred']} more left for the next runs)" if stats["deferred"] else "")

    def detail(item):
        j = item[0]
        if j.needs_detail and j.ats in ATS.DETAILERS and time.monotonic() < deadline + 120:
            with dsems[j.ats]:
                j.detail_tried = True
                try:
                    ATS.DETAILERS[j.ats](j)
                except Exception as e:  # keep going; assess with what we have
                    log.debug("detail failed for %s: %s", j.uid, e)
                    stats["detail_failed"] += 1
        return item

    # Saved matches waiting for the AI check: best first, as many as one run can check.
    ai_backlog.sort(key=lambda c: -int(pending[c[0].uid]["score"]))
    ai_backlog = ai_backlog[:int((cfg.get("ranking") or {}).get("backlog_per_run", 0)
                                 or ai_cfg.get("backlog_per_run", 400))]
    candidates = candidates + ai_backlog
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
    ai_back = [c for c in detailed if c[2] == "_ai"]
    for j, _b, _c, _f in ai_back:
        if j.description:
            texts.put(j.uid, j.description)
            stats["texts_backfilled"] += 1
    detailed = [c for c in detailed if c[2] != "_ai"]
    candidates = [c for c in candidates if c[2] != "_ai"]

    # When the title rules change, move saved matches that no longer fit out of the CSV (kept in rejected.csv).
    rules_sig = hashlib.sha1(json.dumps(cfg.get("titles", {}), sort_keys=True).encode()).hexdigest()[:12]
    if state.get("title_rules") != rules_sig and not args.no_write:
        old = read_matches()
        gone = [r for r in old if not row_still_fits(r, rules)
                or (r.get("ats") == "sfrmk" and classify_location(*ats_more.rmk_location(r["location"])) == "non_us")]
        if gone:
            update_matches({}, {r["uid"] for r in gone})
            append_rejected([{**r, "reject_reason": "rules: role or location no longer in the target list"} for r in gone])
            log.info("Title rules changed: moved %d saved matches that no longer fit to rejected.csv", len(gone))
        state["title_rules"] = rules_sig

    # Same role re-posted (new id) or listed on two boards: alert only once.
    recent_keys = {_dupe_key(r["company"], r["title"], r["location"])
                   for r in read_matches(since=now - timedelta(days=14))}

    new_rows, rejected_rows, queued = [], [], []
    retries = state.setdefault("text_retries", {}) if rules.skills else {}
    for j, b, cat, first_time in detailed:
        if getattr(j, "title_guess", False):  # the listing only had a URL slug; judge the real title
            cat = job_category(j, b, rules)
            if not cat:
                state["seen"][j.uid] = today
                continue
        if rules.skills:
            # Fit is judged on the job text. If it could not be read this run, try again next run
            # (a few times) instead of deciding without it.
            if not j.description and j.needs_detail and j.ats in ATS.DETAILERS:
                if not j.detail_tried:           # the run ended before its turn came: not a failed read
                    stats["retry_later"] += 1
                    continue
                if retries.get(j.uid, 0) < MAX_TEXT_RETRIES:
                    retries[j.uid] = retries.get(j.uid, 0) + 1
                    stats["retry_later"] += 1
                    continue
            retries.pop(j.uid, None)
            if cat == "wide" and not has_evidence(f"{j.title}\n{j.description}", rules):
                # No evidence in the text: dropped, unless the text could not be read at all and the
                # title is one of the likely ones. Those are listed (marked "description not read").
                if j.description or title_category(j.title, rules) != "wide":
                    state["seen"][j.uid] = today
                    stats["wide_dropped"] += 1
                    continue
                stats["unread_kept"] += 1
        if rules.require_entry and not j.description and j.needs_detail and j.ats in ATS.DETAILERS \
                and not j.detail_tried:
            stats["retry_later"] += 1      # the run ended before its text was read: judge it next run
            continue
        a = assess(j, b, rules, cat, now=now)
        state["seen"][j.uid] = today
        if a.location_class == "non_us" or a.status == "old":
            continue
        if rules.ranker is not None and j.description:
            texts.put(j.uid, j.description)
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
            "has": ", ".join(a.has), "missing": ", ".join(a.missing), "detail": a.detail,
        }
        if status == "blocked":
            rejected_rows.append({**row, "reject_reason": "rules: " + "; ".join(a.flags)})
            stats["status_blocked"] += 1
            continue
        queued.append({"row": row, "desc": (j.description or "")[:12000 if fit_mode else 1800], "may_alert": not quiet_baseline and not is_dupe,
                       "q": now.isoformat()})

    # ---- AI review (when a key is configured): only kept jobs reach the CSV and the phone
    ai_cfg = cfg.get("ai", {})
    reviewer = Reviewer(max_requests=int(ai_cfg.get("max_requests_per_run", 12)), system=ai_cfg.get("system"),
                        roles=ai_cfg.get("roles"), keep_senior=bool(ai_cfg.get("keep_senior")),
                        max_min_years=int(ai_cfg.get("max_min_years", 5)))
    if ai_off:
        reviewer.cfg = None     # the profile switched AI off (keys may still be set for other uses)
    alerts = []
    threshold = rules.alert_threshold

    weight = float(ai_cfg.get("score_weight", 0.6))

    def ai_status(score: int, flags: str) -> str:
        # An AI-checked match alerts on its blended score, except reposts and ones asking for 9+ years.
        if score >= threshold and "repost" not in flags and not re.search(r"\b(9|[1-9]\d)\+ yrs", flags):
            return "alert"
        return "quiet"

    def keep_row(item, verdict=None):
        row = dict(item["row"])
        if verdict and fit_mode:
            row.update(ai_fit.row_fields(verdict))
            row["score"] = ai_fit.blend(row["score"], verdict["score"], weight)
            row["status"] = ai_status(row["score"], row["flags"])
        elif verdict:
            row.update({"ai": "keep", "ai_score": verdict["score"], "ai_role": verdict["role"],
                        "ai_level": verdict["level"], "ai_sponsor": verdict["sponsor"], "ai_why": verdict["why"]})
            row["score"] = round((int(row["score"]) + verdict["score"]) / 2)
            row["status"] = "alert" if row["score"] >= threshold and row["status"] == "alert" else "quiet"
            if verdict["sponsor"] == "yes" and "sponsors visa" not in row["flags"]:
                row["flags"] = "; ".join(x for x in [row["flags"], "AI: sponsors visa"] if x)
        new_rows.append(row)
        stats[f"status_{row['status']}"] += 1
        if row["status"] == "alert" and item["may_alert"]:
            age = None
            if row["posted"]:
                age = (now - datetime.fromisoformat(row["posted"])).total_seconds() / 3600
            alerts.append({**row, "flags": [f for f in row["flags"].split("; ") if f], "age_hours": age})

    if fit_mode:
        new_items = [{"uid": q["row"]["uid"], "title": q["row"]["title"], "company": q["row"]["company"],
                      "location": q["row"]["location"], "description": q["desc"]} for q in queued if q["desc"]]
        back_items = [{"uid": j.uid, "title": j.title, "company": j.company, "location": j.location,
                       "description": j.description} for j, _b, _c, _f in ai_back if j.description]
        secs = float(ai_cfg.get("time_budget_minutes", 15)) * 60
        verdicts = fit.check_many(new_items + back_items, secs) if fit.cfg_ok else {}

        def reject_reason(v):
            return "AI: " + v["reject_reason"] + (f" ({v['why']})" if v["why"] else "")

        for q in queued:
            v = verdicts.get(q["row"]["uid"])
            if v is None or v["keep"]:
                keep_row(q, v)   # not checked yet: saved now, checked on a later run
            else:
                rejected_rows.append({**q["row"], **ai_fit.row_fields(v), "reject_reason": reject_reason(v)})
                stats["ai_rejected"] += 1
        ups, drop = {}, set()
        for it in back_items:
            v = verdicts.get(it["uid"])
            if v is None:
                continue
            row = pending[it["uid"]]
            fields = ai_fit.row_fields(v)
            if v["keep"]:
                fields["score"] = ai_fit.blend(row["score"], v["score"], weight)
                fields["status"] = ai_status(fields["score"], row.get("flags", ""))
                ups[it["uid"]] = fields
            else:
                drop.add(it["uid"])
                clean_row = {k: val for k, val in row.items() if not k.startswith("_")}
                rejected_rows.append({**clean_row, **fields, "reject_reason": reject_reason(v)})
                stats["ai_rejected"] += 1
        if not args.no_write:
            update_matches(ups, drop)
        stats["ai_reviewed"] = len(verdicts)
        stats["ai_backfilled"] = len(ups) + len(drop)
        stats["ai_waiting"] = max(0, len(pending) - stats["ai_backfilled"]) + sum(
            1 for q in queued if q["row"]["uid"] not in verdicts)
    elif reviewer.cfg:
        queue = state.get("ai_queue", []) + queued
        items = [{"uid": q["row"]["uid"], "title": q["row"]["title"], "company": q["row"]["company"],
                  "location": q["row"]["location"], "workplace": q["row"]["workplace"], "posted": q["row"]["posted"],
                  "flags": q["row"]["flags"], "description": q["desc"]} for q in queue]
        verdicts = reviewer.review(items)
        left = []
        for q in queue:
            v = verdicts.get(q["row"]["uid"])
            if v and v["keep"]:
                keep_row(q, v)
            elif v:
                rejected_rows.append({**q["row"], "ai": "reject", "ai_score": v["score"], "ai_role": v["role"],
                                      "ai_level": v["level"], "ai_sponsor": v["sponsor"], "ai_why": v["why"],
                                      "reject_reason": "AI: " + (v["reject_reason"] or v["why"])})
                stats["ai_rejected"] += 1
            elif now - datetime.fromisoformat(q["q"]) > timedelta(hours=36):
                keep_row(q)  # waited too long for the AI: save it unchecked rather than lose it
            else:
                left.append(q)
        state["ai_queue"] = left[-400:]
        stats["ai_reviewed"] = len(verdicts)
        # Spare AI budget: review recent saved matches that have no AI verdict yet (title-level check).
        if reviewer.available:
            backlog = [r for r in read_matches(since=now - timedelta(days=10)) if not r.get("ai")][:reviewer.batch * 4]
            bv = reviewer.review([{"uid": r["uid"], "title": r["title"], "company": r["company"],
                                   "location": r["location"], "workplace": r["workplace"], "posted": r["posted"],
                                   "flags": r["flags"], "description": ""} for r in backlog])
            ups, drop = {}, set()
            for r in backlog:
                v = bv.get(r["uid"])
                if not v:
                    continue
                fields = {"ai": "keep" if v["keep"] else "reject", "ai_score": v["score"], "ai_role": v["role"],
                          "ai_level": v["level"], "ai_sponsor": v["sponsor"], "ai_why": v["why"]}
                if v["keep"]:
                    ups[r["uid"]] = fields
                else:
                    drop.add(r["uid"])
                    rejected_rows.append({**r, **fields, "reject_reason": "AI: " + (v["reject_reason"] or v["why"])})
            if not args.no_write:
                update_matches(ups, drop)
            stats["ai_backfilled"] = len(bv)
    else:
        for q in queued:
            keep_row(q)

    for k in newly_baselined:
        state["baselined"][k] = today
    if not args.no_write:
        append_matches(new_rows)
        append_rejected(rejected_rows)
        if rules.ranker is not None:
            texts.save(today)
            rescored = rescore_saved(rules, texts, now, focus_boards_map, int(prof.get("report_days", 60)),
                                     blend_ai=weight if fit_mode else None)
            stats.update(rescored)

    # Saved matches that a complete listing no longer shows have been taken down; ones that show up
    # again are reopened. Kept in state so the report and page can leave closed jobs out.
    closed = state.setdefault("closed", {}) if track_closed else {}
    missing_once = state.setdefault("missing_once", {})
    if track_closed and live:
        for r in read_matches(since=now - timedelta(days=rules.max_post_age_days + 30)):
            key = r["uid"].rsplit(":", 1)[0]
            if key not in live:
                continue
            if r["uid"] in live[key]:
                closed.pop(r["uid"], None)
                missing_once.pop(r["uid"], None)
            elif r["uid"] not in closed and r["uid"] not in {x["uid"] for x in new_rows}:
                # Greenhouse/Lever/Ashby list every job in one answer: one miss means taken down. A long
                # paged read (Workday) must miss it twice in a row first.
                if r.get("ats") in COMPLETE_LISTINGS or missing_once.get(r["uid"]):
                    closed[r["uid"]] = today
                    missing_once.pop(r["uid"], None)
                    stats["closed"] += 1
                else:
                    missing_once[r["uid"]] = today

    # ---- notifications
    alerts.sort(key=lambda m: -m["score"])
    max_alerts = int(cfg.get("scoring", {}).get("max_alerts_per_run", 15))
    if not bootstrap and notifier.configured:
        for m in alerts[:max_alerts]:
            notifier.send_job(m)
        if len(alerts) > max_alerts:
            notifier.send_text(f"{len(alerts) - max_alerts} more new matches are in the report.", title=label,
                               click=report_url)

    local_now = now.astimezone(tz)
    digest_hour = int(cfg.get("notify", {}).get("daily_digest_hour_local", 8))
    local_today = local_now.date().isoformat()
    channels = notifier.describe()
    if notifier.configured and not args.dry_run and state.get("channels") != channels:
        week = [r for r in read_matches(since=now - timedelta(days=7)) if r["status"] in ("alert", "quiet")]
        first = Notifier(state, dry_run=False)
        first.send_text(
            f"{label} is connected ({channels}). Watching {total_boards:,} job boards. "
            f"{len(week)} matching US jobs are in this week's report. New strong matches will reach you "
            + ("here within about 30 minutes of posting, plus a daily digest." if not prof
               else "here after each scan, plus a daily digest."),
            title=label, click=report_url,
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
            notifier.send_text(msg, title=label, click=report_url)
        else:
            log.info("digest: %s", msg.replace("\n", " | "))
        state["last_digest"] = local_today

    ai_desc = fit.describe() if fit_mode else reviewer.describe()
    board_warnings = []
    if focus and not args.no_write:
        board_warnings = write_coverage(os.path.join(_store.DATA, "coverage.csv"), focus, coverage, state, now,
                                        int(prof.get("report_days", 60)))
        stats["focus_zero"] = sum(1 for c in coverage.values() if not c.get("listed"))
        stats["focus_full"] = sum(1 for c in coverage.values() if c.get("complete"))
    # Board recall over the last 7 days: of the matching jobs other sources listed, how many the employer
    # boards had too. Employers missed are kept so the board finder can look for their boards.
    report_extra: list[str] = []
    if want_sources:
        rec = state.setdefault("recall", {})
        on, tot = rec.get(today, [0, 0])
        rec[today] = [on + stats["recall_on_boards"], tot + stats["recall_total"]]
        for d in [d for d in rec if d < (now - timedelta(days=7)).date().isoformat()]:
            del rec[d]
        w_on, w_tot = sum(v[0] for v in rec.values()), sum(v[1] for v in rec.values())
        miss = state.setdefault("missed_companies", {})
        for c in missed_companies:
            if c and c.strip():
                miss[c.strip()] = today
        for c in [c for c, d in miss.items() if d < (now - timedelta(days=45)).date().isoformat()]:
            del miss[c]
        if w_tot:
            report_extra = [f"Coverage check: of {w_tot:,} matching jobs that job sites and feeds listed this week, "
                            f"{w_on:,} ({100 * w_on // w_tot}%) were already found on the employers' own boards; "
                            f"the rest are listed from those sources.", ""]
    pruned = prune_seen(state)
    robot.prune()
    state["bootstrapped"] = True
    if not args.no_write:
        save_state(state)
        from .calibrate import handled_uids
        handled = handled_uids(os.path.dirname(CONFIG_PATH)) if FOCUS_PATH else set()
        view = cfg.get("report") or {}
        if view.get("order") == "newest":
            write_report(build_newest_report(tz_name, total_boards, now=now, view=view, closed=set(closed),
                                             extra_lines=report_extra))
        else:
            write_report(build_report(tz_name, total_boards, now=now, profile=prof, closed=set(closed), handled=handled))
        if rules.ranker is not None:      # SKILLS_<PROFILE>.md next to the report
            import radar.report as _report
            current = [r for r in read_matches(since=now - timedelta(days=int(prof.get("report_days", 60))))
                       if r["uid"] not in closed]
            md = _rank.skills_report(rules.ranker, [(r["title"], texts.get(r["uid"])) for r in current], label)
            if md:
                path = os.path.join(os.path.dirname(_report.REPORT_PATH),
                                    os.path.basename(_report.REPORT_PATH).replace("REPORT", "SKILLS", 1))
                with open(path, "w", encoding="utf-8") as f:
                    f.write(md)
        write_dashboard(build_dashboard(tz_name, total_boards, ai_desc, now=now, profile=prof,
                                        closed=set(closed), newest=view.get("order") == "newest"))

    elapsed = time.monotonic() - t0
    ok = {k[3:]: v for k, v in stats.items() if k.startswith("ok_")}
    summary = (
        f"Run finished in {elapsed:.0f}s. Boards OK: {dict(ok)}; errors: {dict(errors)}. "
        f"Jobs scanned: {stats['jobs_scanned']}. New matches: {len(new_rows)} "
        f"(alert {stats['status_alert']}, quiet {stats['status_quiet']}, blocked {stats['status_blocked']}). "
        f"Alerts sent: {notifier.sent}. Details failed: {stats['detail_failed']}. Pruned: {pruned}. "
        f"Skipped for time: {stats['skipped_time']}. Skipped (rate limited): {stats['skipped_throttled']}"
        + (f" {dict(throttled)}" if throttled else "") + ". "
        f"AI review: {ai_desc}; reviewed {stats['ai_reviewed']}, rejected {stats['ai_rejected']}, "
        f"saved matches checked {stats['ai_backfilled']}, "
        f"waiting {stats['ai_waiting'] if fit_mode else len(state.get('ai_queue', []))}."
    )
    if rules.ranker is not None:
        summary += (f" Rule-based fit score: {len(texts.texts)} job texts kept ({stats['texts_backfilled']} added for "
                    f"saved matches); rescored {stats['rescored']}, moved to hidden {stats['rescore_hidden']}, "
                    f"back from hidden {stats['rescore_restored']}, still without text {stats['text_missing']}.")
    if rules.skills:
        summary += (
            f" Keyword searches: {stats['searches']} run ({stats['search_failed']} failed), "
            f"{stats['search_hits']} results. Generic titles dropped for lack of matching tools: "
            f"{stats['wide_dropped']}; left for the next run: {stats['deferred']}; "
            f"text not read yet: {stats['retry_later']}; taken down: {stats['closed']}."
        )
    if want_sources:
        summary += (f" Other sources: {stats['source_email']} jobs from alert emails, {stats['source_adzuna']} from "
                    f"Adzuna ({stats['adzuna_calls']} calls), {stats['source_workable_search']} from Workable search, "
                    f"{stats['source_hn']} from HN, {stats['source_remoteok']} from Remote OK, {stats['source_wwr']} "
                    f"from WWR, {stats['source_usajobs']} from USAJOBS; {stats['source_dupes']} already known, "
                    f"{stats['source_candidates']} new candidates. Board recall: {stats['recall_on_boards']}/"
                    f"{stats['recall_total']} matching jobs from other sources were already on employer boards.")
    if focus:
        summary += (f" Employer boards: {len(coverage)} read, {stats['focus_full']} read completely, "
                    f"{stats['focus_zero']} listed nothing or failed (see coverage.csv).")
    log.info(summary)
    for w in board_warnings:
        log.warning("Board may be broken: %s", w)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(f"### {label} run\n\n{summary}\n\n")
            if board_warnings:
                f.write("**Boards that suddenly list nothing (a reader may be broken):**\n\n"
                        + "".join(f"- {w}\n" for w in board_warnings) + "\n")
            if new_rows:
                f.write("| Score | Status | Role | Company | Location |\n|---:|---|---|---|---|\n")
                for r in sorted(new_rows, key=lambda r: -r["score"])[:40]:
                    f.write(f"| {r['score']} | {r['status']} | [{r['title']}]({r['url']}) | {r['company']} | {r['location']} |\n")
    return 0


def write_coverage(path: str, focus: list[Board], coverage: dict, state: dict, now: datetime,
                   report_days: int) -> list[str]:
    """One row per employer board of the profile: jobs it listed this run, whether every job was read,
    errors, and saved matches. Returns warnings for boards that suddenly list nothing."""
    import csv as _csv
    prev = state.setdefault("coverage_prev", {})
    open_matches = Counter(r["uid"].rsplit(":", 1)[0].lower() for r in read_matches(since=now - timedelta(days=report_days)))
    warnings, rows = [], []
    for b in focus:
        key = b.key.lower()
        c = coverage.get(b.key)
        listed = c.get("listed", 0) if c else None
        before = prev.get(key)
        note = ""
        if c is None:
            note = "not reached this run"
        elif c.get("error"):
            note = "error: " + c["error"]
        if c is not None and listed == 0 and before and before >= 10:
            note = (note + "; " if note else "") + f"lists nothing now (listed {before} before)"
            warnings.append(f"{b.company} ({b.key}): {note}")
        if listed:
            prev[key] = listed
        rows.append({"company": b.company, "ats": b.ats, "board": b.board,
                     "jobs_listed": "" if listed is None else listed,
                     "read_completely": "yes" if c and c.get("complete") else "",
                     "open_matches": open_matches.get(key, 0), "note": note})
    rows.sort(key=lambda r: (r["open_matches"] > 0, r["company"].lower()))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["company"])
        w.writeheader()
        w.writerows(rows)
    return warnings


def _row_job(r: dict, text: str) -> tuple[Job, str]:
    """A saved match as a Job again (for rescoring). The board id sits between the job system and the job id."""
    uid, ats = r["uid"], r.get("ats") or r["uid"].split(":", 1)[0]
    board = uid[len(ats) + 1: uid.rfind(":")]
    posted = None
    if r.get("posted"):
        try:
            posted = datetime.fromisoformat(r["posted"])
        except ValueError:
            posted = None
    return Job(ats=ats, board=board, job_id=uid.rsplit(":", 1)[-1], company=r.get("company", ""),
               title=r.get("title", ""), location=r.get("location", ""), url=r.get("url", ""), posted_at=posted,
               workplace=r.get("workplace", ""), description=text), board


def rescore_saved(rules: Rules, texts, now: datetime, focus_map: dict, days: int, blend_ai: float | None = None) -> Counter:
    """Score every saved match (and every job hidden by the rules) again from its kept text, so a change to
    the rules or the skills dictionary applies to the whole history at once. Jobs that no longer fit move to
    the hidden list; hidden jobs that fit now come back."""
    stats = Counter()
    since = now - timedelta(days=days)

    def again(r: dict, text: str):
        job, board = _row_job(r, text)
        b = focus_map.get(f"{job.ats}:{board}".lower()) or Board(job.ats, board, job.company)
        return assess(job, b, rules, r.get("category") or "wide", now=now)

    ups, drop, hidden_rows = {}, set(), []
    matches = read_matches(since=since)
    have = {r["uid"] for r in matches}
    for r in matches:
        text = texts.get(r["uid"])
        if not text:
            stats["text_missing"] += 1
            continue
        a = again(r, text)
        if a.status == "old" or a.location_class == "non_us":
            continue
        flags = a.flags + (["repost"] if "repost" in (r.get("flags") or "") else [])
        score = a.score
        if blend_ai is not None and str(r.get("ai_score", "")).strip():
            score = ai_fit.blend(score, int(r["ai_score"]), blend_ai)
        fields = {"score": score, "flags": "; ".join(flags), "has": ", ".join(a.has),
                  "missing": ", ".join(a.missing), "detail": a.detail}
        if a.status == "blocked":
            drop.add(r["uid"])
            clean = {k: v for k, v in r.items() if not k.startswith("_")}
            hidden_rows.append({**clean, **fields, "status": "blocked", "reject_reason": "rules: " + fields["flags"]})
            stats["rescore_hidden"] += 1
            continue
        status = r["status"]
        if a.status == "alert" and "repost" not in flags:
            status = "alert"
        elif a.status == "quiet":
            status = "quiet"
        fields["status"] = status
        if any(str(r.get(k, "")) != str(v) for k, v in fields.items()):
            ups[r["uid"]] = fields
            stats["rescored"] += 1
    back, restored = [], set()
    for r in read_rejected(since=since):
        uid = r["uid"]
        if uid in have or uid in restored or not (r.get("reject_reason") or "").startswith("rules:"):
            continue
        text = texts.get(uid)
        if not text:
            continue
        a = again(r, text)
        if a.status in ("alert", "quiet") and a.location_class != "non_us":
            clean = {k: v for k, v in r.items() if not k.startswith("_") and k != "reject_reason"}
            back.append({**clean, "score": a.score, "status": "quiet", "flags": "; ".join(a.flags),
                         "has": ", ".join(a.has), "missing": ", ".join(a.missing), "detail": a.detail})
            restored.add(uid)
    stats["rescore_restored"] = len(restored)
    update_matches(ups, drop)
    append_rejected(hidden_rows)
    if restored:
        _store.remove_rejected(restored)
        append_matches(back)
    return stats


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Job Radar")
    p.add_argument("--dry-run", action="store_true", help="don't send notifications")
    p.add_argument("--no-write", action="store_true", help="don't write state or report")
    p.add_argument("--only", nargs="*", help="only these ATS types")
    p.add_argument("--limit", type=int, default=0, help="max boards per ATS (testing)")
    p.add_argument("--all-shards", action="store_true", help="check every Workday board this run")
    p.add_argument("--profile", default="", help="run another search: profiles/<name>/config.yaml")
    p.add_argument("--full-sweep", action="store_true", help="search focus boards for every keyword this run")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    if args.profile:
        from . import profile
        global CONFIG_PATH, FOCUS_PATH
        paths = profile.activate(args.profile)
        CONFIG_PATH, FOCUS_PATH = paths["config"], paths["focus"]
    rc = run(args)
    # A worker stuck on a slow server must not keep the process (and the Actions job) alive. Workers
    # that are merely finishing up get a moment first, so a normal run returns instead of hard-exiting.
    workers = [t for t in threading.enumerate() if t is not threading.main_thread() and not t.daemon]
    give_up = time.monotonic() + 3
    for t in workers:
        t.join(max(0.0, give_up - time.monotonic()))
    if any(t.is_alive() for t in workers):
        log.warning("Exiting with slow requests still open")
        sys.stdout.flush()
        os._exit(rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
