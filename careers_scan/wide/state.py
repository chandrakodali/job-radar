"""Maintain the wide careers sweep's sites, roles and crawl metadata."""
import argparse
import csv
import gzip
import json
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from careers_scan.roles import role_key

APPLY_CATEGORIES = {"email", "form", "doc"}
FAILURE_CATEGORIES = {"blocked", "unreachable", "js_only", "robots_disallowed"}
WORK_FIELDS = ("domain", "url", "alt_url", "warc", "name")
RESULT_FIELDS = ("category", "via", "name", "careers_url", "apply_emails", "apply_phrase",
                 "form_url", "doc_url", "ats", "roles", "tech", "us", "note")
COUNT_FIELDS = ("tier", "category", "us", "via")

SCHEMA = """
CREATE TABLE IF NOT EXISTS site(
  domain TEXT PRIMARY KEY, url TEXT NOT NULL, alt_url TEXT DEFAULT '', detail_url TEXT DEFAULT '',
  n_detail INTEGER DEFAULT 0, cc_score INTEGER DEFAULT 0, src TEXT DEFAULT '', warc TEXT DEFAULT '',
  first_seen TEXT DEFAULT '', checked TEXT DEFAULT '', fails INTEGER DEFAULT 0,
  category TEXT DEFAULT '', via TEXT DEFAULT '', name TEXT DEFAULT '', careers_url TEXT DEFAULT '',
  apply_emails TEXT DEFAULT '', apply_phrase TEXT DEFAULT '', form_url TEXT DEFAULT '', doc_url TEXT DEFAULT '',
  ats TEXT DEFAULT '', roles TEXT DEFAULT '[]', tech INTEGER DEFAULT 0, us TEXT DEFAULT '',
  tier INTEGER DEFAULT 9, note TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS site_due ON site(tier, checked);
CREATE TABLE IF NOT EXISTS role(
  key TEXT PRIMARY KEY, domain TEXT, title TEXT, url TEXT, location TEXT DEFAULT '', emails TEXT DEFAULT '',
  apply_url TEXT DEFAULT '', first_seen TEXT, last_seen TEXT, baseline INTEGER DEFAULT 0, assessed TEXT DEFAULT '{}');
CREATE INDEX IF NOT EXISTS role_domain ON role(domain);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def read_csv(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", newline="") as fh:
        yield from csv.DictReader(fh)


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def candidates(db, path, src, skip_csv=None, today=None):
    today = today or date.today()
    skip = {row["domain"] for row in read_csv(str(skip_csv))} if skip_csv else set()
    count = 0
    with db:
        for row in read_csv(str(path)):
            domain = row["domain"].strip().lower()
            url = row["url"].strip()
            if not domain or not url or domain in skip:
                continue
            score = int(row.get("score") or 0)
            details = int(row.get("n_detail") or 0)
            old = db.execute("SELECT src, cc_score FROM site WHERE domain = ?", (domain,)).fetchone()
            urls = (url, row.get("alt_url") or "", row.get("detail_url") or "", row.get("warc") or "")
            if old is None:
                db.execute("""INSERT INTO site
                    (domain, url, alt_url, detail_url, warc, cc_score, n_detail, src, first_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (domain, *urls, score, details, src, today.isoformat()))
            else:
                sources = old["src"].split("|") if old["src"] else []
                if src not in sources:
                    db.execute("UPDATE site SET n_detail = n_detail + ?, src = ? WHERE domain = ?",
                               (details, "|".join([*sources, src]), domain))
                if score > old["cc_score"]:
                    db.execute("""UPDATE site SET url = ?, alt_url = ?, detail_url = ?, warc = ?, cc_score = ?
                        WHERE domain = ?""", (*urls, score, domain))
            count += 1
        if src.startswith("cc:"):
            db.execute("INSERT OR IGNORE INTO meta VALUES (?, ?)", (src, today.isoformat()))
    return count


def canary(path, known):
    expected = {row["domain"] for row in read_csv(str(known)) if row["category"] == "email"}
    found = {row["domain"] for row in read_csv(str(path)) if row["domain"] in expected}
    share = 100 * len(found) / len(expected) if expected else 0
    return f"sweep found {len(found)} of {len(expected)} known apply-by-email sites ({share:.1f}%)"


