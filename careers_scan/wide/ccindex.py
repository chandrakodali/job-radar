"""Career pages across the whole web, from Common Crawl's URL index (Parquet files; no API key).

    python -m careers_scan.wide.ccindex paths --crawl latest --out paths.txt
    python -m careers_scan.wide.ccindex sweep --paths paths.txt --shard 0 --shards 10 --out out/cc-0.csv.gz
    python -m careers_scan.wide.ccindex merge --inputs out --out cc-candidates.csv.gz

Each index file is downloaded whole (one request, polite retries), queried locally with DuckDB, then
deleted. That is the access pattern Common Crawl recommends (https://status.commoncrawl.org/).
Output: one row per site (registered domain) with its best careers-page URL.
"""
from __future__ import annotations

import argparse
import csv
import glob
import gzip
import logging
import os
import re
import sys
import tempfile
import time
from datetime import date, timedelta

import requests

from ..detect import NON_CAREER_PATH_RE

log = logging.getLogger("careers_scan.wide.ccindex")
DATA = "https://data.commoncrawl.org/"
COLLINFO = "https://index.commoncrawl.org/collinfo.json"
UA = "job-radar-careers-scan/1.0 (+https://github.com/chandrakodali/job-radar)"
FIELDS = ["domain", "url", "score", "alt_url", "detail_url", "n_detail", "n_landing", "fetch_time", "warc"]

# A path segment that names a careers page. Lower-case; valid for both RE2 (DuckDB) and Python.
SEG = (r"careers?|career[-_]?(?:page|opportunities|openings|center|centre)|careers[-_]?page|"
       r"jobs?|job[-_]?(?:openings|opportunities|listings|postings|board|vacancies)|"
       r"join[-_]?(?:us|our[-_]?team|the[-_]?team)|work[-_]?(?:with|for)[-_]?us|"
       r"working[-_]?(?:at|with|here)(?:[-_][a-z0-9-]{1,20})?|employment(?:[-_]opportunities)?|"
       r"hiring|(?:we[-_]?are|were|now)[-_]?hiring|open[-_]?(?:positions|roles|jobs)|"
       r"(?:current[-_]?)?openings|vacancies")
_EXT = r"(?:\.(?:html?|php|aspx?|jsp|cfm))?"
_PART = r"[a-z0-9._~%-]"
# up to two leading segments, the careers segment, optionally one more segment (a single job's page)
PATH_RE = rf"^/(?:{_PART}{{1,30}}/){{0,2}}(?:{SEG}){_EXT}(?:/{_PART}{{1,100}})?/?$"
HOST_RE = r"^(?:careers?|jobs|hiring)\."
_SPLIT = re.compile(rf"^(?P<lead>/(?:{_PART}{{1,30}}/){{0,2}})(?P<seg>{SEG}){_EXT}(?:/(?P<tail>{_PART}{{1,100}}))?/?$")
_STRONG = {"careers", "career", "jobs", "job"}
_JUNK_TAIL = re.compile(r"^(?:page|p|feed|rss|search|login|register|apply|index\.(?:html?|php))$")
_REJECT = re.compile(r"/(?:category|tag|tags|author|topic|forum|wiki|students?|alumni|courses?|shop|store|search)(?:/|$)")

# Country-code TLDs are skipped (US focus) except the ones US companies commonly use.
CC_TLDS_OK = ("us", "co", "io", "ai", "me", "tv", "ly", "so", "to", "fm", "gg", "sh", "vc", "cc", "ws")
# Job boards, tracking-system hosts and big platforms: never a company's own careers page.
DENY = frozenset("""
greenhouse.io lever.co ashbyhq.com workable.com bamboohr.com smartrecruiters.com myworkdayjobs.com icims.com
jobvite.com breezy.hr applytojob.com recruitee.com teamtailor.com taleo.net ultipro.com paylocity.com adp.com
paycomonline.net successfactors.com oraclecloud.com zohorecruit.com freshteam.com jazzhr.com applicantpro.com
careerplug.com isolvedhire.com indeed.com glassdoor.com linkedin.com ziprecruiter.com monster.com
careerbuilder.com simplyhired.com dice.com craigslist.org snagajob.com jooble.org talent.com lensa.com
salary.com flexjobs.com wellfound.com angel.co builtin.com usajobs.gov governmentjobs.com higheredjobs.com
idealist.org theladders.com adzuna.com careerjet.com jobrapido.com jora.com facebook.com twitter.com x.com
instagram.com youtube.com reddit.com medium.com pinterest.com quora.com wikipedia.org github.com apple.com
google.com amazon.com microsoft.com
""".split())


