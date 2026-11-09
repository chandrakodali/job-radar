"""Live test of the extra job-system readers on sample companies (run in GitHub Actions)."""
import json, re, sys, time, traceback, os
from urllib.parse import urlsplit, parse_qs
import requests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from radar import ats_more as A, robots
from radar.http import FetchError, get_text
from radar.models import Board

SAMPLES = {
    "icims": ["careers-ebscoind.icims.com", "careers-here.icims.com", "external-canoncareers.icims.com"],
    "jazzhr": ["k1im", "bsa", "esvolta"],
    "jobvite": ["nutanix", "inogen", "asus"],
    "sfrmk": ["careers.qorvo.com", "jobs.nucor.com", "jobs.pseg.com"],
    "paylocity": ["job:4056745", "job:4324694"],
    "taleo": ["kp|external", "cinfin|ex", "lbl|5"],
    "avature": ["delta.avature.net|en_US/careers", "twosigma.avature.net|careers", "koch.avature.net|en_US/careers"],
    "phenom": ["jobs.baesystems.com|global/en"],
    "eightfold": ["qualcomm|qualcomm.com", "eaton|eaton.com", "johndeere|deere.com"],
    "jibe": ["careers.rivian.com", "jobs.keysight.com"],
    "breezy": ["navaide", "vetsez", "lumerate"],
    "pinpoint": ["impulsespace", "franklin-electric"],
    "teamtailor": ["noda-1720000970.teamtailor.com"],
    "dover": ["mindsmith"],
    "gem": ["albacore"],
    "dayforce": ["texasfarm|candidateportal"],
    "manatal": ["q2hrsolutions", "globalchannelmanagement", "permhunt"],
}
# Systems without samples: take a few boards from the Common Crawl index.
CC = {
    "ukg": ("recruiting.ultipro.com/*", r"recruiting\d?\.ultipro\.com/([A-Za-z0-9]+)/JobBoard/([0-9a-fA-F-]{36})",
            lambda m, u: f"{urlsplit(u).netloc}|{m.group(1)}|{m.group(2)}"),
    "adp": ("workforcenow.adp.com/mascsr/*", r"cid=([0-9a-fA-F-]{36})",
            lambda m, u: m.group(1) + "|" + (parse_qs(urlsplit(u).query).get("ccId") or ["19000101_000001"])[0]),
    "hireology": ("careers.hireology.com/*", r"careers\.hireology\.com/([a-z0-9_-]+)", lambda m, u: m.group(1)),
    "oorwin": ("*.oorwin.com", r"https?://([a-z0-9-]+)\.oorwin\.com/careers", lambda m, u: m.group(1)),
    "teamtailor": ("*.teamtailor.com", r"https?://([a-z0-9-]+\.teamtailor\.com)/jobs/\d+", lambda m, u: m.group(1)),
    "gem": ("jobs.gem.com/*", r"jobs\.gem\.com/([a-z0-9_-]+)/", lambda m, u: m.group(1)),
    "dover": ("app.dover.com/jobs/*", r"app\.dover\.com/jobs/([a-z0-9-]+)/?$", lambda m, u: m.group(1)),
    "breezy": ("*.breezy.hr", r"https?://([a-z0-9-]+)\.breezy\.hr/p/", lambda m, u: m.group(1)),
    "dayforce": ("jobs.dayforcehcm.com/*", r"jobs\.dayforcehcm\.com/[a-z]{2}-[A-Z]{2}/([a-z0-9_-]+)/([A-Za-z0-9_-]+)",
                 lambda m, u: f"{m.group(1)}|{m.group(2)}"),
}
TECH = re.compile(r"devops|cloud|site reliability|\bsre\b|platform|infrastructure|systems? admin|network|engineer", re.I)


def cc_samples(ats, want=3):
    pat, rx, mk = CC[ats]
    s = requests.Session(); s.headers["User-Agent"] = "job-radar-probe/1.0 (+https://github.com/chandrakodali/job-radar)"
    try:
        api = s.get("https://index.commoncrawl.org/collinfo.json", timeout=60).json()[0]["cdx-api"]
        r = s.get(api, params={"url": pat, "output": "json", "fl": "url", "pageSize": "1", "page": "0"}, timeout=120)
    except Exception as e:
        print(f"  CC lookup failed: {e}"); return []
    out = []
    lines = r.text.splitlines()
    print(f"  CC {pat}: HTTP {r.status_code}, {len(lines)} urls, first={lines[0][:120] if lines else ''}")
    for line in lines:
        try:
            u = json.loads(line)["url"]
        except Exception:
            continue
        m = re.search(rx, u, re.I)
        if m:
            b = mk(m, u)
            if b not in out:
                out.append(b)
        if len(out) >= want:
            break
    time.sleep(1)
    return out


def paylocity_guid(jobid):
    html = get_text(f"https://recruiting.paylocity.com/Recruiting/Jobs/Details/{jobid}")
    guids = re.findall(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", html)
    print(f"  paylocity page {len(html)} chars; guids: {sorted(set(guids))[:5]}")
    for k in ("CompanyId", "companyId", "ModuleId", "Jobs/All", "jobs/all"):
        i = html.find(k)
        if i >= 0:
            print(f"    ...{html[max(0, i-80):i+120]!r}")
    m = re.search(r"/Recruiting/Jobs/All/([0-9a-fA-F-]{36})", html, re.I)
    return m.group(1) if m else (guids[0] if guids else None)


def main():
    only = set(sys.argv[1].split(",")) if len(sys.argv) > 1 and sys.argv[1] else set()
    robots.install(robots.Robots({}, time.strftime("%Y-%m-%d")))
    samples = dict(SAMPLES)
    for ats in CC:
        if only and ats not in only:
            continue
        extra = cc_samples(ats)
        samples[ats] = (samples.get(ats) or []) + [x for x in extra if x not in (samples.get(ats) or [])]
    summary = {}
    for ats, boards in samples.items():
        if only and ats not in only:
            continue
        print(f"\n===== {ats} =====")
        ok = 0
        for board in boards[:4]:
            try:
                if ats == "paylocity" and board.startswith("job:"):
                    g = paylocity_guid(board[4:])
                    print(f"  resolved {board} -> {g}")
                    if not g:
                        continue
                    board = g
                t0 = time.time()
                jobs = A.LISTERS[ats](Board(ats=ats, board=board, company=board, named=False))
                dt = time.time() - t0
                us = [j for j in jobs if TECH.search(j.title)]
                print(f"  {board}: {len(jobs)} jobs in {dt:.1f}s; tech-ish {len(us)}")
                for j in jobs[:2]:
                    print(f"    - {j.title[:70]!r} | {j.location[:50]!r} | {j.posted_at} | {j.url[:110]}")
                if jobs:
                    ok += 1
                    pick = (us or jobs)[0]
                    if ats in A.DETAILERS:
                        try:
                            A.DETAILERS[ats](pick)
                            print(f"    detail: title={pick.title[:60]!r} posted={pick.posted_at} loc={pick.location[:50]!r} "
                                  f"countries={pick.countries[:3]} desc={len(pick.description)} chars company={pick.company[:40]!r}")
                        except Exception as e:
                            print(f"    detail FAILED: {type(e).__name__}: {e}")
            except Exception as e:
                print(f"  {board}: FAILED {type(e).__name__}: {str(e)[:200]}")
                if not isinstance(e, FetchError):
                    traceback.print_exc(limit=3)
        summary[ats] = f"{ok}/{min(4, len(boards))}"
    print("\nSUMMARY", json.dumps(summary))


if __name__ == "__main__":
    main()
