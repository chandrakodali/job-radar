"""Scratch probe, run from Actions (job systems are not reachable elsewhere). Reads scripts/probe.txt:
    GET <url>             status, final URL, start of the body
    POST <url> <json>     same, with a JSON body
    HOSTS <url>           status, final URL, job-system hints and the outside hosts the page links to
    SNIP <url> <regex>    text around the first matches of a pattern
    WD <tenant>           find a Workday tenant's data centre and the sites its robots.txt names
"""
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from radar.http import USER_AGENT  # noqa: E402

DCS = [1, 3, 5, 10, 12, 102, 103, 105, 107, 108, 109, 501, 502, 503]
SHOW = int(os.environ.get("PROBE_SHOW", "300"))
H = {"User-Agent": USER_AGENT, "Accept": "text/html,application/json,text/plain,*/*;q=0.8"}
HINT = re.compile(r"[\w.-]*(?:myworkdayjobs\.com/[\w-]+(?:/[\w-]+)?|myworkdaysite\.com/[\w/-]+|greenhouse\.io/[\w-]+|lever\.co/[\w.-]+|ashbyhq\.com/[\w.-]+|smartrecruiters\.com/[\w-]+|[\w-]+\.icims\.com|[\w.-]+oraclecloud\.com[\w/.-]*sites/[\w-]+|[\w-]+\.eightfold\.ai|[\w-]+\.avature\.net[\w/-]*|jobvite\.com/[\w-]+|[\w-]+\.taleo\.net[\w/-]*|apply\.workable\.com/[\w-]+|phenompeople|jobs2web|successfactors\.\w+/career\?company=\w+|talentbrew|radancy|ultipro\.com/[\w/-]+|dayforcehcm\.com/[\w/-]+|paylocity\.com/[\w/-]+|adp\.com/[\w/?=&.;-]+|[\w-]+\.applytojob\.com|[\w-]+\.breezy\.hr|[\w-]+\.teamtailor\.com|teamtailor|jobs\.gem\.com/[\w-]+|[\w-]+\.bamboohr\.com|ats\.rippling\.com/[\w-]+|attrax|clinchtalent|[\w-]+\.csod\.com[\w/-]*|pageuppeople\.com[\w/-]*|brassring\.com[\w/?=&.-]*|silkroad|hrmdirect|m-cloud\.io[\w/?=&.-]*|jobsapi[\w.-]+|comeet\.\w+[\w/.-]*|personio\.\w+|recruitee\.com|[\w-]+\.jobs\.net|hirebridge|isolvedhire|applicantpro|paycomonline\.\w+[\w/?=&.-]*|paycor\.com[\w/?=&.-]*|njoyn|cornerstone|softgarden|d\.vinci|rexx|umantis|beesite|jobylon|talentsoft|haufe|workforcenow|myworkday\.com/[\w-]+|kenexa|jobs\.sap\.com|careers-page\.com/[\w-]+|zohorecruit\.\w+[\w/-]*|freshteam|trinethire\.com[\w/-]*|applytojob|careerplug|ceipal|jobdiva[\w./?=&-]*|bullhorn[\w./-]*)", re.I)


def get(url, **kw):
    return requests.get(url, headers=H, timeout=(8, 25), **kw)


def board_check(ats: str, board: str) -> str:
    """Run the radar's own reader on a board: jobs listed, US ones, and how many titles fit the profile."""
    from collections import Counter
    import yaml
    from radar import deep, robots
    from radar.match import Rules, classify_location, job_category
    from radar.models import Board
    cfg = yaml.safe_load(open(os.path.join(ROOT, "profiles", "automotive", "config.yaml")))
    rules = Rules.from_config(cfg)
    b = Board(ats, board, board, focus=True)
    jobs = deep.base_list(b, {"full_list": ["workday"], "full_max_pages": 150, "list_max_pages": 10,
                              "list_max_age_days": 150, "sitemap_max_jobs": 5000})
    us = [j for j in jobs if classify_location(j.location, j.countries) != "non_us"]
    cats = Counter(job_category(j, b, rules) or "-" for j in us)
    core = [j.title for j in us if job_category(j, b, rules) == "core"][:8]
    return (f"BOARD {ats} {board}: listed={len(jobs)} complete={getattr(jobs, 'complete', False)} "
            f"us_or_unknown={len(us)} titles={dict(cats)}\n   core: {core}")


