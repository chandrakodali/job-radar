"""Combine shard results into the published files and the watch list.

    python -m careers_scan.merge --companies careers_scan/data/companies.csv --shards-dir out --shards 10

Writes (all under careers_scan/):
    data/classified.csv     every company and how it takes applications
    data/no_job_board.csv   companies that hire without a job board (email, form, document)
    data/check_by_hand.csv  careers pages where no apply route showed up in the HTML
    data/watchlist.csv      pages the daily watcher checks for new postings
    SUMMARY.md              counts and the no-job-board list, engineering roles first
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timezone

from .scan import CATEGORIES, FIELDS, company_key, read_companies

log = logging.getLogger("careers_scan.merge")
NO_BOARD = ("email", "form", "doc")
BY_HAND = ("careers_page_unclear", "no_openings")
WATCH = NO_BOARD + BY_HAND


def load_rows(paths: list[str]) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    log.warning("%s:%s: unreadable line skipped", p, n)
                    continue
                key = r.get("domain") or ""
                if key:
                    rows[key] = {k: r.get(k, "") for k in FIELDS}
    return rows


def read_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: str, rows: list[dict], fields: list[str]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    os.replace(tmp, path)


def _sort_key(r: dict):
    cat = r.get("category", "")
    rank = CATEGORIES.index(cat) if cat in CATEGORIES else len(CATEGORIES)
    return (rank, r.get("mentions_engineering") != "yes", (r.get("name") or "").lower(), r.get("domain", ""))


def _md(s, limit=70) -> str:
    s = " ".join(str(s or "").split()).replace("|", "/")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def summary(rows: list[dict], missing_shards: list[int], scanned_now: int, carried: int, now: str) -> str:
    c = Counter(r["category"] for r in rows)
    out = [
        "# Companies that hire without a job board",
        "",
        f"Updated {now} UTC. {len(rows)} companies checked; {scanned_now} scanned in this run"
        + (f", {carried} kept from the previous run (their shard did not finish)" if carried else "") + ".",
        "",
    ]
    if missing_shards:
        out += [f"> Shards {', '.join(map(str, missing_shards))} did not finish; their companies keep earlier results.", ""]
    labels = {
        "email": "Apply by email", "form": "Apply through a form", "doc": "Jobs posted as a document (Notion, Google Docs)",
        "ats": "Applicant tracking system", "ats_probe": "Tracking system found through its API",
        "external_board": "Only on LinkedIn / Wellfound / YC / Indeed", "careers_page_unclear": "Careers page, apply route not visible",
        "no_openings": "Careers page says no openings", "no_careers_page": "No careers page found",
        "js_only": "Site needs JavaScript", "blocked": "Blocked by bot protection", "parked": "Domain parked or for sale",
        "unreachable": "Unreachable", "robots_disallowed": "robots.txt disallows", "no_website": "No website",
    }
    out += ["| How they hire | Companies |", "| --- | ---: |"]
    for cat in CATEGORIES:
        if c.get(cat):
            out.append(f"| {labels.get(cat, cat)} | {c[cat]} |")
    nb = [r for r in rows if r["category"] in NO_BOARD]
    out += ["", f"## No job board ({len(nb)})", "",
            "Engineering-related careers pages first. Full list: [data/no_job_board.csv](data/no_job_board.csv).", "",
            "| Company | How | Apply | Roles seen | Careers page |", "| --- | --- | --- | --- | --- |"]
    for r in nb[:400]:
        how = r["category"]
        apply = r.get("apply_emails") or r.get("form_url") or r.get("doc_url") or ""
        roles = r.get("roles_sample", "")
        eng = " (eng)" if r.get("mentions_engineering") == "yes" else ""
        page = r.get("careers_url") or r.get("website")
        out.append(f"| {_md(r['name'], 40)}{eng} | {how} | {_md(apply, 60)} | {_md(roles, 70)} | {_md(page, 80)} |")
    if len(nb) > 400:
        out.append(f"\n…and {len(nb) - 400} more in the CSV.")
    hand = [r for r in rows if r["category"] in BY_HAND]
    out += ["", f"## Check by hand ({len(hand)})", "",
            "Careers pages where the apply route wasn't in the page HTML (often built with JavaScript): "
            "[data/check_by_hand.csv](data/check_by_hand.csv).", ""]
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Merge careers_scan shard results")
    ap.add_argument("--companies", required=True)
    ap.add_argument("--shards-dir", required=True)
    ap.add_argument("--shards", type=int, required=True)
    ap.add_argument("--root", default="careers_scan")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    data_dir = os.path.join(args.root, "data")
    expected = {i: os.path.join(args.shards_dir, f"shard-{i}.jsonl") for i in range(args.shards)}
    found = {i: p for i, p in expected.items() if os.path.exists(p)}
    # Artifacts may land in sub-folders depending on how they were downloaded.
    for p in glob.glob(os.path.join(args.shards_dir, "**", "shard-*.jsonl"), recursive=True):
        try:
            i = int(os.path.basename(p)[len("shard-"):-len(".jsonl")])
        except ValueError:
            continue
        found.setdefault(i, p)
    missing = sorted(set(expected) - set(found))
    if not found:
        log.error("no shard results in %s", args.shards_dir)
        return 1
    new = load_rows([found[i] for i in sorted(found)])

    wanted = {company_key(c): c for c in read_companies(args.companies)}
    previous = {r.get("domain", ""): r for r in read_csv(os.path.join(data_dir, "classified.csv"))}
    rows, carried = [], 0
    for key, c in wanted.items():
        if key in new:
            rows.append(new[key])
        elif key in previous:  # its shard failed: keep the last known result
            rows.append(previous[key])
            carried += 1
    scanned_now = sum(1 for k in wanted if k in new)
    if missing and scanned_now == 0:
        log.error("every shard failed; keeping previous results untouched")
        return 1
    rows.sort(key=_sort_key)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    write_csv(os.path.join(data_dir, "classified.csv"), rows, FIELDS)
    write_csv(os.path.join(data_dir, "no_job_board.csv"), [r for r in rows if r["category"] in NO_BOARD], FIELDS)
    write_csv(os.path.join(data_dir, "check_by_hand.csv"), [r for r in rows if r["category"] in BY_HAND], FIELDS)
    watch = [{"domain": r["domain"], "name": r["name"], "url": r.get("careers_url") or r.get("website"),
              "category": r["category"]}
             for r in rows if r["category"] in WATCH and (r.get("careers_url") or r.get("website"))]
    write_csv(os.path.join(data_dir, "watchlist.csv"), watch, ["domain", "name", "url", "category"])
    with open(os.path.join(args.root, "SUMMARY.md"), "w", encoding="utf-8") as fh:
        fh.write(summary(rows, missing, scanned_now, carried, now))
    log.info("merged %s rows (%s new, %s carried over, missing shards: %s); %s", len(rows), scanned_now, carried,
             missing or "none", dict(Counter(r["category"] for r in rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