def sql_for(parquet_path: str, cols: set[str]) -> str:
    """The query for one index file. `cols` = column names present (the schema gained columns over time)."""
    dom = ("coalesce(url_host_private_domain, url_host_registered_domain)" if "url_host_private_domain" in cols
           else "url_host_registered_domain")
    lang = "(content_languages IS NULL OR content_languages LIKE 'eng%')" if "content_languages" in cols else "TRUE"
    tlds = ",".join(f"'{t}'" for t in CC_TLDS_OK)
    p = parquet_path.replace("'", "''")
    return f"""
      SELECT url, url_host_name AS host, {dom} AS dom, lower(url_path) AS path, CAST(fetch_time AS VARCHAR) AS fetch_time,
             warc_filename, warc_record_offset, warc_record_length
      FROM read_parquet('{p}')
      WHERE fetch_status = 200
        AND content_mime_detected IN ('text/html', 'application/xhtml+xml')
        AND (url_query IS NULL OR url_query = '')
        AND length(url_path) <= 140
        AND {lang}
        AND (length(url_host_tld) > 2 OR url_host_tld IN ({tlds}))
        AND url_host_tld NOT LIKE 'xn--%'
        AND (regexp_matches(lower(url_path), '{PATH_RE}')
             OR (url_path = '/' AND regexp_matches(url_host_name, '{HOST_RE}')))
    """


def classify_path(host: str, dom: str, path: str):
    """('landing' | 'detail', score, landing path) for a lower-case URL path; None if not a careers URL."""
    if path in ("", "/"):
        return ("landing", 9, "/") if re.match(HOST_RE, host) else None
    m = _SPLIT.match(path)
    if not m or NON_CAREER_PATH_RE.search(path) or _REJECT.search(path):
        return None
    lead, seg, tail = m.group("lead"), m.group("seg"), m.group("tail")
    score = (10 if seg in _STRONG else 8) - 2 * (lead.count("/") - 1)
    if seg in ("careers", "career"):
        score += 1
    if host in (dom, "www." + dom) or re.match(HOST_RE, host):
        score += 1
    if not tail:
        return "landing", score, path
    if _JUNK_TAIL.match(tail):
        return None
    p = path.rstrip("/")
    return "detail", score, p[: p.rfind("/")]


def _k(url: str) -> str:
    return re.sub(r"^https?://(?:www\.)?", "", (url or "").lower()).rstrip("/")


def _new(dom: str) -> dict:
    return {"domain": dom, "url": "", "score": -1, "alt_url": "", "alt_score": -1, "detail_url": "",
            "n_detail": 0, "n_landing": 0, "fetch_time": "", "warc": "", "der_url": "", "der_score": -1}


def _offer(d: dict, url: str, score: int, fetch_time: str = "", warc: str = "") -> None:
    """Offer a landing URL to a candidate: the best becomes `url`, the runner-up `alt_url`."""
    if not url:
        return
    if _k(url) == _k(d["url"]):
        if url.startswith("https") and not d["url"].startswith("https"):
            d.update(url=url, fetch_time=fetch_time or d["fetch_time"], warc=warc or d["warc"])
        return
    if (score, -len(url)) > (d["score"], -len(d["url"])):
        if d["url"]:
            d["alt_url"], d["alt_score"] = d["url"], d["score"]
        d.update(url=url, score=score, fetch_time=fetch_time, warc=warc)
    elif _k(url) != _k(d["alt_url"]) and (score, -len(url)) > (d["alt_score"], -len(d["alt_url"])):
        d["alt_url"], d["alt_score"] = url, score


def add_rows(out: dict[str, dict], rows) -> None:
    """Fold index rows (dicts with url, host, dom, path, fetch_time, warc_*) into {domain: candidate}."""
    for r in rows:
        dom = (r.get("dom") or "").lower().strip(".")
        host = (r.get("host") or "").lower()
        url = r.get("url") or ""
        if not dom or not host or not url or dom in DENY:
            continue
        c = classify_path(host, dom, r.get("path") or "/")
        if not c:
            continue
        kind, score, base = c
        d = out.get(dom)
        if d is None:
            d = out[dom] = _new(dom)
        if kind == "detail":
            d["n_detail"] += 1
            d["detail_url"] = d["detail_url"] or url
            if score - 1 > d["der_score"]:
                d["der_url"], d["der_score"] = f'{url.split("://", 1)[0]}://{host}{base}', score - 1
            continue
        d["n_landing"] += 1
        warc = f'{r.get("warc_filename") or ""} {r.get("warc_record_offset") or 0} {r.get("warc_record_length") or 0}'
        _offer(d, url, score, str(r.get("fetch_time") or "")[:10], warc)


