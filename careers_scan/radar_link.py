"""Hand job boards found by the careers scan to the radar (which fetches, filters and alerts on ATS jobs).

    python -m careers_scan.radar_link      # writes careers_scan/data/radar_boards.csv

The radar reads that file like discovered.csv: boards it already knows are skipped there.
"""
from __future__ import annotations

import csv
import logging
import os
import re
import sys
from urllib.parse import parse_qs, unquote, urlsplit

log = logging.getLogger("careers_scan.radar_link")
ROOT = "careers_scan"
OUT = os.path.join(ROOT, "data", "radar_boards.csv")
RADAR_LISTS = ("companies.csv", "companies_custom.csv", "discovered.csv")
SUBDOMAIN_ATS = {"recruitee", "bamboohr", "breezy", "pinpoint", "jazzhr", "teamtailor"}  # board = x in x.vendor.com


def _segs(path: str) -> list[str]:
    return [unquote(s) for s in (path or "").split("/") if s]


def _sub(host: str, apex: str) -> str:
    """'acme' from 'acme.bamboohr.com' (first label before the vendor domain), '' if none."""
    if host == apex or not host.endswith("." + apex):
        return ""
    label = host[: -len(apex) - 1].split(".")[0]
    return "" if label in ("www", "api", "app", "jobs", "careers", "boards", "apply") else label


def radar_board(url: str) -> tuple[str, str] | None:
    """Turn a job-board URL into the radar's (ats, board) pair, or None for vendors the radar can't read.

    Board formats follow radar/ats.py and radar/ats_more.py (slugs keep their case: some APIs need it).
    """
    try:
        p = urlsplit((url or "").strip())
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    segs = _segs(p.path)
    first = segs[0] if segs else ""
    if host.endswith("greenhouse.io"):
        q = parse_qs(p.query)
        slug = (q.get("for") or [""])[0] or (first if first not in ("embed", "v1", "boards") else "")
        if first == "v1" and len(segs) >= 3 and segs[1] == "boards":
            slug = segs[2]
        eu = ".eu." in f".{host}" or host.startswith("eu.")
        return ("greenhouse", ("eu:" if eu else "") + slug) if slug else None
    if host.endswith("lever.co") and host != "lever.co" and not host.startswith("www."):
        eu = "eu:" if ".eu." in f".{host}" else ""
        if first == "v0" and len(segs) >= 3 and segs[1] == "postings":
            slug = segs[2]
        else:
            slug = first if first not in ("v0", "postings") else ""
        return ("lever", eu + slug) if slug and re.fullmatch(r"[\w.\-]+", slug) else None
    if host.endswith(".myworkdayjobs.com"):
        parts = host.split(".")
        if len(parts) >= 4 and re.fullmatch(r"wd\d+", parts[1]):
            site = [x for x in segs if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", x)][:1]
            if site and site[0] not in ("wday", "job"):
                return "workday", f"{parts[0]}|{parts[1]}|{site[0]}"
        return None
    if host.endswith(".myworkdaysite.com") and len(segs) >= 3 and segs[0] == "recruiting":
        return "workday", f"@{segs[1]}|{host.split('.')[0]}|{segs[2]}"
    if host.endswith("ashbyhq.com") and host.split(".")[0] in ("jobs", "api") and first:
        if first == "posting-api" and len(segs) >= 3:
            return "ashby", segs[2]
        return "ashby", first
    if host in ("apply.workable.com", "jobs.workable.com") and first and first not in ("api", "j"):
        return "workable", first
    if host.endswith(".workable.com") and _sub(host, "workable.com"):
        return "workable", _sub(host, "workable.com")
    if host.endswith("smartrecruiters.com") and host.split(".")[0] in ("jobs", "careers") and first:
        return "smartrecruiters", first
    for apex, ats in (("recruitee.com", "recruitee"), ("bamboohr.com", "bamboohr"), ("breezy.hr", "breezy"),
                      ("pinpointhq.com", "pinpoint"), ("applytojob.com", "jazzhr")):
        if _sub(host, apex):
            return ats, _sub(host, apex)
    if host.endswith(".teamtailor.com") and _sub(host, "teamtailor.com"):
        return "teamtailor", _sub(host, "teamtailor.com")
    if host == "ats.rippling.com" and first:
        return "rippling", first
    if host in ("app.dover.com", "app.dover.io") and len(segs) >= 2 and segs[0] in ("apply", "jobs"):
        return "dover", segs[1]
    if host in ("jobs.dover.com", "jobs.dover.io") and first and first.lower() != "dover":
        return "dover", first
    if host == "jobs.gem.com" and first:
        return "gem", first
    if host == "jobs.jobvite.com" and first:
        return "jobvite", first
    if host.endswith("careers-page.com") and first:
        return "manatal", first
    return None


def known_boards(root: str = ".") -> set[str]:
    keys = set()
    for name in RADAR_LISTS:
        path = os.path.join(root, name)
        if not os.path.exists(path):
            continue
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                ats, board = (r.get("ats") or "").strip().lower(), (r.get("board") or "").strip()
                if ats and board:
                    keys.add(f"{ats}:{board}".lower())
    return keys


def build(classified: list[dict], known: set[str]) -> list[dict]:
    out, seen = [], set()
    for r in classified:
        if r.get("category") not in ("ats", "ats_probe"):
            continue
        hit = radar_board(r.get("ats_url", ""))
        if not hit:
            continue
        ats, board = hit
        key = f"{ats}:{board}".lower()
        if key in known or key in seen or not re.fullmatch(r"(eu:|@)?[\w.\-|@]+", board):
            continue
        if ats in SUBDOMAIN_ATS and not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", board.lower()):
            continue  # not a valid host label (e.g. JazzHR's temporary "20210212011927_x" ids)
        seen.add(key)
        out.append({"ats": ats, "board": board, "company": r.get("name") or board, "domain": r.get("domain", "")})
    out.sort(key=lambda x: (x["ats"], x["board"].lower()))
    return out


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    path = os.path.join(ROOT, "data", "classified.csv")
    if not os.path.exists(path):
        log.warning("no classified.csv yet; run the careers scan first")
        return 0
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    boards = build(rows, known_boards("."))
    tmp = OUT + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["ats", "board", "company", "domain"])
        w.writeheader()
        w.writerows(boards)
    os.replace(tmp, OUT)
    ats_rows = sum(1 for r in rows if r.get("category") in ("ats", "ats_probe"))
    log.info("%s companies use a tracking system; %s boards are new to the radar -> %s", ats_rows, len(boards), OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
