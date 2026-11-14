"""Assess wide sweep roles and publish reports for each radar profile."""
import argparse
import csv
import gzip
import json
import logging
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from careers_scan import apply_list, roles
from careers_scan.detect import match_vendor
from careers_scan.wide.state import connect, stats
from careers_scan.wide.triage import WideFetcher
from radar.notify import Notifier

log = logging.getLogger(__name__)
PROFILE_PATHS = "config.yaml,profiles/freshers/config.yaml"
OUTPUT = Path("careers_scan/wide")
COMPANY_FIELDS = ("domain", "name", "category", "us", "tech", "apply_emails", "form_url",
                  "doc_url", "careers_url", "roles_sample", "first_seen")
ROLE_FIELDS = ("profile", *apply_list.FIELDS)


def assess(db, profiles, fetcher, maximum, now):
    rules = {profile: roles.load_rules(path)[0] for profile, path in profiles.items()}
    rows = db.execute("""SELECT role.*, site.name, site.careers_url, site.url AS site_url,
                                site.apply_emails, site.form_url
        FROM role JOIN site USING (domain)
        WHERE site.tier IN (1,2) AND site.us != 'non_us'
        ORDER BY role.first_seen DESC, role.key""")
    details = {}
    output = {profile: [] for profile in profiles}
    alerts = {}
    with db:
        for row in rows:
            matched = {profile: category for profile, rule in rules.items()
                       if (category := roles.infra_category(row["title"], rule))}
            if not matched:
                continue
            cached = json.loads(row["assessed"])
            missing = matched.keys() - cached.keys()
            url = row["url"] or row["careers_url"] or row["site_url"]
            detail_url = row["url"] and url not in (row["careers_url"], row["site_url"])
            detail = {}
            if missing and detail_url:
                if url not in details and len(details) < maximum:
                    page = fetcher.page(url)
                    details[url] = (roles.read_job_page(page.text, page.url, {row["domain"]})
                                    if not page.error and page.status < 400 else {})
                detail = details.get(url, {})
            company = row["name"] or row["domain"]
            for profile, category in matched.items():
                if profile not in cached:
                    if detail_url and url not in details:
                        continue
                    if detail_url and not detail:
                        # Keep the role visible on temporary fetch failures, but retry the detail later.
                        cache_result = False
                    else:
                        cache_result = True
                    location = row["location"] or detail.get("location", "")
                    score = roles.assess_role(rules[profile], company, row["domain"], row["title"], category,
                                              location, detail.get("description", ""), url, now=now)
                    outside = score.location_class == "non_us"
                    assessment = {"score": score.score, "status": "outside_us" if outside else score.status,
                                  "flags": "; ".join(score.flags), "location": location, "outside_us": outside,
                                  "emails": detail.get("emails", []), "apply_url": detail.get("apply_url", "")}
                    if cache_result:
                        cached[profile] = assessment
                else:
                    assessment = cached[profile]
                addresses = [*row["emails"].split(";"), *assessment.get("emails", []),
                             *row["apply_emails"].split(";")]
                address = next((email.strip() for email in addresses if email.strip()), "")
                form = row["apply_url"] or assessment.get("apply_url") or row["form_url"]
                vendor = match_vendor(form) if form else None
                kind = "email" if address else vendor.kind if vendor and vendor.kind in {"form", "doc"} else (
                    "link" if form else "page")
                result = {**assessment, "profile": profile, "title": row["title"], "company": company,
                          "apply": address or form or url, "apply_kind": kind, "job_url": url,
                          "careers_url": row["careers_url"], "category": category, "domain": row["domain"],
                          "first_seen": row["first_seen"], "last_seen": row["last_seen"], "key": row["key"]}
                output[profile].append(result)
                if (not row["baseline"] and row["first_seen"] == now.date().isoformat()
                        and result["status"] == "alert"):
                    alerts.setdefault(row["key"], result)
            db.execute("UPDATE role SET assessed = ? WHERE key = ? AND assessed != ?",
                       (json.dumps(cached), row["key"], json.dumps(cached)))
    for rows in output.values():
        rows.sort(key=lambda row: (-row["score"], row["company"].casefold(), row["title"]))
    return output, list(alerts.values())


