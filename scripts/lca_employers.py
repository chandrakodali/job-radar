"""Employers that hired for this kind of work in the US, from the Department of Labor's public LCA
disclosure data (H-1B/E-3 filings, public domain, published each quarter at
https://www.dol.gov/agencies/eta/foreign-labor/performance).

It is used only to FIND employers to watch, not to score jobs. Every filing names the employer, the job
title and the work site, so a list of employers that filed for "AUTOSAR Engineer", "Embedded Software
Engineer", "Controls Engineer" ... shows who hires for this work, including smaller suppliers and
engineering-service firms that are easy to miss.

    python scripts/lca_employers.py LCA_Disclosure_Data_FY2026_Q3.xlsx [more files...]
writes profiles/automotive/lca_employers.csv: employer, filings, states, sample titles, already watched?
"""
from __future__ import annotations

import csv
import os
import re
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TITLE = re.compile(
    r"autosar|simulink|model[\s-]*based|\bmbd\b|embedded (software|firmware|controls?|systems?)|"
    r"\bcontrols? (software|engineer|systems)|calibration engineer|\badas\b|driver assist|powertrain|"
    r"steering|chassis|\becus?\b|firmware|functional safety|\bhil\b|hardware[\s-]in[\s-]the[\s-]loop|"
    r"vehicle (software|controls?|systems?|integration)|software integration|feature (engineer|owner)|"
    r"\bbms\b|battery (controls?|management|software)|motor control|inverter|propulsion|autonomous|"
    r"automotive|electrification|brake|infotainment|telematics|\becu\b|diagnostics engineer",
    re.I)
NOT = re.compile(r"\b(sales|marketing|finance|account|data scientist|business analyst|nurse|teacher|"
                 r"salesforce|sap\b|web|front[\s-]?end|full[\s-]?stack|cloud|devops|mechanical design)\b", re.I)


def rows_from(path: str):
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True)
    ws = wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    head = [str(h or "").strip().upper() for h in next(it)]
    col = {h: i for i, h in enumerate(head)}
    need = ["CASE_STATUS", "EMPLOYER_NAME", "JOB_TITLE", "WORKSITE_STATE"]
    if not all(n in col for n in need):
        raise SystemExit(f"{path}: unexpected columns {head[:20]}")
    for r in it:
        yield {n: (r[col[n]] if col[n] < len(r) else None) for n in need}


def norm(name: str) -> str:
    n = re.sub(r"\b(inc|llc|l\.l\.c|ltd|corp(oration)?|co|company|na|n\.a|usa|us|america|americas|north america|"
               r"holdings?|group|technologies|technology|systems)\b\.?", " ", (name or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", n).strip()


def watched_names() -> set[str]:
    names = set()
    for path in (os.path.join(ROOT, "profiles", "automotive", "companies.csv"), os.path.join(ROOT, "companies.csv"),
                 os.path.join(ROOT, "companies_custom.csv")):
        if os.path.exists(path):
            for r in csv.DictReader(l for l in open(path, encoding="utf-8") if not l.startswith("#")):
                if r.get("company"):
                    names.add(norm(r["company"]))
    return names


def main(paths: list[str]) -> int:
    count, states, titles = Counter(), defaultdict(Counter), defaultdict(Counter)
    display = {}
    for p in paths:
        for r in rows_from(p):
            if not str(r["CASE_STATUS"] or "").lower().startswith("certified"):
                continue
            t = str(r["JOB_TITLE"] or "")
            if not TITLE.search(t) or NOT.search(t):
                continue
            key = norm(str(r["EMPLOYER_NAME"] or ""))
            if not key:
                continue
            display.setdefault(key, str(r["EMPLOYER_NAME"]).strip())
            count[key] += 1
            states[key][str(r["WORKSITE_STATE"] or "")] += 1
            titles[key][t.strip().title()] += 1
    watched = watched_names()
    out = os.path.join(ROOT, "profiles", "automotive", "lca_employers.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["employer", "filings", "top_states", "sample_titles", "watched"])
        for key, n in count.most_common():
            seen = any(key == x or (len(x) > 4 and (key.startswith(x) or x.startswith(key))) for x in watched)
            w.writerow([display[key], n, " ".join(s for s, _ in states[key].most_common(4)),
                        " | ".join(t for t, _ in titles[key].most_common(4)), "yes" if seen else ""])
    print(f"{len(count)} employers -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
