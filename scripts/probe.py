"""Scratch probe, run from Actions (job systems are not reachable elsewhere). Reads scripts/probe.txt:
    GET <url>            status, final URL, start of the body
    POST <url> <json>    same, with a JSON body
    WD <tenant>          look for a Workday tenant on every data centre; prints the sites its robots.txt names
"""
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from radar.http import session  # noqa: E402

DCS = [1, 2, 3, 5, 10, 12, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 201, 202, 501, 502, 503]
SHOW = int(os.environ.get("PROBE_SHOW", "500"))


def one(line: str) -> str:
    parts = line.split(None, 2)
    kind = parts[0].upper()
    try:
        if kind == "WD":
            t = parts[1]
            out = []
            for n in DCS:
                u = f"https://{t}.wd{n}.myworkdayjobs.com/robots.txt"
                try:
                    r = session().get(u, timeout=(6, 12), allow_redirects=False)
                except Exception as e:
                    out.append(f"wd{n}: {type(e).__name__}")
                    continue
                sites = sorted(set(re.findall(r"myworkdayjobs\.com/([^/\s]+)/siteMap", r.text, re.I)))
                if r.status_code == 200 and (sites or "Disallow" in r.text):
                    out.append(f"wd{n}: 200 sites={sites} len={len(r.text)}")
                elif r.status_code not in (404, 500, 502, 503):
                    out.append(f"wd{n}: {r.status_code} {r.headers.get('location', '')[:80]}")
            return f"WD {t}: " + (" | ".join(out) or "nothing")
        url = parts[1]
        if kind == "POST":
            r = session().post(url, json=json.loads(parts[2]) if len(parts) > 2 else {}, timeout=(10, 30))
        else:
            r = session().get(url, timeout=(10, 30), headers={"Accept": "text/html,application/json,*/*;q=0.8"})
        body = re.sub(r"\s+", " ", r.text[:200000])
        ats = sorted(set(re.findall(r"[\w.-]*(?:myworkdayjobs\.com/[\w-]+(?:/[\w-]+)?|myworkdaysite\.com/[\w/-]+|greenhouse\.io/[\w-]+|lever\.co/[\w.-]+|ashbyhq\.com/[\w.-]+|smartrecruiters\.com/[\w-]+|[\w-]+\.icims\.com|[\w.-]+oraclecloud\.com[\w/.-]*sites/[\w-]+|[\w-]+\.eightfold\.ai|[\w-]+\.avature\.net|jobvite\.com/[\w-]+|[\w-]+\.taleo\.net|apply\.workable\.com/[\w-]+|phenompeople|jobs2web|successfactors|talentbrew|radancy|ultipro\.com/[\w/-]+|dayforcehcm\.com/[\w/-]+|paylocity\.com/[\w/-]+|adp\.com/[\w/?=&.-]+|[\w-]+\.applytojob\.com|[\w-]+\.breezy\.hr|[\w-]+\.teamtailor\.com|jobs\.gem\.com/[\w-]+|[\w-]+\.bamboohr\.com|ats\.rippling\.com/[\w-]+|recruiting\.paylocity|attrax|clinchtalent|csod\.com/[\w/-]+|pageuppeople|jobs\.jobvite|brassring|kenexa|silkroad|hrmdirect|ukg)", r.text[:3000000], re.I)))[:12]
        return f"{kind} {url}\n   -> {r.status_code} {r.url if r.url != url else ''} len={len(r.text)} ats={ats}\n   {body[:SHOW]}"
    except Exception as e:
        return f"{line}\n   -> {type(e).__name__}: {str(e)[:160]}"


def main() -> int:
    path = os.path.join(ROOT, "scripts", "probe.txt")
    lines = [l.strip() for l in open(path, encoding="utf-8") if l.strip() and not l.startswith("#")]
    with ThreadPoolExecutor(12) as pool:
        for res in pool.map(one, lines):
            print(res, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
