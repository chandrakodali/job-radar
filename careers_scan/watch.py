"""Twice-daily check of the careers pages that have no job board.

    python -m careers_scan.watch [--dry-run] [--limit N]

For every page in careers_scan/data/watchlist.csv it:
- compares the text with the last snapshot (careers_scan/snapshots/) -> CHANGES.md, data/changes_log.csv
- reads the job titles, keeps cloud / DevOps / SRE / platform / infrastructure roles (the radar's rules),
  opens each role's page and scores it -> ROLES.md, data/roles.csv (see apply_list.py)
- sends a phone alert for each new matching role (radar's Telegram / ntfy settings), never on the first run
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .detect import APPLY_BY_EMAIL_RE, EMAIL_RE, ROLE_RE, _soup, visible_text
from .fetch import Fetcher
from . import apply_list
from .roles import extract_roles, is_careers_url

log = logging.getLogger("careers_scan.watch")
ROOT = "careers_scan"
MAX_ALERTS = 15
NOISE_RE = re.compile(
    r"©|\(c\)\s*\d{4}|all rights reserved|cookie|privacy policy|terms of (use|service)|"
    r"^\s*(\d{1,2}[:.]\d{2}|\d+\s*(min|mins|minutes|hours?|days?|weeks?)\s+ago)\s*$|"
    r"^(skip to (main )?content|menu|close|search|back to top)$",
    re.I,
)
MIN_TEXT = 30  # characters; less means the page is built with JavaScript or failed


def page_lines(html: str) -> list[str]:
    """Stable, comparable lines of a page's main text (no menus, footers, scripts or timestamps)."""
    text = visible_text(_soup(html), drop_chrome=True)
    out, seen = [], set()
    for ln in text.splitlines():
        ln = " ".join(ln.split())
        if len(ln) < 3 or NOISE_RE.search(ln):
            continue
        if ln not in seen:
            seen.add(ln)
            out.append(ln)
    return out


def diff_lines(old: list[str], new: list[str]) -> tuple[list[str], list[str]]:
    o, n = set(old), set(new)
    return [x for x in new if x not in o], [x for x in old if x not in n]


def is_interesting(added: list[str]) -> bool:
    """New role titles, apply-by-email wording, or a new address on the page."""
    for ln in added:
        if len(ln) <= 100 and ROLE_RE.search(ln):
            return True
        if APPLY_BY_EMAIL_RE.search(ln) or EMAIL_RE.search(ln):
            return True
    return len(added) >= 5


def _snap_path(domain: str) -> str:
    safe = re.sub(r"[^a-z0-9._-]", "_", domain.lower())[:120] or "unknown"
    return os.path.join(ROOT, "snapshots", f"{safe}.txt")


