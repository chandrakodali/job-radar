"""Export optional sources of employer domains for the wide sweep."""
import argparse
import csv
import os
import re
from datetime import date
from itertools import islice, product
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from careers_scan.roles import infra_category, load_rules
from careers_scan.urls import PROFILE_PLATFORMS, registrable_domain
from careers_scan.wide.ccindex import DENY
from radar.http import get_json, post_json
from radar.sources import HN_ITEM, HN_SEARCH

EMAIL = re.compile(r"[\w.+-]+@([\w.-]+\.[a-z]{2,})", re.I)
EXCLUDED = DENY | PROFILE_PLATFORMS | {
    "gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com", "proton.me",
    "protonmail.com", "aol.com", "live.com", "example.com", "ycombinator.com",
}
PHRASES = ('"send your resume to"', '"send your CV to"', '"email your resume"', '"email your CV"',
           '"submit your resume to"', '"apply by email"')
TITLES = ("devops engineer", "site reliability engineer", "cloud engineer", "platform engineer",
          "infrastructure engineer", "systems administrator", "network engineer", "security engineer")


def candidate(raw, homepage=False):
    url = raw.strip()
    if "://" not in url:
        url = "https://" + url
    try:
        parsed = urlsplit(url)
        domain = registrable_domain(parsed.hostname or "")
        port = parsed.port
    except ValueError:
        return None
    if (parsed.scheme not in {"https", "http"} or not domain or domain in EXCLUDED
            or parsed.username is not None or parsed.password is not None or port not in (None, 443, 80)):
        return None
    if homepage:
        return domain, f"https://{domain}/"
    return domain, parsed._replace(fragment="").geturl()


def hacker_news():
    query = {"tags": "story,author_whoishiring", "query": "Who is hiring?", "hitsPerPage": 30}
    hits = get_json(HN_SEARCH, params=query).get("hits", [])
    threads = (hit for hit in hits if re.match(r"^ask hn: who is hiring\?", hit.get("title") or "", re.I))
    for thread in islice(threads, 6):
        item = get_json(HN_ITEM.format(id=thread["objectID"]))
        for comment in item.get("children") or []:
            if not comment.get("text"):
                continue
            page = BeautifulSoup(comment["text"], "html.parser")
            for link in page.find_all("a", href=True):
                href = link["href"]
                if href.startswith(("http://", "https://")):
                    yield candidate(href, homepage=True)
                elif href.startswith("mailto:"):
                    for domain in EMAIL.findall(href):
                        yield candidate(domain, homepage=True)
            for domain in EMAIL.findall(page.get_text(" ", strip=True)):
                yield candidate(domain, homepage=True)


def phrase_search(key, today):
    if not key:
        return
    queries = list(product(PHRASES, TITLES))
    start = today.toordinal() * 30 % len(queries)
    for phrase, title in (queries * 2)[start:start + 30]:
        response = post_json("https://api.tavily.com/search", {
            "query": f'{phrase} "{title}"', "search_depth": "basic", "max_results": 8,
            "exclude_domains": sorted(EXCLUDED), "country": "united states", "auto_parameters": False,
        }, headers={"Authorization": f"Bearer {key}"})
        for item in response.get("results") or []:
            yield candidate(item["url"])


def lca(path, profiles):
    from openpyxl import load_workbook

    rules = [load_rules(profile)[0] for profile in profiles]
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet in book.worksheets:
            rows = sheet.values
            header = next(rows, ())
            required = {"CASE_STATUS", "EMPLOYER_POC_EMAIL", "JOB_TITLE"}
            if not required.issubset(header):
                continue
            status, email, title = (header.index(field) for field in ("CASE_STATUS", "EMPLOYER_POC_EMAIL", "JOB_TITLE"))
            for row in rows:
                if row[status] != "Certified" or not row[email] or not row[title]:
                    continue
                if any(infra_category(str(row[title]), rule) for rule in rules):
                    for domain in EMAIL.findall(str(row[email])):
                        yield candidate(domain, homepage=True)
            return
        raise ValueError("LCA workbook lacks CASE_STATUS, EMPLOYER_POC_EMAIL or JOB_TITLE")
    finally:
        book.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=("hn", "tavily", "lca"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--xlsx", type=Path)
    parser.add_argument("--profiles", nargs="+", default=["config.yaml", "profiles/freshers/config.yaml"])
    args = parser.parse_args(argv)
    if args.source == "hn":
        found = hacker_news()
    elif args.source == "tavily":
        found = phrase_search(os.environ.get("TAVILY_API_KEY"), date.today())
    elif args.xlsx:
        found = lca(args.xlsx, args.profiles)
    else:
        parser.error("lca requires --xlsx")
    sites = {}
    for row in found:
        if row is not None:
            domain, url = row
            sites.setdefault(domain, url)
    with args.out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(("domain", "url"))
        writer.writerows(sorted(sites.items()))


if __name__ == "__main__":
    main()
