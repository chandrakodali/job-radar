"""Durable job records, append-only CSV export, and the latest report."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
from collections.abc import Mapping
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .parsing import normalize_title

CSV_FIELDS = (
    "first_seen_utc", "posted_utc", "posted_text",
    "score", "status", "score_reasons", "flags",
    "title", "company", "location", "work_type", "employment_type", "pay",
    "years_required", "job_url", "apply_url", "apply_domain",
    "category", "state", "city", "job_id",
)
_SCRAPED_CSV_FIELDS = {
    "posted_text", "title", "company", "location", "work_type",
    "employment_type", "pay",
}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    job_id TEXT PRIMARY KEY NOT NULL,
    first_seen_utc TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    city TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS seen_first_seen ON seen(first_seen_utc);
CREATE TABLE IF NOT EXISTS matches (
    sequence INTEGER PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE,
    first_seen_utc TEXT NOT NULL,
    effective_utc TEXT NOT NULL,
    repost_key TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS matches_repost_key ON matches(repost_key);
CREATE INDEX IF NOT EXISTS matches_effective ON matches(effective_utc);
CREATE TABLE IF NOT EXISTS csv_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    last_sequence INTEGER NOT NULL,
    offset INTEGER NOT NULL
);
"""


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"Invalid JSON in {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def write_text(path: Path, contents: str) -> None:
    """Replace a small text file atomically on the same filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    )
    tmp_path = Path(temporary.name)
    try:
        with temporary:
            temporary.write(contents)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    write_text(path, json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n")


def _utc(value: datetime | str) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.utcoffset() is None:
        raise ValueError("Timestamps must have a UTC offset")
    return result.astimezone(UTC)


def _iso(value: datetime | str) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def _text(value: object) -> str:
    return "" if value is None else " ".join(str(value).split())


def _joined(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return " | ".join(_text(item) for item in value)
    return _text(value)


def _key_part(value: str) -> str:
    return re.sub(r"[\W_]+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def _repost_key(company: str, title: str, city: str) -> str | None:
    parts = tuple(_key_part(value) for value in (company, normalize_title(title), city))
    return json.dumps(parts, ensure_ascii=False) if all(parts) else None


def _csv_chunk(record: Mapping[str, str] | None = None) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, CSV_FIELDS, lineterminator="\n")
    if record is None:
        writer.writeheader()
        return stream.getvalue().encode("utf-8-sig")
    cell_values = dict(record)
    for field in _SCRAPED_CSV_FIELDS:
        # Quoting a CSV cell alone does not prevent spreadsheet formula execution.
        if cell_values[field].startswith(("=", "+", "-", "@", "＝", "＋", "－", "＠")):
            cell_values[field] = "'" + cell_values[field]
    writer.writerow(cell_values)
    return stream.getvalue().encode("utf-8")


def _markdown(text: str) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\\x60*_\[\]()|~])", r"\\\1", text)


def _report_item(job: Mapping[str, str], local_zone: ZoneInfo) -> list[str]:
    title = _markdown(job["title"])
    url = quote(job["job_url"], safe=":/%?&=+@,.-_")
    display = " · ".join(_markdown(job[field]) for field in ("company", "location") if job[field])
    date = _utc(job["posted_utc"] or job["first_seen_utc"]).astimezone(local_zone)
    date_label = "Posted" if job["posted_utc"] else "First seen"
    details = [f"{date_label} {date:%b %d, %Y %I:%M %p %Z}", f"score {job['score']}"]
    if job["flags"]:
        details.append(_markdown(job["flags"].replace(" | ", ", ")))
    return [f"- [{title}]({url})" + (f" · {display}" if display else ""), "  " + " · ".join(details)]


class Storage:
    def __init__(self, data_dir: Path, *, dry_run: bool = False) -> None:
        self.data_dir = data_dir
        self.dry_run = dry_run
        self._db: sqlite3.Connection

    def __enter__(self) -> Storage:
        path = self.data_dir / "seen.sqlite"
        if not self.dry_run:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(":memory:" if self.dry_run else path, timeout=30)
        try:
            if self.dry_run and path.is_file():
                # This database uses SQLite's default rollback-journal mode.
                with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as existing:
                    existing.backup(self._db)
            self._db.executescript(_SCHEMA)
        except BaseException:
            self._db.close()
            raise
        return self

    def __exit__(self, *_: object) -> None:
        self._db.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        # Reserve the write lock before checking duplicates or exporting bytes.
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            yield

    def prune(self, now: datetime) -> None:
        with self._db:
            self._db.execute(
                "DELETE FROM seen WHERE first_seen_utc < ?",
                (_iso(now - timedelta(days=120)),),
            )

    def first_seen(self, job_id: str) -> datetime | None:
        row = self._db.execute(
            """
            SELECT first_seen_utc FROM seen WHERE job_id = ?
            UNION ALL
            SELECT first_seen_utc FROM matches WHERE job_id = ?
            LIMIT 1
            """,
            (job_id, job_id),
        ).fetchone()
        return _utc(row[0]) if row else None

    def has_seen(self, job_id: str) -> bool:
        return self.first_seen(job_id) is not None

    def _remember(self, job_id: str, timestamp: str, title: str, company: str, city: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO seen (job_id, first_seen_utc, title, company, city) VALUES (?, ?, ?, ?, ?)",
            (job_id, timestamp, title, company, city),
        )

    def remember(self, job_id: str, first_seen: datetime, title: str, company: str, city: str) -> None:
        with self._db:
            self._remember(job_id, _iso(first_seen), title, company, city)

    def save(self, row: Mapping[str, Any]) -> str:
        """Commit one match and its seen ID together, then return its final status."""
        record = {key: _text(row.get(key)) for key in CSV_FIELDS}
        record["first_seen_utc"] = _iso(row["first_seen_utc"])
        if record["posted_utc"]:
            record["posted_utc"] = _iso(row["posted_utc"])
        record["flags"] = _joined(row.get("flags"))
        record["score_reasons"] = _joined(row.get("score_reasons"))
        job_id = record["job_id"]
        status = record["status"]
        if not job_id or status not in {"alert", "quiet", "blocked", "repost"}:
            raise ValueError("A match requires a job ID and a valid output status")
        key = _repost_key(record["company"], record["title"], record["city"])
        with self._write_transaction():
            if self._db.execute("SELECT 1 FROM matches WHERE job_id = ?", (job_id,)).fetchone():
                return "duplicate"
            if key and self._db.execute("SELECT 1 FROM matches WHERE repost_key = ?", (key,)).fetchone():
                if "repost" not in record["flags"].split(" | "):
                    record["flags"] = " | ".join(filter(None, (record["flags"], "repost")))
                if status != "blocked":
                    status = record["status"] = "repost"
            first_seen = self.first_seen(job_id)
            if first_seen is not None:
                record["first_seen_utc"] = _iso(first_seen)
            self._remember(job_id, record["first_seen_utc"], record["title"], record["company"], record["city"])
            self._db.execute(
                "INSERT INTO matches (job_id, first_seen_utc, effective_utc, repost_key, payload) VALUES (?, ?, ?, ?, ?)",
                (
                    job_id, record["first_seen_utc"], record["posted_utc"] or record["first_seen_utc"],
                    key, json.dumps(record, ensure_ascii=False),
                ),
            )
        return status

    def _csv_chunks(self, after: int, offset: int) -> Iterator[tuple[int, bytes]]:
        if offset == 0:
            yield 0, _csv_chunk()
        cursor = self._db.execute(
            "SELECT sequence, payload FROM matches WHERE sequence > ? ORDER BY sequence",
            (after,),
        )
        for sequence, payload in cursor:
            yield sequence, _csv_chunk(json.loads(payload))

    def flush_csv(self) -> None:
        """Export committed rows. Resume a partial append at the matching byte."""
        if self.dry_run:
            return
        path = self.data_dir / "tallo_matches.csv"
        with self._write_transaction():
            after, offset = self._db.execute(
                "SELECT last_sequence, offset FROM csv_state WHERE singleton = 1"
            ).fetchone() or (0, 0)
            with path.open("a+b") as stream:
                size = os.fstat(stream.fileno()).st_size
                if size < offset:
                    after, offset = 0, 0
                stream.seek(offset)
                uncommitted = size - offset
                for sequence, chunk in self._csv_chunks(after, offset):
                    existing = min(uncommitted, len(chunk))
                    if existing and stream.read(existing) != chunk[:existing]:
                        raise ValueError(f"{path} differs from the SQLite export. Restore the paired data files.")
                    uncommitted -= existing
                    if existing < len(chunk):
                        stream.write(chunk[existing:])
                    after = sequence
                if uncommitted:
                    raise ValueError(f"{path} has rows absent from SQLite. Restore the paired data files.")
                stream.flush()
                os.fsync(stream.fileno())
                size = os.fstat(stream.fileno()).st_size
            self._db.execute(
                "INSERT OR REPLACE INTO csv_state (singleton, last_sequence, offset) VALUES (1, ?, ?)",
                (after, size),
            )

    def write_report(self, now: datetime, since_hours: float = 48) -> None:
        if self.dry_run:
            return
        now = _utc(now)
        window = timedelta(hours=since_hours)
        rows = self._db.execute(
            "SELECT payload FROM matches WHERE effective_utc BETWEEN ? AND ? ORDER BY effective_utc DESC",
            (_iso(now - window), _iso(now)),
        )
        jobs = [json.loads(row[0]) for row in rows]
        jobs.sort(key=lambda job: int(job["score"]), reverse=True)

        local_zone = ZoneInfo("America/Chicago")
        buckets: list[tuple[str, list[dict[str, str]]]] = [
            (f"Last {min(24, since_hours):g} hours", []),
        ]
        if since_hours > 24:
            buckets.append((f"24–{min(48, since_hours):g} hours ago", []))
        if since_hours > 48:
            buckets.append((f"48–{since_hours:g} hours ago", []))
        blocked: list[dict[str, str]] = []
        for job in jobs:
            if job["status"] == "blocked":
                blocked.append(job)
                continue
            age = now - _utc(job["posted_utc"] or job["first_seen_utc"])
            bucket = int(age > timedelta(hours=24)) + int(age > timedelta(hours=48))
            buckets[bucket][1].append(job)
        buckets.append(("Blocked (clearance/citizenship)", blocked))

        report = ["# Tallo jobs", "", f"Generated {now.astimezone(local_zone):%b %d, %Y %I:%M %p %Z}", ""]
        for heading, section in buckets:
            report.extend((f"## {heading}", ""))
            if section:
                for job in section:
                    report.extend(_report_item(job, local_zone))
            else:
                report.append("No matches.")
            report.append("")
        write_text(self.data_dir / "tallo_latest.md", "\n".join(report))