def check_one(fetcher: Fetcher, item: dict, state: dict) -> dict:
    domain, url = item["domain"], item["url"]
    st = state.setdefault(domain, {})
    res = {"domain": domain, "name": item.get("name", ""), "url": url, "status": "", "added": [], "removed": []}
    pg = fetcher.page(url)
    if pg.error or pg.status >= 400:
        st["fails"] = int(st.get("fails", 0)) + 1
        res["status"] = f"failed ({pg.error or 'HTTP ' + str(pg.status)})"
        return res
    res["final_url"] = pg.url
    res["roles"] = []
    if is_careers_url(pg.url):
        try:
            res["roles"] = extract_roles(pg.text, pg.url)
        except Exception as e:  # noqa: BLE001 - a strange page must not stop the run
            log.warning("role extraction failed for %s: %s", url, type(e).__name__)
    lines = page_lines(pg.text)
    if sum(len(x) for x in lines) < MIN_TEXT:
        st["fails"] = int(st.get("fails", 0)) + 1
        res["status"] = "no readable text (JavaScript page?)"
        return res
    st["fails"] = 0
    path = _snap_path(domain)
    old = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            old = [x.rstrip("\n") for x in fh]
    if old is None:
        res["status"] = "baseline"
    else:
        res["added"], res["removed"] = diff_lines(old, lines)
        res["status"] = "changed" if (res["added"] or res["removed"]) else "same"
    if old is None or res["added"] or res["removed"]:
        res["new_lines"] = lines
    st["checked"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Watch no-job-board careers pages for new postings")
    ap.add_argument("--dry-run", action="store_true", help="no alerts and nothing written")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    wl_path = os.path.join(ROOT, "data", "watchlist.csv")
    if not os.path.exists(wl_path):
        log.warning("no watchlist yet (%s); run the careers-scan workflow first", wl_path)
        return 0
    with open(wl_path, newline="", encoding="utf-8") as fh:
        items = [r for r in csv.DictReader(fh) if r.get("domain") and r.get("url")]
    if args.limit:
        items = items[: args.limit]
    state_path = os.path.join(ROOT, "data", "watch_state.json")
    try:
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}

    fetcher = Fetcher()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = list(pool.map(lambda it: check_one(fetcher, it, state), items))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    changed = [r for r in results if r["status"] == "changed"]
    interesting = [r for r in changed if is_interesting(r["added"])]
    failing = [r for r in results if r["status"].startswith(("failed", "no readable")) and
               int(state.get(r["domain"], {}).get("fails", 0)) >= 3]
    log.info("checked %s pages: %s baseline, %s changed (%s look like new postings), %s same, %s failing",
             len(results), sum(r["status"] == "baseline" for r in results), len(changed), len(interesting),
             sum(r["status"] == "same" for r in results), len(failing))
    role_rows, new_roles, had_roles = apply_list.build(results, fetcher, ROOT)
    listed = [x for x in role_rows if x["status"] in ("alert", "quiet")]
    log.info("roles: %s cloud/DevOps/SRE/platform/infra roles (%s US or unstated), %s new since last run",
             len(role_rows), len(listed), len(new_roles))
    if args.dry_run:
        for r in interesting:
            log.info("[dry-run] %s: +%s lines, e.g. %s", r["domain"], len(r["added"]), r["added"][:3])
        for x in listed[:40]:
            log.info("[dry-run] role %s | %s | %s | %s | %s", x["score"], x["title"], x["company"], x["location"],
                     x["apply"])
        return 0
    apply_list.save(role_rows, ROOT)

    os.makedirs(os.path.join(ROOT, "snapshots"), exist_ok=True)
    for r in results:
        if r.get("new_lines") is not None:
            with open(_snap_path(r["domain"]), "w", encoding="utf-8") as fh:
                fh.write("\n".join(r["new_lines"]) + "\n")
    # Forget companies no longer on the watch list.
    keep = {it["domain"] for it in items}
    if not args.limit:
        state = {d: v for d, v in state.items() if d in keep}
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)

    md = [f"# Careers page changes", "", f"Checked {now} UTC: {len(results)} pages, {len(changed)} changed, "
          f"{len(interesting)} look like new postings.", ""]
    for r in sorted(interesting, key=lambda r: r["name"].lower()) + \
            sorted([r for r in changed if r not in interesting], key=lambda r: r["name"].lower()):
        tag = "" if r in interesting else " (minor change)"
        md += [f"## [{r['name'] or r['domain']}]({r['url']}){tag}", ""]
        md += [f"- added: {x[:200]}" for x in r["added"][:15]]
        md += [f"- removed: {x[:200]}" for x in r["removed"][:5]]
        md.append("")
    if failing:
        md += ["## Pages failing for 3+ checks", ""] + [f"- {r['domain']}: {r['status']}" for r in failing] + [""]
    with open(os.path.join(ROOT, "CHANGES.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")
    log_path = os.path.join(ROOT, "data", "changes_log.csv")
    new_file = not os.path.exists(log_path)
    with open(log_path, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(["checked_utc", "domain", "name", "url", "added", "removed", "looks_like_new_posting", "sample"])
        for r in changed:
            w.writerow([now, r["domain"], r["name"], r["url"], len(r["added"]), len(r["removed"]),
                        "yes" if r in interesting else "", " | ".join(r["added"][:3])[:300]])

    # Phone alerts: new matching roles only (page changes stay in CHANGES.md). The first run only records
    # what is already open, so you aren't flooded.
    to_alert = [x for x in new_roles if x["status"] == "alert"] if had_roles else []
    if to_alert:
        try:
            from radar.notify import Notifier
            n = Notifier({}, dry_run=False)
            if n.configured:
                for x in to_alert[:MAX_ALERTS]:
                    how = f"Apply by email: {x['apply']}" if x["apply_kind"] == "email" else f"Apply: {x['apply']}"
                    body = f"{x['title']} at {x['company']} ({x['location'] or 'location not stated'}). {how}"
                    if x["flags"]:
                        body += f" [{x['flags']}]"
                    n.send_text(body[:600], title=f"No-job-board role: {x['title']}"[:120], click=x["job_url"])
                if len(to_alert) > MAX_ALERTS:
                    n.send_text(f"{len(to_alert) - MAX_ALERTS} more new roles; see careers_scan/ROLES.md",
                                title="No-job-board roles")
        except Exception as e:  # noqa: BLE001 - alerts are optional
            log.warning("alerts not sent: %s", type(e).__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
