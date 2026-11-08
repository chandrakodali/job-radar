"""Rebuild companies.csv from the public SimplifyJobs listing datasets on GitHub.

Those datasets list tens of thousands of US new-grad and intern postings with links to
the companies' own job boards. We keep the job-board identifiers (Greenhouse, Lever,
Ashby, SmartRecruiters, Workday) so the radar can watch each company directly.

Run: python scripts/build_companies.py
"""
from __future__ import annotations

import collections
import csv
import os
import re
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "companies.csv")
SOURCES = [
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
]

PATTERNS = [
    ("greenhouse", re.compile(r"https?://(?:boards|job-boards)(\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=)?([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"https?://jobs\.(eu\.)?lever\.co/([A-Za-z0-9_.-]+)")),
    ("ashby", re.compile(r"https?://jobs\.ashbyhq\.com/([^/?#]+)")),
    ("workday", re.compile(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")),
    ("smartrecruiters", re.compile(r"https?://(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
    ("workday_site", re.compile(r"https?://(wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)")),
    ("oracle", re.compile(r"https?://([a-z0-9.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-zA-Z-]+/sites/([A-Za-z0-9_-]+)")),
    ("workable", re.compile(r"https?://apply\.workable\.com/([A-Za-z0-9_-]+)")),
    ("rippling", re.compile(r"https?://ats\.rippling\.com/([A-Za-z0-9_-]+)")),
    ("bamboohr", re.compile(r"https?://([a-z0-9-]+)\.bamboohr\.com/(?:careers|jobs)")),
    ("recruitee", re.compile(r"https?://([a-z0-9-]+)\.recruitee\.com/")),
]
BAD_SLUGS = {"embed", "job_app", "jobs", "search", "apply", "en-us", "en"}
BAD_WORKDAY_SITE = re.compile(r"(?i)private|confidential|internal|executive|employee|referral|^job$|^jobs$")


def board_from_url(url: str) -> tuple[str, str] | None:
    for ats, pat in PATTERNS:
        m = pat.search(url or "")
        if not m:
            continue
        if ats in ("greenhouse", "lever"):
            eu, slug = m.group(1), m.group(2)
            slug = slug.lower()
            if slug in BAD_SLUGS:
                return None
            return ats, ("eu:" + slug) if eu else slug
        if ats == "ashby":
            slug = requests.utils.unquote(m.group(1))
            return (ats, slug) if slug.lower() not in BAD_SLUGS else None
        if ats == "workday":
            tenant, wd, site = m.group(1).lower(), m.group(2).lower(), m.group(3)
            if BAD_WORKDAY_SITE.search(site) or site.lower() in BAD_SLUGS:
                return None
            return ats, f"{tenant}|{wd}|{site}"
        if ats == "workday_site":
            wd, tenant, site = m.group(1).lower(), m.group(2).lower(), m.group(3)
            if BAD_WORKDAY_SITE.search(site):
                return None
            return "workday", f"@{tenant}|{wd}|{site}"
        if ats == "oracle":
            return ats, f"{m.group(1).lower()}|{m.group(2)}"
        if ats in ("workable", "rippling", "bamboohr", "recruitee"):
            slug = m.group(1).lower()
            return (ats, slug) if slug not in BAD_SLUGS and slug not in {"api", "www", "app"} else None
        if ats == "smartrecruiters":
            slug = m.group(1).lower()
            return (ats, slug) if slug not in BAD_SLUGS else None
    return None


def main() -> int:
    rows = []
    for url in SOURCES:
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        data = r.json()
        print(f"{url.split('/')[4]}: {len(data)} listings")
        rows.extend(data)

    now = time.time()
    comp = collections.defaultdict(lambda: {"names": collections.Counter(), "n": 0, "cit": 0, "last": 0})
    for r in rows:
        b = board_from_url(r.get("url") or "")
        if not b:
            continue
        key = (b[0], b[1].lower())
        c = comp[key]
        c.setdefault("board", b[1])
        c["names"][(r.get("company_name") or "").strip()] += 1
        c["n"] += 1
        if "citizenship" in (r.get("sponsorship") or "").lower():
            c["cit"] += 1
        c["last"] = max(c["last"], int(r.get("date_posted") or 0))

    out = []
    for (ats, _), c in comp.items():
        if c["n"] >= 3 and c["cit"] / c["n"] >= 0.5:
            continue  # mostly citizenship-required postings
        if c["last"] and now - c["last"] > 540 * 86400:
            continue  # no listing in ~18 months
        name = c["names"].most_common(1)[0][0] or c["board"]
        out.append({"ats": ats, "board": c["board"], "company": name})
    out.sort(key=lambda x: (x["ats"], x["company"].lower(), x["board"]))

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ats", "board", "company"])
        w.writeheader()
        w.writerows(out)
    counts = collections.Counter(x["ats"] for x in out)
    print(f"Wrote {len(out)} boards to companies.csv: {dict(counts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