def worklist(db, out, maximum, today=None):
    today = today or date.today()
    rows = db.execute("""SELECT domain, url, alt_url, warc, name FROM site
        WHERE CASE
          WHEN tier = 9 THEN 1
          WHEN tier = 1 THEN checked <= :daily
          WHEN tier = 2 THEN checked <= :weekly
          WHEN tier IN (3,5) AND (tier = 3 OR fails < 3) THEN checked <= :monthly
          ELSE checked <= :halfyear
        END
        ORDER BY CASE tier WHEN 1 THEN 0 WHEN 9 THEN 1 ELSE 2 END,
          CASE WHEN tier = 9 THEN n_detail END DESC, CASE WHEN tier = 9 THEN cc_score END DESC,
          checked, domain
        LIMIT :maximum""", {"daily": (today - timedelta(days=1)).isoformat(),
                           "weekly": (today - timedelta(days=7)).isoformat(),
                           "monthly": (today - timedelta(days=30)).isoformat(),
                           "halfyear": (today - timedelta(days=180)).isoformat(),
                           "maximum": maximum})
    with open(out, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(WORK_FIELDS)
        writer.writerows(rows)


def tier_for(result):
    category = result["category"]
    if category in APPLY_CATEGORIES:
        return 1 if result.get("tech") and result.get("us") != "non_us" else 2
    if category in {"careers_page_unclear", "no_openings"}:
        return 3
    return 5 if category in FAILURE_CATEGORIES else 4


def apply_result(db, result, today):
    domain = result["domain"]
    old = db.execute("SELECT checked, category, fails FROM site WHERE domain = ?", (domain,)).fetchone()
    if old is None:
        raise ValueError(f"triage result for unknown domain {domain}")
    tier = tier_for(result)
    fails = old["fails"] + 1 if tier == 5 else 0
    if old["category"] in APPLY_CATEGORIES and tier == 5 and fails < 5:
        db.execute("UPDATE site SET checked = ?, fails = ? WHERE domain = ?", (today, fails, domain))
        return

    roles = result.get("roles", [])
    defaults = {"roles": json.dumps(roles), "tech": int(result.get("tech", 0))}
    values = [defaults[key] if key in defaults else result.get(key, "") for key in RESULT_FIELDS]
    fields = ", ".join(f"{key} = ?" for key in RESULT_FIELDS)
    db.execute(f"UPDATE site SET {fields}, checked = ?, fails = ?, tier = ? WHERE domain = ?",
               (*values, today, fails, tier, domain))
    for role in roles:
        db.execute("""INSERT INTO role (key, domain, title, url, location, emails, apply_url,
                     first_seen, last_seen, baseline) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET title = excluded.title, last_seen = excluded.last_seen,
                url = excluded.url, location = excluded.location, emails = excluded.emails,
                apply_url = excluded.apply_url,
                assessed = CASE WHEN role.url != excluded.url OR role.location != excluded.location
                           THEN '{}' ELSE role.assessed END""",
                   (role_key(domain, role["t"]), domain, role["t"], role["u"], role.get("l", ""),
                    ";".join(role.get("e", [])), role.get("a", ""), today, today, int(not old["checked"])))


def apply_results(db, directory, today=None):
    today = today or date.today()
    seen = set()
    with db:
        for path in sorted(Path(directory).glob("triage-*.jsonl.gz")):
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    result = json.loads(line)
                    if result["domain"] not in seen:
                        apply_result(db, result, today.isoformat())
                        seen.add(result["domain"])
        db.execute("DELETE FROM role WHERE last_seen < ?", ((today - timedelta(days=60)).isoformat(),))
    return len(seen)


def stats(db):
    return {field: dict(db.execute(f"SELECT {field}, count(*) FROM site GROUP BY {field}"))
            for field in COUNT_FIELDS}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for cmd in ("candidates", "worklist", "apply", "stats"):
        sub.add_parser(cmd).add_argument("--db", required=True)
    args = sub.choices["candidates"]
    args.add_argument("--csv", required=True)
    args.add_argument("--src", required=True)
    args.add_argument("--skip-csv")
    args.add_argument("--canary", help="canary result to retain in the run summary")
    args = sub.add_parser("canary")
    args.add_argument("--csv", required=True)
    args.add_argument("--known", required=True)
    args = sub.choices["worklist"]
    args.add_argument("--out", required=True)
    args.add_argument("--max", type=int, required=True)
    sub.choices["apply"].add_argument("--results", required=True)
    args = parser.parse_args(argv)
    if args.command == "canary":
        print(canary(args.csv, args.known))
        return
    with closing(connect(args.db)) as db:
        if args.command == "candidates":
            print(candidates(db, args.csv, args.src, args.skip_csv), "candidate rows")
            if args.canary:
                with db:
                    db.execute("REPLACE INTO meta VALUES ('canary', ?)", (args.canary,))
        elif args.command == "worklist":
            if args.max < 0:
                parser.error("--max must be nonnegative")
            worklist(db, args.out, args.max)
        elif args.command == "apply":
            print(apply_results(db, args.results), "sites triaged")
        else:
            print(json.dumps(stats(db), indent=2))


if __name__ == "__main__":
    main()
