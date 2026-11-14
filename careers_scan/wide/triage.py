"""Read a shard of careers pages and write site and role results."""
import argparse
import gzip
import json
import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from careers_scan.fetch import USER_AGENT, Fetcher
from careers_scan.roles import extract_roles, is_careers_url
from careers_scan.scan import shard_of
from careers_scan.wide.hint import (TECH_RE, HintScanner, OnePageFetcher, archived_page,
                                    guess_name, us_class)
from careers_scan.wide.state import APPLY_CATEGORIES, read_csv

log = logging.getLogger(__name__)
ROLE_CATEGORIES = APPLY_CATEGORIES | {"careers_page_unclear", "no_openings"}
ROW_FIELDS = ("domain", "category", "careers_url", "apply_emails", "apply_phrase",
              "form_url", "doc_url", "ats", "note", "final_site")
_archive_lock = threading.Lock()
_last_archive = 0.0


class WideFetcher(Fetcher):
    def __init__(self):
        super().__init__(timeout=(6, 15), max_bytes=1_500_000)

    def _session(self):
        session = getattr(self._local, "s", None)
        if session is None:
            session = requests.Session()
            retry = Retry(total=1, connect=0, read=0, status=1, backoff_factor=0.5,
                          status_forcelist=(429, 503), allowed_methods={"GET", "HEAD"},
                          respect_retry_after_header=False, raise_on_status=False)
            for scheme in ("https://", "http://"):
                session.mount(scheme, HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4))
            session.max_redirects = 10
            session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"})
            self._local.s = session
        return session


def _archive(session, warc):
    global _last_archive
    with _archive_lock:
        delay = _last_archive + 0.5 - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        _last_archive = time.monotonic()
    return archived_page(session, warc)


def triage_one(fetcher, session, item, use_archive=True):
    domain, url = item["domain"], item["url"]
    name = item.get("name") or ""
    row, page = HintScanner(fetcher, probe=False, time_budget=45).scan_hint(
        domain, url, item.get("alt_url") or "", name)
    via = "live"
    if (use_archive and item.get("warc") and row["category"] in {"blocked", "unreachable"}
            and "robots.txt disallows" not in row.get("note", "")):
        html = _archive(session, item["warc"])
        if html:
            archived_row, archived_info = HintScanner(OnePageFetcher(url, html), probe=False).scan_hint(
                domain, url, name=name)
            if archived_row["category"] in ROLE_CATEGORIES | {"ats"}:
                row, page, via = archived_row, archived_info, "archive"
    roles = []
    us = "unknown"
    if page["html"]:
        name = guess_name(page["html"], page["title"], domain)
        us = us_class(page["text"])
        if row["category"] in ROLE_CATEGORIES and is_careers_url(page["url"]):
            roles = [{"t": r.title, "u": r.url, "l": r.location, "e": r.emails[:2], "a": r.apply_url}
                     for r in extract_roles(page["html"], page["url"])[:40]]
    tech = any(TECH_RE.search(role["t"]) for role in roles) if roles else row["mentions_engineering"] == "yes"
    return {**{key: row.get(key, "") for key in ROW_FIELDS},
            "name": name or row.get("name", domain), "us": us, "tech": int(tech), "roles": roles, "via": via}


def _safe_triage(fetcher, item, use_archive, deadline):
    if deadline is not None and time.monotonic() >= deadline:
        return None
    try:
        return triage_one(fetcher, fetcher._session() if use_archive else None, item, use_archive)
    except Exception as exc:
        log.warning("%s: %s", item["domain"], type(exc).__name__)
        return {"domain": item["domain"], "category": "unreachable", "via": "live",
                "note": type(exc).__name__}


def run(items, out, workers=48, deadline=None, use_archive=True, fetcher_factory=WideFetcher):
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    items = iter(items)
    count = 0
    with gzip.open(part, "wt", encoding="utf-8") as fh, ThreadPoolExecutor(max_workers=workers) as pool:
        while deadline is None or time.monotonic() < deadline:
            fetcher = fetcher_factory()
            pending = set()
            batch = 0
            exhausted = False
            while True:
                while len(pending) < workers and batch < 2000 and not exhausted:
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    item = next(items, None)
                    if item is None:
                        exhausted = True
                        break
                    pending.add(pool.submit(_safe_triage, fetcher, item, use_archive, deadline))
                    batch += 1
                if not pending:
                    break
                finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                for task in finished:
                    result = task.result()
                    if result is not None:
                        fh.write(json.dumps(result, ensure_ascii=False) + "\n")
                        count += 1
            if exhausted or batch == 0:
                break
    part.replace(out)
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worklist", required=True)
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--workers", type=int, default=48)
    parser.add_argument("--deadline-minutes", type=float, default=300)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--no-archive", action="store_true")
    args = parser.parse_args(argv)
    if not (0 <= args.shard < args.shards and args.workers > 0 and args.limit >= 0
            and args.deadline_minutes >= 0):
        parser.error("invalid shard, worker, limit or deadline value")
    from itertools import islice

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    items = (item for item in read_csv(args.worklist) if shard_of(item["domain"], args.shards) == args.shard)
    if args.limit:
        items = islice(items, args.limit)
    deadline = time.monotonic() + args.deadline_minutes * 60
    print(run(items, args.out, args.workers, deadline, not args.no_archive), "sites triaged")


if __name__ == "__main__":
    main()
