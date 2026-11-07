import codecs
import csv
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tallo.storage import CSV_FIELDS, Storage, read_json, write_json

NOW = datetime(2026, 11, 7, 21, tzinfo=UTC)


def match(job_id: str, **changes: object) -> dict[str, object]:
    return {
        "job_id": job_id,
        "first_seen_utc": NOW,
        "posted_utc": NOW - timedelta(hours=2),
        "posted_text": "2 hours ago",
        "status": "alert",
        "score": 65,
        "score_reasons": ["core title +45", "no title level +5", "posted within 24h +15"],
        "title": "DevOps Engineer",
        "company": "Acme, Inc.",
        "city": "San-Antonio",
        "location": "San Antonio, TX",
        "job_url": f"https://tallo.com/talent/job/devops-engineer-{job_id}",
        **changes,
    }


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        assert tuple(reader.fieldnames or ()) == CSV_FIELDS
        return list(reader)


def test_dedupe_by_id_and_repost_key_across_runs(tmp_path):
    path = tmp_path / "tallo_matches.csv"
    with Storage(tmp_path) as store:
        assert store.save(match("12345")) == "alert"
        assert store.save(match("12345", title="Different title")) == "duplicate"
        store.flush_csv()
    first_export = path.read_bytes()
    assert first_export.startswith(codecs.BOM_UTF8)
    with Storage(tmp_path) as store:
        assert store.has_seen("12345")
        assert store.first_seen("12345") == NOW
        assert store.save(match("12346", title="DEVOPS ENG.", company=" acme INC ", city="San Antonio")) == "repost"
        assert store.save(match("12347", city="Dallas")) == "alert"
        assert store.save(match("12348", status="blocked", flags=["citizenship required"])) == "blocked"
        store.flush_csv()
        store.flush_csv()
    assert path.read_bytes().startswith(first_export)
    assert path.read_bytes().count(codecs.BOM_UTF8) == 1
    rows = csv_rows(path)
    assert [(row["job_id"], row["status"]) for row in rows] == [
        ("12345", "alert"), ("12346", "repost"), ("12347", "alert"), ("12348", "blocked")
    ]
    assert rows[1]["flags"] == "repost"
    assert rows[3]["flags"] == "citizenship required | repost"
    assert rows[0]["score_reasons"] == "core title +45 | no title level +5 | posted within 24h +15"


def test_prune_seen_preserves_csv_deduplication(tmp_path):
    old = NOW - timedelta(days=121)
    with Storage(tmp_path) as store:
        store.remember("12344", old, "Old job", "Acme", "Austin")
        store.save(match("12345", first_seen_utc=old, posted_utc=old))
        store.remember("12346", NOW, "New job", "Acme", "Austin")
        store.remember("12346", NOW + timedelta(days=1), "New job", "Acme", "Austin")
        store.prune(NOW)
        assert not store.has_seen("12344")
        assert store.first_seen("12346") == NOW
        assert store.first_seen("12345") == old
        assert store.save(match("12345")) == "duplicate"
        assert store.save(match("12347")) == "repost"
    with closing(sqlite3.connect(tmp_path / "seen.sqlite")) as connection:
        rows = connection.execute("SELECT job_id FROM seen ORDER BY job_id").fetchall()
    assert rows == [("12346",), ("12347",)]


@pytest.mark.parametrize("offset_from_end", [0, 11])
def test_csv_recovers_from_interrupted_append(tmp_path, offset_from_end):
    path = tmp_path / "tallo_matches.csv"
    first = match("12345")
    second = match("12346", title="Cloud Engineer", company="Café")
    with Storage(tmp_path / "reference") as reference:
        reference.save(first)
        reference.save(second)
        reference.flush_csv()
    expected = (tmp_path / "reference" / path.name).read_bytes()

    with Storage(tmp_path) as store:
        store.save(first)
        store.flush_csv()
        first_export = path.read_bytes()
        store.save(second)

    prefix = expected[:len(expected) - offset_from_end]
    with path.open("ab") as stream:
        stream.write(prefix[len(first_export):])
    with Storage(tmp_path) as store:
        assert store.has_seen("12346")
        store.flush_csv()
        store.flush_csv()
    assert path.read_bytes() == expected


