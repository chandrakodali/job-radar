import csv
import gzip
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from careers_scan.wide import report, state

from test_wide import Fake


def test_reports_cache_detail_and_only_alert_new_nonbaseline_roles(tmp_path):
    now = datetime(2026, 11, 14, tzinfo=timezone.utc)
    careers = "https://acme.com/careers"
    detail = careers + "/devops"
    fetcher = Fake({detail: "<h1>DevOps Engineer</h1><p>Remote (US). 1 year of experience.</p>"})
    release = tmp_path / "wide-email-companies.csv.gz"
    output = tmp_path / "output"
    with closing(state.connect(tmp_path / "wide.sqlite")) as db:
        db.execute("INSERT INTO site (domain,url,category,tier,name,careers_url,apply_emails,us) "
                   "VALUES ('acme.com',?,'email',1,'Acme',?,'jobs@acme.com','unknown')", (careers, careers))
        db.executemany("INSERT INTO role(key,domain,title,url,location,first_seen,last_seen,baseline) "
                       "VALUES (?,'acme.com',?,?,'Remote (US)','2026-11-14','2026-11-14',?)",
                       [("baseline", "DevOps Engineer", detail, 1), ("new", "Cloud Engineer", careers, 0)])
        profiles = {"default": Path("config.yaml"), "freshers": Path("profiles/freshers/config.yaml")}
        rows, alerts = report.assess(db, profiles, fetcher, 1, now)
        assert {row["key"] for row in alerts} == {"new"}
        assert fetcher.calls == [detail]
        assert report.assess(db, profiles, Fake({}), 0, now)[0] == rows
        report.write_reports(db, output, rows, now, release)
    markdown = (output / "ROLES.md").read_text()
    assert "mailto:jobs@acme.com?subject=" in markdown and "## freshers" in markdown
    assert "Tier 1 sites" in (output / "SUMMARY.md").read_text()
    with (output / "data/roles.csv").open() as fh:
        assert {row["profile"] for row in csv.DictReader(fh)} == {"default", "freshers"}
    with gzip.open(release, "rt") as fh:
        assert next(csv.DictReader(fh))["domain"] == "acme.com"
    assert (output / "data/email_companies.csv").is_file()