def write_csv(path, fields, rows):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "wt", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def company_rows(db, where="", limit=-1):
    columns = ", ".join(key for key in COMPANY_FIELDS if key != "roles_sample")
    for row in db.execute(f"""SELECT {columns}, roles FROM site
        WHERE category IN ('email','form','doc') {where}
        ORDER BY tier, lower(name), domain LIMIT ?""", (limit,)):
        yield {**dict(row), "roles_sample": " | ".join(role["t"] for role in json.loads(row["roles"])[:5])}


def write_reports(db, output, rows, now, release):
    (output / "data").mkdir(parents=True, exist_ok=True)
    markdown = ["# Careers found by the wide sweep", "",
                "A site marked `us = unknown` did not show a US address."]
    for profile, entries in rows.items():
        shown = [{**entry, "first_seen": entry["first_seen"] + "T00:00:00+00:00"}
                 for entry in entries if entry["status"] in {"alert", "quiet"}][:500]
        markdown.extend(["", f"## {profile}", "", *apply_list._table(shown, now)])
    (output / "ROLES.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    write_csv(output / "data/roles.csv", ROLE_FIELDS, (row for entries in rows.values() for row in entries))
    write_csv(output / "data/email_companies.csv", COMPANY_FIELDS,
              company_rows(db, "AND tier IN (1,2) AND us != 'non_us'", 50_000))
    write_csv(release, COMPANY_FIELDS, company_rows(db))
    meta = dict(db.execute("SELECT k, v FROM meta ORDER BY k"))
    crawls = ", ".join(key[3:] for key in meta if key.startswith("cc:")) or "None recorded"
    lines = ["# Wide sweep summary", "", f"Updated {now.date().isoformat()}.", "",
             f"Crawls swept: {crawls}.", "", meta.get("canary", "No coverage canary recorded."), ""]
    for field, counts in stats(db).items():
        lines.extend([f"## By {field}", "", *(f"- {k or 'unknown'}: {v}" for k, v in counts.items()), ""])
    lines.extend(["## Tier 1 sites", ""])
    lines.extend(f"- {apply_list._md(site['name'] or site['domain'])} ({site['domain']})"
                 for site in company_rows(db, "AND tier = 1", 200))
    (output / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def send_alerts(alerts):
    if not alerts:
        return
    try:
        notifier = Notifier({}, dry_run=False)
        if not notifier.configured:
            return
        for role in alerts[:15]:
            how = f"Apply by email: {role['apply']}" if role["apply_kind"] == "email" else f"Apply: {role['apply']}"
            body = f"{role['title']} at {role['company']} ({role['location'] or 'location not stated'}). {how}"
            if role["flags"]:
                body += f" [{role['flags']}]"
            notifier.send_text(body[:600], title=f"Wide sweep: {role['title']}"[:120], click=role["job_url"])
        if len(alerts) > 15:
            notifier.send_text(f"{len(alerts) - 15} more new roles; see careers_scan/wide/ROLES.md",
                               title="Wide sweep roles")
    except Exception as exc:
        log.warning("alerts not sent: %s", type(exc).__name__)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--profiles", default=PROFILE_PATHS)
    parser.add_argument("--max-detail", type=int, default=1500)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--alerts-out", type=Path, help="write pending alerts for a publishing workflow")
    args = parser.parse_args(argv)
    if args.max_detail < 0:
        parser.error("--max-detail must be nonnegative")
    paths = [Path(path.strip()) for path in args.profiles.split(",") if path.strip()]
    profiles = {str(path.parent) if path.parent != Path(".") else path.stem: path for path in paths}
    now = datetime.now(timezone.utc)
    with closing(connect(args.db)) as db:
        rows, alerts = assess(db, profiles, WideFetcher(), args.max_detail, now)
        write_reports(db, OUTPUT, rows, now, Path("wide-email-companies.csv.gz"))
    if args.alerts_out:
        args.alerts_out.write_text(json.dumps(alerts), encoding="utf-8")
    if not args.dry_run:
        send_alerts(alerts)


if __name__ == "__main__":
    main()