def test_export_rebuilds_if_csv_missing_and_escapes_formula_cells(tmp_path):
    path = tmp_path / "tallo_matches.csv"
    with Storage(tmp_path) as store:
        store.save(match("12345", company="  =1+1 "))
        store.flush_csv()
        expected = path.read_bytes()
        path.unlink()
        store.flush_csv()
    assert path.read_bytes() == expected
    assert csv_rows(path)[0]["company"] == "'=1+1"


def test_dry_run_never_creates_or_changes_files(tmp_path):
    absent = tmp_path / "absent"
    with Storage(absent, dry_run=True) as store:
        store.save(match("12345"))
        store.flush_csv()
        store.write_report(NOW)
    assert not absent.exists()

    with Storage(tmp_path) as store:
        store.save(match("12345"))
        store.flush_csv()
        store.write_report(NOW)
    before = {path.name: (path.stat().st_mtime_ns, path.read_bytes()) for path in tmp_path.iterdir()}
    with Storage(tmp_path, dry_run=True) as store:
        assert store.has_seen("12345")
        assert store.save(match("12346")) == "repost"
        assert store.has_seen("12346")
        store.prune(NOW + timedelta(days=121))
        store.flush_csv()
        store.write_report(NOW + timedelta(days=1))
    after = {path.name: (path.stat().st_mtime_ns, path.read_bytes()) for path in tmp_path.iterdir()}
    assert after == before


def test_report_groups_by_posted_and_score_with_blocked_last(tmp_path):
    with Storage(tmp_path) as store:
        store.save(match("12345", title="Cloud Engineer", score=55))
        store.save(match("12346", title="DevOps Engineer [I]", score=70, posted_utc=None, flags=["date unknown"]))
        store.save(match("12347", title="SRE", score=80, posted_utc=NOW - timedelta(hours=25)))
        store.save(match("12348", title="Platform Engineer", score=90, status="blocked"))
        store.save(match("12349", title="Outside window", posted_utc=NOW - timedelta(hours=49)))
        store.write_report(NOW)
    report = (tmp_path / "tallo_latest.md").read_text(encoding="utf-8")
    assert report.index("DevOps Engineer") < report.index("Cloud Engineer")
    assert report.index("Last 24 hours") < report.index("Cloud Engineer") < report.index("24–48 hours ago")
    assert report.index("24–48 hours ago") < report.index("[SRE]") < report.index("Blocked (clearance/citizenship)")
    assert report.index("Blocked (clearance/citizenship)") < report.index("Platform Engineer")
    assert "First seen Nov 07, 2026 03:00 PM CST" in report
    assert "DevOps Engineer \\[I\\]" in report
    assert "https://tallo.com/talent/job/devops-engineer-12347" in report
    assert "Outside window" not in report


def test_atomic_json_roundtrip_and_failed_replace_preserves_prior_version(tmp_path, monkeypatch):
    path = tmp_path / "checkpoint.json"
    assert read_json(path) is None
    write_json(path, {"city": "Montréal"})
    assert read_json(path) == {"city": "Montréal"}

    def fail_replace(_source, _target):
        raise OSError("simulated interrupted replacement")

    monkeypatch.setattr("tallo.storage.os.replace", fail_replace)
    with pytest.raises(OSError, match="interrupted replacement"):
        write_json(path, {"city": "Austin"})
    assert read_json(path) == {"city": "Montréal"}
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("invalid", [b"{", b"[]", b"\xff"])
def test_invalid_checkpoint_is_not_silently_discarded(tmp_path, invalid):
    path = tmp_path / "checkpoint.json"
    path.write_bytes(invalid)
    with pytest.raises(ValueError, match="checkpoint.json"):
        read_json(path)
