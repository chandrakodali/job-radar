"""Classify how each company takes job applications: tracking system, email, form, job site, or unknown.

Usage (one shard of the company list):
    python -m careers_scan.scan --companies careers_scan/data/companies.csv --shard 0 --shards 10 \
        --out careers_scan/out/shard-0.jsonl
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from .detect import CAREER_PATH_SEGMENTS, CAREER_TEXT_RE, analyze, match_vendor, slug_matches, vendor_slug, _alnum
from .fetch import Fetcher, Page
from .urls import canon, host_of, normalize_website, registrable_domain

log = logging.getLogger("careers_scan")

COMMON_PATHS = ("/careers", "/jobs", "/join", "/join-us", "/about/careers", "/company/careers",
                "/about-us/careers", "/work-with-us", "/hiring", "/careers.html", "/jobs.html")
FREE_MAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "icloud.com",
             "me.com", "proton.me", "protonmail.com", "pm.me", "hey.com", "fastmail.com", "zoho.com", "gmx.de",
             "gmx.net", "web.de", "mail.com", "yandex.ru", "qq.com", "163.com"}
MAX_PAGES = 12          # page fetches per company, homepage and robots.txt not counted
MAX_LISTED = 5          # careers links taken from the homepage
MAX_DETAIL = 3          # individual job pages opened when a listing shows no apply route
TIME_BUDGET = 120.0     # seconds per company

# Final categories, most to least decisive.
CATEGORIES = (
    "email",                  # careers page / job page asks for applications by email
    "form",                   # applications through a form (Google Forms, Typeform, Tally, ...)
    "doc",                    # jobs posted as a Notion page / Google Doc
    "ats",                    # applicant tracking system found on the site
    "ats_probe",              # no link on the site, but a matching board exists on Greenhouse/Lever/Ashby
    "external_board",         # jobs only on LinkedIn / Wellfound / YC / Indeed ...
    "careers_page_unclear",   # careers page found, no apply route found in its HTML
    "no_openings",            # careers page says there are no openings, no apply route
    "no_careers_page",        # site works, no careers page found
    "js_only",                # site needs JavaScript to show anything
    "blocked",                # bot protection / access denied
    "parked",                 # domain for sale / expired
    "unreachable",            # DNS, connection, TLS or HTTP errors
    "robots_disallowed",      # robots.txt forbids reading the pages
    "no_website",             # no usable website in the source data
)

FIELDS = ["domain", "name", "website", "category", "apply_emails", "apply_phrase", "careers_url", "ats",
          "ats_url", "form_url", "doc_url", "board", "board_url", "roles_found", "roles_sample",
          "mentions_engineering", "says_no_openings", "final_site", "pages_checked", "note", "sources",
          "yc_batch", "stage", "scanned_at"]


@dataclass
class Company:
    name: str
    website: str
    careers_url: str = ""
    sources: str = ""
    yc_batch: str = ""
    stage: str = ""


@dataclass
class Evidence:
    ats: list[tuple[str, str, str]] = field(default_factory=list)       # (vendor, url, where)
    boards: list[tuple[str, str]] = field(default_factory=list)
    forms: list[tuple[str, str]] = field(default_factory=list)
    docs: list[tuple[str, str]] = field(default_factory=list)
    emails: dict[str, tuple[str, str]] = field(default_factory=dict)    # email -> (kind, where)
    careers_pages: list[str] = field(default_factory=list)
    apply_phrase: str = ""
    roles: list[str] = field(default_factory=list)
    no_openings: bool = False
    mentions_eng: bool = False
    js_pages: int = 0
    notes: list[str] = field(default_factory=list)

    def add_vendor(self, kind: str, name: str, url: str, where: str) -> None:
        bucket = {"ats": self.ats, "board": self.boards, "form": self.forms, "doc": self.docs}[kind]
        if kind == "ats":
            if all(u != url for _, u, _ in self.ats):
                self.ats.append((name, url, where))
        elif all(u != url for _, u in bucket):
            bucket.append((name, url))


def shard_of(domain: str, shards: int) -> int:
    return int(hashlib.sha1(domain.encode()).hexdigest(), 16) % max(shards, 1)


def _with_path(site: str, path: str) -> str:
    p = urlsplit(site)
    return urlunsplit((p.scheme, p.netloc, path, "", ""))


def _clean_hint(u: str) -> str | None:
    u = (u or "").strip()
    if not u:
        return None
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u.lstrip("/")
    try:
        p = urlsplit(u)
    except ValueError:
        return None
    return u if p.hostname else None


class Scanner:
    def __init__(self, fetcher: Fetcher, probe: bool = True, time_budget: float = TIME_BUDGET):
        self.f = fetcher
        self.probe = probe
        self.time_budget = time_budget

    # ------------------------------------------------------------------ helpers
    def _home(self, site: str) -> Page:
        """Fetch the homepage, falling back to www./http:// when the bare https host fails."""
        host = host_of(site)
        first = self.f.page(f"https://{host}/")
        if not first.error:
            return first
        if first.error in ("dns", "connect") and not host.startswith("www."):
            alt = self.f.page(f"https://www.{host}/")
            if not alt.error:
                return alt
        if first.error in ("ssl", "connect"):
            alt = self.f.page(f"http://{host}/")
            if not alt.error:
                return alt
        return first

    def _take_vendor_links(self, ev: Evidence, info, c: Company, domains: set[str], careers_page: bool,
                           where: str) -> None:
        for link in info.vendor_links:
            text_says_careers = bool(link.text and CAREER_TEXT_RE.search(link.text))
            belongs = slug_matches(vendor_slug(link.url), c.name, next(iter(domains), "")) or any(
                slug_matches(vendor_slug(link.url), c.name, d) for d in domains)
            if link.kind in ("ats", "board"):
                # On the careers page any job-board link counts. Elsewhere it must be labelled as
                # careers or name this company (a recruiting-tech homepage may link customers' boards).
                if careers_page or text_says_careers or belongs:
                    ev.add_vendor(link.kind, link.vendor, link.url, where)
            elif careers_page or text_says_careers:
                ev.add_vendor(link.kind, link.vendor, link.url, where)

    def _take_page(self, ev: Evidence, info, careers_page: bool, where: str, domains: set[str] | None = None) -> None:
        for email, kind, _snippet in info.emails:
            if kind == "contextual" and domains is not None:
                # A generic address only counts on the company's own domain or a personal mailbox
                # (a careers page can quote a partner's or shop's address).
                rd = registrable_domain(email.split("@", 1)[1])
                if rd not in domains and rd not in FREE_MAIL:
                    continue
            if kind == "hiring" or careers_page:
                ev.emails.setdefault(email, (kind, where))
        if careers_page:
            if info.apply_by_email_phrase and not ev.apply_phrase:
                ev.apply_phrase = info.apply_by_email_phrase[:240]
            for r in info.roles:
                if r not in ev.roles:
                    ev.roles.append(r)
            ev.no_openings = ev.no_openings or info.no_openings
            ev.mentions_eng = ev.mentions_eng or info.mentions_engineering
            if info.js_shell:
                ev.js_pages += 1

    def _probe_ats(self, c: Company, domain: str) -> tuple[str, str] | None:
        """Last resort for JavaScript-built careers pages: ask the three big ATS APIs by name."""
        label = domain.split(".")[0]
        slugs = []
        for s in (_alnum(label), _alnum(c.name), re.sub(r"[^a-z0-9-]", "", c.name.lower().replace(" ", "-"))):
            if len(s) >= 3 and s not in slugs:
                slugs.append(s)
        for slug in slugs[:2]:
            st, d = self.f.json(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
            if isinstance(d, dict) and d.get("name") and slug_matches(d["name"], c.name, domain):
                # Boards outlive their use: only count one that lists jobs right now.
                st, jobs = self.f.json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
                if isinstance(jobs, dict) and jobs.get("jobs"):
                    return "greenhouse", f"https://job-boards.greenhouse.io/{slug}"
            st, d = self.f.json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
            if isinstance(d, dict) and isinstance(d.get("jobs"), list) and d["jobs"]:
                return "ashby", f"https://jobs.ashbyhq.com/{slug}"
            st, d = self.f.json(f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1")
            if isinstance(d, list) and d:
                return "lever", f"https://jobs.lever.co/{slug}"
        return None

    # ------------------------------------------------------------------ main
    def scan(self, c: Company) -> dict:
        t0 = time.monotonic()
        row = {k: "" for k in FIELDS}
        row.update(name=c.name, website=c.website, sources=c.sources, yc_batch=c.yc_batch, stage=c.stage,
                   scanned_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        site = normalize_website(c.website)
        hint = _clean_hint(c.careers_url)
        if not site and not hint:
            row.update(category="no_website", note="website missing or not a company site", domain=company_key(c))
            return row
        if not site and match_vendor(hint):
            # Only a job-board link, no website (e.g. a YC jobs page): classify from the link alone.
            v = match_vendor(hint)
            ev = Evidence()
            ev.add_vendor(v.kind, v.name, hint, "careers link")
            row.update(domain=company_key(c), category={"ats": "ats", "board": "external_board", "form": "form",
                                                        "doc": "doc"}[v.kind], note="no website; careers link only")
            return self._finish(row, ev, 0)
        domain = registrable_domain(site) if site else registrable_domain(hint)
        row["domain"] = company_key(c)
        ev = Evidence()
        pages = 0

        # A careers URL from the source data that points at a job board is weak evidence (it may be
        # out of date); what the live site shows takes priority.
        hint_vendor = match_vendor(hint) if hint else None
        if hint_vendor:
            ev.add_vendor(hint_vendor.kind, hint_vendor.name, hint, "source careers_url")

        home = self._home(site) if site else Page(url="", error="no_site")
        home_info = None
        domains = {domain}
        blocked_home = False
        if home.error and home.error != "no_site":
            row["note"] = f"homepage: {home.error}"
        elif not home.error:
            info = analyze(home.text, home.url, home.status)
            final_rd = registrable_domain(home.url)
            if final_rd and final_rd != domain:
                row["final_site"] = final_rd
                domains.add(final_rd)
                v = match_vendor(home.url)
                if v:  # the "website" is itself a job board page
                    ev.add_vendor(v.kind, v.name, home.url, "website redirect")
            if home.status >= 400 or info.blocked:
                blocked_home = info.blocked or home.status in (401, 403, 429, 503)
                row["note"] = f"homepage HTTP {home.status}" + (" (bot protection)" if info.blocked else "")
            elif info.parked:
                row.update(category="parked", note="domain parked or for sale")
                return self._finish(row, ev, pages)
            else:
                home_info = info
        if blocked_home and not hint and not ev.ats:
            row["category"] = "blocked"
            return self._finish(row, ev, pages)

        queue: list[tuple[str, bool]] = []   # (url, came from a link labelled as careers)
        seen = {canon(home.url)} if home.url else set()
        detail_urls: set[str] = set()

        def push(u: str, strong: bool, front: bool = False) -> None:
            k = canon(u)
            if k in seen:
                return
            seen.add(k)
            if front:
                queue.insert(0, (u, strong))
            else:
                queue.append((u, strong))

        if hint and not hint_vendor:
            push(hint, True)
        if home_info:
            # The homepage only counts as a careers page when the website itself is one
            # (careers.acme.com, acme.com/jobs); a homepage *titled* "jobs" is often a job-search product.
            hp = urlsplit(home.url)
            home_is_careers = (hp.hostname or "").split(".")[0] in ("careers", "jobs", "career", "karriere") or \
                any(seg.split(".")[0] in CAREER_PATH_SEGMENTS for seg in (hp.path or "").lower().split("/") if seg)
            where_home = "careers page" if home_is_careers else "homepage"
            self._take_vendor_links(ev, home_info, c, domains, home_is_careers, where_home)
            self._take_page(ev, home_info, home_is_careers, where_home, domains)
            if home_is_careers:
                ev.careers_pages.append(home.url)
            taken = 0
            for score, link in home_info.careers_links:
                if link.vendor:
                    continue  # job-board links were handled above
                if taken >= MAX_LISTED:
                    break
                k = canon(link.url)
                if k not in seen:
                    push(link.url, score >= 4)
                    taken += 1
            for path in COMMON_PATHS:
                push(_with_path(home.url, path), False)
            bare = registrable_domain(home.url) or domain
            for sub in ("careers", "jobs"):
                push(f"https://{sub}.{bare}/", False)

        while queue and pages < MAX_PAGES:
            if any(w != "source careers_url" for _, _, w in ev.ats):
                break  # decided: the site uses a tracking system
            if ev.emails and ev.careers_pages and not any(strong for _, strong in queue):
                break  # decided: apply by email, only guessed paths left
            if time.monotonic() - t0 > self.time_budget:
                ev.notes.append("time budget reached")
                break
            url, strong = queue.pop(0)
            pg = self.f.page(url)
            pages += 1
            v = match_vendor(pg.url)
            if v and canon(pg.url) != canon(url):  # /careers redirects straight to a job board or form
                ev.add_vendor(v.kind, v.name, pg.url, "careers redirect")
                continue
            if pg.error == "robots":
                ev.notes.append(f"robots.txt disallows {urlsplit(url).path or '/'}")
                continue
            if pg.error or pg.status >= 400:
                continue
            if home.url and canon(pg.url) == canon(home.url):
                continue  # redirected back to the homepage: the path doesn't exist
            if registrable_domain(pg.url) not in domains and not strong:
                continue  # a guessed path redirected off-site (parking pages, shorteners...)
            info = analyze(pg.text, pg.url, pg.status)
            if info.soft_404 or info.parked or info.blocked:
                continue
            is_detail = url in detail_urls
            careers = info.is_careers_like or strong or is_detail
            if not careers:
                for score, link in info.careers_links[:3]:  # not a careers page, but it may link to one
                    if not link.vendor and score >= 4:
                        push(link.url, True)
                continue
            where = "job page" if is_detail else "careers page"
            if not is_detail and pg.url not in ev.careers_pages:
                ev.careers_pages.append(pg.url)
            self._take_vendor_links(ev, info, c, domains, True, where)
            self._take_page(ev, info, True, where, domains)
            # A careers landing page often links to the real listing ("See open roles").
            for score, link in info.careers_links[:4]:
                if not link.vendor and registrable_domain(link.url) in domains:
                    push(link.url, score >= 4, front=True)
            # No apply route on the listing: open a few individual job pages.
            if not ev.ats and not ev.emails and not ev.forms:
                for link in info.detail_links:
                    if len(detail_urls) >= MAX_DETAIL:
                        break
                    if canon(link.url) not in seen:
                        detail_urls.add(link.url)
                        push(link.url, True)
        return self._finish(row, ev, pages, c=c, domain=domain, home_info=home_info,
                            home_error=home.error if home.error != "no_site" else "",
                            blocked_home=blocked_home)

    def _finish(self, row: dict, ev: Evidence, pages: int, c: Company | None = None, domain: str = "",
                home_info=None, home_error: str = "", blocked_home: bool = False) -> dict:
        row["pages_checked"] = pages
        if ev.careers_pages:
            row["careers_url"] = ev.careers_pages[0]
        if ev.emails:
            ranked = sorted(ev.emails.items(), key=lambda kv: (kv[1][0] != "hiring", kv[0]))
            row["apply_emails"] = "; ".join(e for e, _ in ranked[:5])
        row["apply_phrase"] = ev.apply_phrase
        if ev.ats:
            row["ats"] = ", ".join(sorted({n for n, _, _ in ev.ats}))
            row["ats_url"] = ev.ats[0][1]
        if ev.forms:
            row["form_url"] = ev.forms[0][1]
        if ev.docs:
            row["doc_url"] = ev.docs[0][1]
        if ev.boards:
            row["board"] = ", ".join(sorted({n for n, _ in ev.boards}))
            row["board_url"] = ev.boards[0][1]
        row["roles_found"] = len(ev.roles)
        row["roles_sample"] = " | ".join(ev.roles[:8])
        row["mentions_engineering"] = "yes" if ev.mentions_eng else ""
        row["says_no_openings"] = "yes" if ev.no_openings else ""
        if ev.notes:
            row["note"] = "; ".join(filter(None, [row.get("note", "")] + ev.notes))[:300]
        if row.get("category"):
            return row

        # Hiring emails only count as the apply route when they come from a careers or job page,
        # or are clearly hiring inboxes (careers@, jobs@) anywhere on the site.
        apply_emails = {e: k for e, (k, where) in ev.emails.items() if where != "homepage" or k == "hiring"}
        site_ats = [a for a in ev.ats if a[2] != "source careers_url"]
        if site_ats:
            cat = "ats"
        elif apply_emails and (ev.careers_pages or any(k == "hiring" for k in apply_emails.values())):
            cat = "email"
            # Without explicit "apply by email" wording, the page may still load a tracking system
            # with JavaScript (the address being a general one): check the big three first.
            if not ev.apply_phrase and self.probe and c is not None and domain:
                hit = self._probe_ats(c, row.get("final_site") or domain)
                if hit:
                    cat = "ats_probe"
                    row["ats"], row["ats_url"] = hit
        elif ev.forms:
            cat = "form"
        elif ev.docs:
            cat = "doc"
        elif ev.ats:
            cat = "ats"      # only the source data's careers link, the live site showed nothing else
            row["note"] = "; ".join(filter(None, [row.get("note"), "job board from source data"]))
        elif ev.boards:
            cat = "external_board"
        else:
            cat = ""
            if self.probe and c is not None and domain and home_info is not None:
                hit = self._probe_ats(c, row.get("final_site") or domain)
                if hit:
                    cat = "ats_probe"
                    row["ats"], row["ats_url"] = hit
            if not cat:
                if ev.careers_pages:
                    cat = "no_openings" if ev.no_openings else "careers_page_unclear"
                    if ev.js_pages:
                        row["note"] = "; ".join(filter(None, [row.get("note"), "careers page is built with JavaScript"]))
                elif blocked_home:
                    cat = "blocked"
                elif home_error == "robots":
                    cat = "robots_disallowed"
                elif home_error or home_info is None:
                    cat = "unreachable"
                elif home_info.js_shell:
                    cat = "js_only"
                else:
                    cat = "no_careers_page"
        row["category"] = cat
        return row


# ---------------------------------------------------------------------- CLI

def read_companies(path: str) -> list[Company]:
    out = []
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            out.append(Company(name=(r.get("name") or "").strip(), website=(r.get("website") or "").strip(),
                               careers_url=(r.get("careers_url") or "").strip(),
                               sources=(r.get("sources") or "").strip(), yc_batch=(r.get("yc_batch") or "").strip(),
                               stage=(r.get("stage") or "").strip()))
    return out


def company_key(c: Company) -> str:
    """Stable id for a company, also written as the result's `domain`.

    Its website's registrable domain; without a website, the job-board tenant of its careers link
    ("yc_jobs:swif-ai"), the careers link's domain, or finally its name.
    """
    site = normalize_website(c.website)
    if site:
        return registrable_domain(site)
    hint = _clean_hint(c.careers_url)
    if hint:
        v = match_vendor(hint)
        slug = vendor_slug(hint) if v else ""
        if v and slug:
            return f"{v.name}:{slug}"
        if not v and registrable_domain(hint):
            return registrable_domain(hint)
    return "name:" + _alnum(c.name)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--companies", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--limit", type=int, default=0, help="scan only the first N companies of this shard")
    ap.add_argument("--only", default="", help="comma-separated domains to scan (debugging)")
    ap.add_argument("--no-probe", action="store_true")
    ap.add_argument("--no-robots", action="store_true", help="ignore robots.txt (not recommended)")
    ap.add_argument("--deadline-minutes", type=float, default=0,
                    help="stop starting new companies after this many minutes (rows so far are kept)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    companies = read_companies(args.companies)
    only = {d.strip().lower() for d in args.only.split(",") if d.strip()}
    mine = [c for c in companies if shard_of(company_key(c), args.shards) == args.shard]
    if only:
        mine = [c for c in mine if company_key(c) in only]
    if args.limit:
        mine = mine[: args.limit]
    log.info("shard %s/%s: %s of %s companies", args.shard, args.shards, len(mine), len(companies))

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    fetcher = Fetcher(use_robots=not args.no_robots)
    deadline = time.monotonic() + args.deadline_minutes * 60 if args.deadline_minutes else None
    done = 0
    counts: dict[str, int] = {}
    tmp = args.out + ".part"
    with open(tmp, "w", encoding="utf-8") as out, ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        def job(c: Company) -> dict:
            if deadline and time.monotonic() > deadline:
                return {}
            try:
                return Scanner(fetcher, probe=not args.no_probe).scan(c)
            except Exception as e:  # noqa: BLE001 - one bad site must not stop the shard
                log.exception("scan failed for %s", c.website)
                row = {k: "" for k in FIELDS}
                row.update(name=c.name, website=c.website, domain=company_key(c), category="unreachable",
                           note=f"scanner error: {type(e).__name__}", sources=c.sources,
                           scanned_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
                return row
        futs = [pool.submit(job, c) for c in mine]
        skipped = 0
        for fut in as_completed(futs):
            row = fut.result()
            if not row:
                skipped += 1
                continue
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            done += 1
            counts[row["category"]] = counts.get(row["category"], 0) + 1
            if done % 50 == 0:
                out.flush()
                log.info("%s/%s scanned, %s requests, %s", done, len(mine), fetcher.requests_made,
                         dict(sorted(counts.items(), key=lambda kv: -kv[1])))
    os.replace(tmp, args.out)
    log.info("finished shard %s: %s companies (%s skipped by deadline), %s requests, %s", args.shard, done,
             skipped, fetcher.requests_made, counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