def finish(out: dict[str, dict]) -> dict[str, dict]:
    """A site seen only through single-job pages gets the landing page those pages sit under."""
    for d in out.values():
        if not d["url"] and d["der_url"]:
            d["url"], d["score"] = d["der_url"], d["der_score"]
    return {k: d for k, d in out.items() if d["url"]}


def _norm(d: dict) -> dict:
    n = _new(d["domain"])
    for k in ("url", "alt_url", "detail_url", "fetch_time", "warc"):
        n[k] = d.get(k) or ""
    for k in ("score", "n_detail", "n_landing"):
        n[k] = int(d.get(k) or 0)
    n["alt_score"] = int(d.get("alt_score") or (n["score"] - 1 if n["alt_url"] else -1))
    return n


def merge_into(out: dict[str, dict], d: dict) -> None:
    """Fold one candidate (from another index file, shard or crawl; a dict or a CSV row) into `out`."""
    d = _norm(d)
    cur = out.get(d["domain"])
    if cur is None:
        out[d["domain"]] = d
        return
    cur["n_detail"] += d["n_detail"]
    cur["n_landing"] += d["n_landing"]
    cur["detail_url"] = cur["detail_url"] or d["detail_url"]
    _offer(cur, d["url"], d["score"], d["fetch_time"], d["warc"])
    _offer(cur, d["alt_url"], d["alt_score"])


def sweep_file(con, parquet_path: str) -> dict[str, dict]:
    """Query one local index file with DuckDB; returns {domain: candidate}."""
    p = parquet_path.replace("'", "''")
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{p}')").fetchall()}
    cur = con.execute(sql_for(parquet_path, cols))
    names = [c[0] for c in cur.description]
    out: dict[str, dict] = {}
    while True:
        chunk = cur.fetchmany(50_000)
        if not chunk:
            break
        add_rows(out, (dict(zip(names, row)) for row in chunk))
    return finish(out)


def download(url: str, dest: str, session: requests.Session, budget_s: float = 1800.0) -> None:
    """Download a whole file with resume. At Common Crawl 403/429/5xx mean 'slow down': wait and retry."""
    tmp, t0, attempt = dest + ".part", time.monotonic(), 0
    while True:
        have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        try:
            with session.get(url, headers={"Range": f"bytes={have}-"} if have else {}, stream=True,
                             timeout=(15, 90)) as r:
                if r.status_code == 416 and have:
                    break                                   # nothing left to fetch
                if r.status_code not in (200, 206):
                    raise OSError(f"HTTP {r.status_code}")
                expect, got = int(r.headers.get("Content-Length") or 0), 0
                with open(tmp, "ab" if r.status_code == 206 else "wb") as fh:
                    for chunk in r.iter_content(1 << 20):
                        fh.write(chunk)
                        got += len(chunk)
                if expect and got < expect:
                    raise OSError("short read")
            break
        except (requests.RequestException, OSError) as e:
            attempt += 1
            if time.monotonic() - t0 > budget_s:
                raise TimeoutError(f"gave up on {url}: {e}") from e
            wait = float(min(60, 2 ** min(attempt, 6)))
            log.warning("%s (attempt %s), retrying in %.0fs: %s", e, attempt, wait, url.rsplit("/", 1)[-1])
            time.sleep(wait)
    os.replace(tmp, dest)


def _exists(session: requests.Session, url: str) -> bool:
    try:
        with session.get(url, stream=True, timeout=30) as r:
            return r.status_code == 200
    except requests.RequestException:
        return False


