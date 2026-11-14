import csv
import gzip
import json
from contextlib import closing
from datetime import date, timedelta

import pytest

from careers_scan.wide import state

TODAY = date(2026, 11, 14)


@pytest.fixture
def db(tmp_path):
    with closing(state.connect(tmp_path / "state.sqlite")) as db:
        yield db


def test_candidates_update_skip_and_canary(db, tmp_path):
    path = tmp_path / "candidates.csv.gz"
    known = tmp_path / "known.csv"
    known.write_text("domain,category\nknown.com,email\nabsent.com,email\nother.com,form\n")
    with gzip.open(path, "wt") as fh:
        fh.write("domain,url,alt_url,score,n_detail,warc\n"
                 "acme.com,https://acme.com/jobs,,3,1,\n"
                 "known.com,https://known.com/jobs,,2,1,\n")
    assert state.canary(path, known).endswith("1 of 2 known apply-by-email sites (50.0%)")
    assert state.candidates(db, path, "cc:one", known, TODAY) == 1
    better = tmp_path / "better.csv"
    better.write_text("domain,url,alt_url,score,n_detail,warc\n"
                      "acme.com,https://acme.com/careers,https://acme.com/join,10,2,file 0 20\n")
    state.candidates(db, better, "cc:two", today=TODAY)
    state.candidates(db, better, "cc:two", today=TODAY)
    state.candidates(db, path, "cc:one", known, TODAY)
    site = db.execute("SELECT * FROM site").fetchone()
    assert (site["url"], site["src"], site["n_detail"], site["warc"]) == (
        "https://acme.com/careers", "cc:one|cc:two", 3, "file 0 20")
    assert site["tier"] == 9 and site["first_seen"] == TODAY.isoformat()


def test_worklist_order_and_due_dates(db, tmp_path):
    sites = [("daily", 1, 1, 0, 0, 0), ("fresh", 1, 0, 0, 0, 0),
             ("weekly", 2, 7, 0, 0, 0), ("monthly", 3, 30, 0, 0, 0),
             ("failed", 5, 179, 0, 0, 3), ("never", 9, 0, 1, 10, 0),
             ("popular", 9, 0, 2, 3, 0), ("best", 9, 0, 2, 4, 0)]
    db.executemany("INSERT INTO site (domain,url,tier,checked,n_detail,cc_score,fails) VALUES (?,?,?,?,?,?,?)",
                   [(d, f"https://{d}.test", t, (TODAY - timedelta(days=age)).isoformat(), n, score, fails)
                    for d, t, age, n, score, fails in sites])
    path = tmp_path / "worklist.csv"
    state.worklist(db, path, 5, TODAY)
    with path.open() as fh:
        assert [row["domain"] for row in csv.DictReader(fh)] == ["daily", "best", "popular", "never", "monthly"]


def test_apply_failure_protection_baseline_and_expiry(db, tmp_path):
    db.execute("INSERT INTO site (domain,url) VALUES ('x.com','https://x.com/careers')")
    path = tmp_path / "triage-0.jsonl.gz"

    def apply(age, **fields):
        with gzip.open(path, "wt") as fh:
            fh.write(json.dumps({"domain": "x.com", **fields}) + "\n")
        return state.apply_results(db, tmp_path, TODAY + timedelta(days=age))

    first = {"t": "DevOps Engineer", "u": "https://x.com/jobs/1"}
    new = {"t": "Cloud Engineer", "u": "https://x.com/jobs/2"}
    assert apply(0, category="email", tech=1, roles=[first]) == 1
    for age in range(1, 5):
        apply(age, category="blocked")
        site = db.execute("SELECT category, tier, fails FROM site").fetchone()
        assert tuple(site) == ("email", 1, age)
    apply(5, category="email", roles=[new])
    assert db.execute("SELECT fails FROM site").fetchone()[0] == 0
    got = dict(db.execute("SELECT title, baseline FROM role"))
    assert got == {first["t"]: 1, new["t"]: 0}
    for age in range(6, 11):
        apply(age, category="blocked")
    assert tuple(db.execute("SELECT category,tier,fails FROM site").fetchone()) == ("blocked", 5, 5)
    apply(61, category="unreachable")
    assert list(db.execute("SELECT title FROM role"))[0][0] == new["t"]
