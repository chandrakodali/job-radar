"""Build the list of companies to scan from public GitHub datasets plus your own list.

    python -m careers_scan.sources --yc /tmp/src/yc/companies/all.json \
        --oss /tmp/src/oss/categories --remoteintech /tmp/src/remoteintech/src/companies \
        --extra careers_scan/companies_extra.csv --out careers_scan/data/companies.csv

Every source is optional; a missing or broken source is logged and skipped (the others still load).
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import logging
import os
import re
import sys

import yaml
from bs4 import BeautifulSoup

from .detect import match_vendor
from .scan import Company, company_key
from .urls import normalize_website, registrable_domain

log = logging.getLogger("careers_scan.sources")
PRIORITY = {"extra": 0, "yc": 1, "oss": 2, "remoteintech": 3}


def load_yc(path: str, include_statuses=("Active",)) -> list[Company]:
    """yc-oss/api companies/all.json (https://github.com/yc-oss/api)."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError("unexpected YC data: not a list")
    out = []
    for x in data:
        if not isinstance(x, dict):
            continue
        if include_statuses and (x.get("status") or "") not in include_statuses:
            continue
        out.append(Company(name=str(x.get("name") or "").strip(), website=str(x.get("website") or "").strip(),
                           sources="yc", yc_batch=str(x.get("batch") or ""), stage=str(x.get("stage") or "")))
    return out


def load_oss(categories_dir: str) -> list[Company]:
    """Chinstrap-Community/Awesome-OSS-Companies categories/*.md (HTML tables; 4th column = website)."""
    out = []
    for path in sorted(glob.glob(os.path.join(categories_dir, "*.md"))):
        with open(path, encoding="utf-8") as fh:
            soup = BeautifulSoup(fh.read(), "html.parser")
        for tr in soup.select("tbody tr"):
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            name = " ".join(tds[0].get_text(" ").split())
            a = tds[3].find("a", href=True)
            if name and a:
                out.append(Company(name=name, website=a["href"].strip(), sources="oss"))
    return out


_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*(\n|$)", re.S)


def load_remoteintech(companies_dir: str) -> list[Company]:
    """remoteintech/remote-jobs src/companies/*.md (YAML front matter: title, website, careers_url)."""
    out = []
    for path in sorted(glob.glob(os.path.join(companies_dir, "*.md"))):
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
        m = _FRONT.match(raw)
        if not m:
            continue
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
            for key in ("title", "website", "careers_url"):  # tolerate broken YAML in one file
                mm = re.search(rf"^{key}:\s*[\"']?(.*?)[\"']?\s*$", m.group(1), re.M)
                if mm:
                    meta[key] = mm.group(1)
        if not isinstance(meta, dict):
            continue
        name = str(meta.get("title") or os.path.splitext(os.path.basename(path))[0]).strip()
        out.append(Company(name=name, website=str(meta.get("website") or "").strip(),
                           careers_url=str(meta.get("careers_url") or "").strip(), sources="remoteintech"))
    return out


def load_extra(path: str) -> list[Company]:
    """Your own list: CSV with columns name,website[,careers_url]. Lines starting with # are ignored."""
    out = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = [r for r in fh if r.strip() and not r.lstrip().startswith("#")]
    for r in csv.DictReader(rows):
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        if r.get("website") or r.get("careers_url"):
            out.append(Company(name=r.get("name") or r.get("website", ""), website=r.get("website", ""),
                               careers_url=r.get("careers_url", ""), sources="extra"))
    return out


def merge(lists: list[list[Company]]) -> tuple[list[Company], int]:
    """De-duplicate by registrable domain; combine sources; prefer the highest-priority source's name."""
    by_key: dict[str, Company] = {}
    dropped = 0
    for companies in lists:
        for c in companies:
            site = normalize_website(c.website)
            if not site and c.website and match_vendor(c.website) and not c.careers_url:
                # The "website" is really a job-board page (e.g. a YC jobs page): use it as the careers link.
                c = Company(name=c.name, website="", careers_url=c.website, sources=c.sources,
                            yc_batch=c.yc_batch, stage=c.stage)
            key = company_key(Company(c.name, site or "", c.careers_url))
            if key.startswith("name:"):
                dropped += 1
                continue
            cur = by_key.get(key)
            if cur is None:
                by_key[key] = Company(name=c.name or key, website=site or "", careers_url=c.careers_url,
                                      sources=c.sources, yc_batch=c.yc_batch, stage=c.stage)
                continue
            srcs = sorted(set(cur.sources.split("|")) | set(c.sources.split("|")), key=lambda s: PRIORITY.get(s, 9))
            if PRIORITY.get(c.sources, 9) < PRIORITY.get(cur.sources.split("|")[0], 9) and c.name:
                cur.name = c.name
            cur.sources = "|".join(srcs)
            cur.careers_url = cur.careers_url or c.careers_url
            cur.website = cur.website or site or ""
            cur.yc_batch = cur.yc_batch or c.yc_batch
            cur.stage = cur.stage or c.stage
    # An entry with only a careers link (no website) is usually the same company another source lists
    # with its website: fold it in by name.
    by_name = {re.sub(r"[^a-z0-9]", "", c.name.lower()): k for k, c in by_key.items() if c.website}
    for key in [k for k, c in by_key.items() if not c.website]:
        c = by_key[key]
        target = by_name.get(re.sub(r"[^a-z0-9]", "", c.name.lower()))
        if target:
            t = by_key[target]
            t.careers_url = t.careers_url or c.careers_url
            t.sources = "|".join(sorted(set(t.sources.split("|")) | set(c.sources.split("|")),
                                        key=lambda s: PRIORITY.get(s, 9)))
            del by_key[key]
    out = sorted(by_key.values(), key=lambda c: (company_key(c), c.name.lower()))
    return out, dropped


def write_companies(companies: list[Company], path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "website", "careers_url", "sources", "yc_batch", "stage"])
        for c in companies:
            w.writerow([c.name, c.website, c.careers_url, c.sources, c.yc_batch, c.stage])
    os.replace(tmp, path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build careers_scan/data/companies.csv")
    ap.add_argument("--yc", help="path to yc-oss companies/all.json")
    ap.add_argument("--yc-statuses", default="Active", help="comma-separated YC statuses to include")
    ap.add_argument("--oss", help="path to Awesome-OSS-Companies categories/ directory")
    ap.add_argument("--remoteintech", help="path to remote-jobs src/companies/ directory")
    ap.add_argument("--extra", help="your own CSV (name,website,careers_url)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-companies", type=int, default=1, help="fail if fewer companies than this")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    lists = []
    loaders = [("extra", args.extra, load_extra),
               ("yc", args.yc, lambda p: load_yc(p, tuple(s.strip() for s in args.yc_statuses.split(",") if s.strip()))),
               ("oss", args.oss, load_oss), ("remoteintech", args.remoteintech, load_remoteintech)]
    for name, path, fn in loaders:
        if not path:
            continue
        if not os.path.exists(path):
            log.warning("source %s: %s not found, skipped", name, path)
            continue
        try:
            got = fn(path)
            log.info("source %s: %s companies", name, len(got))
            lists.append(got)
        except Exception as e:  # noqa: BLE001 - one broken source must not stop the others
            log.error("source %s failed: %s: %s", name, type(e).__name__, e)
    companies, dropped = merge(lists)
    log.info("merged: %s unique companies (%s without a usable website dropped)", len(companies), dropped)
    if len(companies) < args.min_companies:
        log.error("only %s companies (minimum %s): refusing to overwrite the list", len(companies), args.min_companies)
        return 1
    write_companies(companies, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
