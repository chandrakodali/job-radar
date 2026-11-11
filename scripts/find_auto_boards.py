"""Find and check the job boards of a profile's employers (run in GitHub Actions, where the job
systems are reachable).

    python scripts/find_auto_boards.py --profile automotive            # check + look for new boards
    python scripts/find_auto_boards.py --profile automotive --check    # only re-check companies.csv

Reads  profiles/<name>/employers.csv   name,kind,value
         kind=page   a careers page to read; job-board links found on it are checked
         kind=guess  "ats:board" to try directly (e.g. greenhouse:waymo, sfrmk:jobs.zf.com)
       profiles/<name>/companies.csv   the boards the radar scans (each is re-checked)
Writes profiles/<name>/boards_report.csv and prints the same as a table: for every board whether it
answers, how many jobs it lists, and whether its keyword search works. Nothing is added to
companies.csv automatically: read the report and add the rows that are right.
"""
from __future__ import annotations

import argparse
import csv
import html
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from careers_scan.radar_link import radar_board  # noqa: E402
from radar import ats as ATS, ats_more, deep, robots  # noqa: E402
from radar.http import FetchError, session  # noqa: E402
from radar.match import classify_location  # noqa: E402
from radar.models import Board  # noqa: E402

ATS_URL = re.compile(
    r"https?:(?://|\\/\\/|\\u002F\\u002F)[\w.\-]*(?:myworkdayjobs\.com|myworkdaysite\.com|greenhouse\.io|lever\.co|"
    r"ashbyhq\.com|smartrecruiters\.com|icims\.com|oraclecloud\.com|eightfold\.ai|avature\.net|jobvite\.com|"
    r"taleo\.net|workable\.com|successfactors\.(?:com|eu)|ultipro\.com|dayforcehcm\.com|phenompeople\.com)"
    r"[^\s\"'<>)]*", re.I)


def extra_board(url: str) -> tuple[str, str] | None:
    """Job systems careers_scan.radar_link doesn't map."""
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    segs = [s for s in p.path.split("/") if s]
    if host.endswith(".oraclecloud.com"):
        m = re.search(r"/sites/([\w-]+)", p.path)
        return ("oracle", f"{host}|{m.group(1)}") if m else None
    if host.endswith(".icims.com") and not host.startswith("www."):
        return "icims", host
    if host.endswith(".eightfold.ai"):
        return "eightfold", f"{host.split('.')[0]}|"
    if host.endswith(".avature.net"):
        portal = "/".join(s for s in segs[:2] if s.lower() not in ("jobdetail", "searchjobs")) or "careers"
        return "avature", f"{host}|{portal}"
    if host.endswith(".taleo.net") and "careersection" in segs:
        i = segs.index("careersection")
        if len(segs) > i + 1:
            return "taleo", f"{host.split('.')[0]}|{segs[i + 1]}"
    return None


def links_on(url: str) -> tuple[str, set[tuple[str, str]]]:
    """(final URL after redirects, boards linked from the page)."""
    robots.check(url)
    r = session().get(url, timeout=(10, 30), headers={"Accept": "text/html,*/*;q=0.8"})
    if r.status_code != 200:
        raise FetchError(f"HTTP {r.status_code}", r.status_code)
    text = html.unescape(r.text[:3_000_000]).replace("\\/", "/").replace("\\u002F", "/")
    found = set()
    for u in [r.url] + [m.group(0) for m in ATS_URL.finditer(text)]:
        b = radar_board(u) or extra_board(u)
        if b and b[1]:
            found.add(b)
    # The page itself may be a SuccessFactors career site (served from the company's own domain).
    host = (urlsplit(r.url).hostname or "").lower()
    if re.search(r"jobs2web|successfactors|rmkcdn|/job_sitemap\.xml", text, re.I) and host:
        found.add(("sfrmk", host))
    return r.url, found


