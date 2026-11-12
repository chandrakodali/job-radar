"""In-depth scan of a profile's own employer boards ("focus" boards).

A normal pass lists a board's newest jobs and judges them by title. For focus boards we also want the
jobs whose titles say little ("Engineer II", "Software Engineer") but whose text names the candidate's
tools. Where the job system has a full-text search, we run it once per keyword and merge the results;
where it returns every job in one call, we ask for the descriptions too.
"""
from __future__ import annotations

import logging
import threading

from . import ats as ATS
from . import ats_more
from .http import FetchError
from .models import Board, Job

log = logging.getLogger("radar.deep")
_LOCK = threading.Lock()   # boards are scanned on several threads; they share the run's counters


def _count(stats, key: str, n: int = 1) -> None:
    if stats is not None:
        with _LOCK:
            stats[key] += n

# Job systems with a keyword search of the full job text: ats -> search(board, term, pages).
SEARCHERS = {
    "workday": lambda b, term, pages: ATS.workday_list(b, max_pages=pages, search=term, us_only=True),
    "smartrecruiters": lambda b, term, pages: ATS.smartrecruiters_list(b, max_pages=max(1, pages // 2), search=term),
    "oracle": lambda b, term, pages: ATS.oracle_list(b, max_pages=pages, search=term),
    "eightfold": lambda b, term, pages: ats_more.eightfold_list(b, num=pages * 10, search=term),
    "phenom": lambda b, term, pages: ats_more.phenom_list(b, max_pages=1, search=term),
    "sfrmk": lambda b, term, pages: ats_more.sfrmk_search(b, term, max_pages=pages),
    "jibe": lambda b, term, pages: ats_more.jibe_list(b, max_pages=pages, search=term),
    "mcloud": lambda b, term, pages: ats_more.mcloud_list(b, max_pages=pages, search=term),
}


def base_list(b: Board, opts: dict) -> list[Job]:
    """The board's ordinary listing, reaching further back than the every-30-minutes radar needs to.
    With `full_list`, every US job the board lists is read whatever its age (Workday, Oracle,
    SmartRecruiters), so no job is missed because no keyword happened to find it."""
    fl = opts.get("full_list") or []
    full = b.ats in fl if isinstance(fl, (list, tuple)) else bool(fl)
    days = int(opts.get("full_max_age_days", 150)) if full else int(opts.get("list_max_age_days", 30))
    pages = int(opts.get("full_max_pages", 150)) if full else int(opts.get("list_max_pages", 10))
    if b.ats == "workday":
        return ATS.workday_list(b, max_pages=pages, max_age_days=days, us_only=True, full=full)
    if b.ats == "smartrecruiters":
        return ATS.smartrecruiters_list(b, max_pages=pages, max_age_days=max(days, 120))
    if b.ats == "oracle":
        return ATS.oracle_list(b, max_pages=pages, max_age_days=days)
    if b.ats == "greenhouse":
        return ATS.greenhouse_list(b, content=True)
    if b.ats == "jibe":
        return ats_more.jibe_list(b, max_pages=max(3, pages // 2))
    if b.ats == "sfrmk":
        return ats_more.sfrmk_list(b, max_jobs=int(opts.get("sitemap_max_jobs", ats_more.MAX_JOBS)))
    return ATS.LISTERS[b.ats](b)


def deep_list(b: Board, terms: list[str], opts: dict, stats=None) -> list[Job]:
    """Ordinary listing plus one keyword search per term, merged. A search that fails is skipped (the
    listing and the other searches still count); a rate-limit answer ends the searches for this board."""
    complete = False
    try:
        listing = base_list(b, opts)
        complete = bool(getattr(listing, "complete", False))
        jobs = {j.uid: j for j in listing}
        listed = True
    except FetchError as e:
        # Some big sites refuse the plain listing but answer searches; fail only if both do.
        if b.ats not in SEARCHERS or not terms or e.status == 429:
            raise
        jobs, listed, first_error = {}, False, e
    search = SEARCHERS.get(b.ats)
    if complete:            # every job on the board was read: keyword searches would add nothing
        _count(stats, "full_lists")
        search = None
    ok = 0
    if search:
        pages = int(opts.get("search_pages", 3))
        for term in terms:
            try:
                found = search(b, term, pages)
            except FetchError as e:
                _count(stats, "search_failed")
                log.debug("search %r failed on %s: %s", term, b.key, e)
                if e.status in (429, 403):
                    break
                continue
            ok += 1
            _count(stats, "searches")
            _count(stats, "search_hits", len(found))
            for j in found:
                have = jobs.get(j.uid)
                if have is None:
                    jobs[j.uid] = j
                elif not have.found_by:
                    have.found_by = term
    if not listed and not ok:
        raise first_error
    out = ATS.Listing(jobs.values())
    out.complete = complete
    return out