def one(line: str) -> str:
    parts = line.split(None, 2)
    kind = parts[0].upper()
    try:
        if kind == "BOARD":
            return board_check(parts[1], parts[2] if len(parts) > 2 else "")
        if kind in ("WD", "WDX"):
            t = parts[1]
            out = []
            for n in (DCS if kind == "WD" else [2, 4, 6, 7, 8, 9, 11, 13, 14, 15, 101, 104, 106, 110, 111, 112, 201, 202, 203, 204]):
                try:
                    r = requests.get(f"https://{t}.wd{n}.myworkdayjobs.com/robots.txt", headers=H, timeout=(3, 8), allow_redirects=False)
                except Exception:
                    continue
                if r.status_code == 200:
                    sites = sorted(set(re.findall(r"(?:Allow|Disallow): /([^/\s]+)/", r.text)) - {"refreshFacet"})
                    out.append(f"wd{n} sites={sites}")
            return f"WD {t}: " + (" | ".join(out) or "-")
        url = parts[1]
        if kind in ("POST", "PKEYS"):
            r = requests.post(url, json=json.loads(parts[2]) if len(parts) > 2 else {}, headers=H, timeout=(8, 25))
        else:
            r = get(url)
        text = r.text[:3000000]
        head = f"{kind} {url} -> {r.status_code} {r.url if r.url != url else ''} len={len(r.text)}"
        if kind == "HOSTS":
            hints = sorted(set(m.group(0).lower() for m in HINT.finditer(text.replace("\\/", "/"))))[:14]
            own = ".".join((urlsplit(r.url).hostname or "").split(".")[-2:])
            hosts = sorted({h.lower() for h in re.findall(r"https?://([\w.-]+)", text.replace("\\/", "/"))
                            if own not in h.lower() and not re.search(
                                r"google|gstatic|facebook|twitter|linkedin|youtube|instagram|w3\.org|schema\.org|cloudflare|jquery|jsdelivr|"
                                r"bootstrapcdn|fontawesome|typekit|cookielaw|onetrust|adobe|doubleclick|wp\.com|gravatar|vimeo|unpkg|"
                                r"cdnjs|apple\.com|microsoft|bing|tiktok|pinterest|x\.com|xing|hotjar|segment|newrelic|sentry|licdn|"
                                r"akamai|cloudfront|amazonaws|azureedge|trustarc|usercentrics|cookiebot|hubspot|hsforms|wordpress|"
                                r"glassdoor|indeed|wikipedia|maps|fonts|analytics|tagmanager|optimizely|qualtrics", h, re.I)})[:30]
            title = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
            return f"{head}\n   title={re.sub(r'\\s+', ' ', title.group(1))[:80] if title else ''}\n   hints={hints}\n   hosts={hosts}"
        if kind in ("KEYS", "PKEYS"):
            def shape(x, d=0):
                if isinstance(x, dict):
                    return {k: shape(v, d + 1) for k, v in list(x.items())[:60]} if d < 6 else "{..}"
                if isinstance(x, list):
                    return [shape(x[0], d + 1), f"len={len(x)}"] if x else []
                return str(x)[:70]
            return head + "\n   " + json.dumps(shape(r.json()), ensure_ascii=False)[:2600]
        if kind == "SNIP":
            pat = re.compile(parts[2], re.I | re.S)
            flat = re.sub(r"\s+", " ", text)
            bits = [flat[max(0, m.start() - 150): m.end() + SHOW] for m in list(pat.finditer(flat))[:3]]
            return head + "".join(f"\n   >> {b}" for b in bits) + ("" if bits else "\n   >> no match")
        return f"{head}\n   {re.sub(r'\\s+', ' ', text[:200000])[:SHOW]}"
    except Exception as e:
        return f"{line[:120]} -> {type(e).__name__}: {str(e)[:120]}"


def main() -> int:
    from datetime import date
    from radar import robots
    robots.install(robots.Robots({}, date.today().isoformat()))   # the radar's readers obey robots.txt here too
    path = os.path.join(ROOT, "scripts", "probe.txt")
    lines = [l.strip() for l in open(path, encoding="utf-8") if l.strip() and not l.startswith("#")]
    with ThreadPoolExecutor(16) as pool:
        for res in pool.map(one, lines):
            print(res, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