def check(ats: str, board: str, company: str) -> dict:
    b = Board(ats=ats, board=board, company=company, focus=True)
    row = {"company": company, "ats": ats, "board": board, "ok": "no", "jobs": "", "us": "", "search": "", "note": ""}
    try:
        if ats == "workday":
            jobs = ATS.workday_list(b, max_pages=2)
        elif ats not in ATS.LISTERS:
            row["note"] = "no reader for this job system"
            return row
        else:
            jobs = ATS.LISTERS[ats](b)
        row.update(ok="yes", jobs=len(jobs),
                   us=sum(1 for j in jobs if classify_location(j.location, j.countries) != "non_us"))
        if jobs:
            row["note"] = "e.g. " + "; ".join(j.title for j in jobs[:2])[:90]
    except FetchError as e:
        row["note"] = f"list: {e}"[:90]
    except Exception as e:  # a reader bug must not stop the report
        row["note"] = f"list: {type(e).__name__}: {e}"[:90]
    if ats in deep.SEARCHERS:
        try:
            hits = deep.SEARCHERS[ats](b, "embedded", 1)
            row["search"] = len(hits)
            if hits:
                row["ok"] = "yes"
                row["note"] = (row["note"] + " | search e.g. " + hits[0].title)[:160]
        except FetchError as e:
            row["search"] = f"fail: {e}"[:40]
        except Exception as e:
            row["search"] = f"fail: {type(e).__name__}"[:40]
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="automotive")
    ap.add_argument("--check", action="store_true", help="only re-check companies.csv")
    args = ap.parse_args()
    base = os.path.join(ROOT, "profiles", args.profile)
    from datetime import date
    robots.install(robots.Robots({}, date.today().isoformat()))

    todo: dict[tuple[str, str], str] = {}   # (ats, board) -> company
    known = set()
    with open(os.path.join(base, "companies.csv"), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ats, board = (r.get("ats") or "").strip().lower(), (r.get("board") or "").strip()
            if ats and board and not ats.startswith("#"):
                todo[(ats, board)] = r.get("company") or board
                known.add((ats, board.lower()))
    pages = []
    emp = os.path.join(base, "employers.csv")
    if not args.check and os.path.exists(emp):
        with open(emp, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                name, kind, value = (r.get("name") or "").strip(), (r.get("kind") or "").strip(), (r.get("value") or "").strip()
                if not name or name.startswith("#") or not value:
                    continue
                if kind == "guess" and ":" in value:
                    ats, board = value.split(":", 1)
                    todo.setdefault((ats.strip().lower(), board.strip()), name)
                elif kind == "page":
                    pages.append((name, value))

    def scan(item):
        name, url = item
        try:
            final, found = links_on(url)
            return name, url, final, found, ""
        except Exception as e:
            return name, url, "", set(), f"{type(e).__name__}: {e}"[:80]

    print("## Careers pages\n")
    with ThreadPoolExecutor(8) as pool:
        for name, url, final, found, err in pool.map(scan, pages):
            print(f"- {name}: {url} -> {err or final} | boards: {sorted(found) or 'none found'}")
            for ats, board in found:
                todo.setdefault((ats, board), name)

    with ThreadPoolExecutor(12) as pool:
        rows = list(pool.map(lambda kv: check(kv[0][0], kv[0][1], kv[1]), sorted(todo.items(), key=lambda kv: kv[1].lower())))
    for r in rows:
        r["listed"] = "yes" if (r["ats"], r["board"].lower()) in known else "new"
    out = os.path.join(base, "boards_report.csv")
    fields = ["company", "ats", "board", "listed", "ok", "jobs", "us", "search", "note"]
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print("\n## Boards\n")
    print("| " + " | ".join(fields) + " |")
    for r in rows:
        print("| " + " | ".join(str(r[k]).replace("|", "¦") for k in fields) + " |")
    bad = [r for r in rows if r["listed"] == "yes" and r["ok"] != "yes"]
    print(f"\n{len(rows)} boards checked; {sum(r['ok'] == 'yes' for r in rows)} answer; "
          f"{len(bad)} listed boards do not answer: {[(r['ats'], r['board']) for r in bad]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