def latest_crawls(session: requests.Session, n: int = 1) -> list[str]:
    """Newest crawl ids, newest first. Probes CC-MAIN-<year>-<week> when the index server is busy."""
    try:
        r = session.get(COLLINFO, timeout=30)
        ids = [c["id"] for c in r.json() if str(c.get("id", "")).startswith("CC-MAIN-")][:n]
        if r.status_code == 200 and ids:
            return ids
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
        pass
    ids, d = [], date.today()
    for _ in range(40):                                     # weekly ids, about nine months back
        y, w, _dow = d.isocalendar()
        cid = f"CC-MAIN-{y}-{w:02d}"
        if _exists(session, f"{DATA}crawl-data/{cid}/cc-index-table.paths.gz"):
            ids.append(cid)
            if len(ids) >= n:
                break
        d -= timedelta(days=7)
        time.sleep(1.0)
    return ids


def index_paths(session: requests.Session, crawl: str) -> list[str]:
    """Relative paths of the crawl's URL-index files that describe successfully fetched pages."""
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "paths.gz")
        download(f"{DATA}crawl-data/{crawl}/cc-index-table.paths.gz", f, session, budget_s=600)
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if "/subset=warc/" in ln]


def write_csv(path: str, cands) -> int:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    n = 0
    with gzip.open(path + ".tmp", "wt", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for d in sorted(cands, key=lambda d: d["domain"]):
            w.writerow(d)
            n += 1
    os.replace(path + ".tmp", path)
    return n


def read_csv(path: str):
    with gzip.open(path, "rt", newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("paths", help="list the index files of one crawl")
    p.add_argument("--crawl", default="latest", help="crawl id (CC-MAIN-2026-43) or 'latest'")
    p.add_argument("--out", required=True)
    s = sub.add_parser("sweep", help="download and query one shard of the index files")
    s.add_argument("--paths", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--shard", type=int, default=0)
    s.add_argument("--shards", type=int, default=1)
    s.add_argument("--limit-files", type=int, default=0)
    s.add_argument("--deadline-minutes", type=float, default=0)
    m = sub.add_parser("merge", help="combine shard outputs into one candidate list")
    m.add_argument("--inputs", required=True, help="directory holding *.csv.gz shard outputs")
    m.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    session = requests.Session()
    session.headers["User-Agent"] = UA

    if a.cmd == "paths":
        crawl = a.crawl
        if crawl == "latest":
            ids = latest_crawls(session)
            if not ids:
                log.error("could not find the newest crawl; pass --crawl CC-MAIN-YYYY-WW")
                return 1
            crawl = ids[0]
        paths = index_paths(session, crawl)
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write("\n".join(paths) + "\n")
        print(crawl)                                        # workflows read the crawl id from stdout
        log.info("%s: %s index files", crawl, len(paths))
        return 0 if paths else 1

    if a.cmd == "sweep":
        import duckdb
        with open(a.paths, encoding="utf-8") as fh:
            mine = [ln.strip() for ln in fh if ln.strip()][a.shard::max(a.shards, 1)]
        if a.limit_files:
            mine = mine[: a.limit_files]
        deadline = time.monotonic() + a.deadline_minutes * 60 if a.deadline_minutes else None
        con = duckdb.connect()
        out: dict[str, dict] = {}
        done: list[str] = []
        with tempfile.TemporaryDirectory(prefix="ccidx-") as td:
            local = os.path.join(td, "part.parquet")
            for i, rel in enumerate(mine):
                if deadline and time.monotonic() > deadline:
                    log.warning("deadline reached with %s files left", len(mine) - i)
                    break
                try:
                    download(DATA + rel, local, session)
                    got = sweep_file(con, local)
                except Exception as e:  # noqa: BLE001 - one bad file must not stop the shard
                    log.error("skipped %s: %s: %s", rel, type(e).__name__, e)
                    continue
                finally:
                    for f in (local, local + ".part"):
                        if os.path.exists(f):
                            os.remove(f)
                for d in got.values():
                    merge_into(out, d)
                done.append(rel)
                log.info("%s/%s files, %s sites so far (+%s)", i + 1, len(mine), len(out), len(got))
        n = write_csv(a.out, out.values())
        with open(a.out + ".done", "w", encoding="utf-8") as fh:
            fh.write("\n".join(done) + ("\n" if done else ""))
        log.info("shard %s: %s of %s files read, %s sites", a.shard, len(done), len(mine), n)
        return 0 if done or not mine else 1

    out = {}
    files = sorted(glob.glob(os.path.join(a.inputs, "**", "*.csv.gz"), recursive=True))
    for f in files:
        for d in read_csv(f):
            merge_into(out, d)
    n = write_csv(a.out, out.values())
    log.info("merged %s files into %s sites", len(files), n)
    return 0 if files else 1


if __name__ == "__main__":
    sys.exit(main())
