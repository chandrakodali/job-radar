"""Download the newest quarterly LCA disclosure file from the Department of Labor (public domain).

    python scripts/fetch_lca.py OUT.xlsx      exits 1 when no file could be found
"""
from __future__ import annotations

import sys
from datetime import date

import requests

BASE = "https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/LCA_Disclosure_Data_FY{fy}_Q{q}.xlsx"
UA = "job-radar/1.0 (personal job search tool; +https://github.com/chandrakodali/job-radar)"


def main(out: str) -> int:
    today = date.today()
    fy = today.year + (1 if today.month >= 10 else 0)   # federal fiscal year starts in October
    s = requests.Session()
    s.headers["User-Agent"] = UA
    for y in (fy, fy - 1):
        for q in (4, 3, 2, 1):
            url = BASE.format(fy=y, q=q)
            try:
                r = s.get(url, stream=True, timeout=120)
            except requests.RequestException as e:
                print(f"{url}: {e}")
                continue
            if r.status_code != 200 or "html" in r.headers.get("Content-Type", ""):
                print(f"{url}: HTTP {r.status_code}")
                continue
            with open(out, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
            print(f"Downloaded {url}")
            return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
