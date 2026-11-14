import gzip
import json
import time

from careers_scan.fetch import Page
from careers_scan.wide import triage

from test_wide import Fake, RANE

URL = "https://www.ranenetwork.com/about/career-page"
ITEM = {"domain": "ranenetwork.com", "url": URL, "warc": "file 0 15"}
HTML = RANE.replace("Enterprise Account Manager", "DevOps Engineer")


def test_triage_email_page():
    result = triage.triage_one(Fake({URL: HTML}), None, ITEM)
    assert (result["name"], result["category"], result["us"], result["tech"], result["via"]) == (
        "RANE", "email", "us", 1, "live")
    assert result["roles"][0]["t"] == "RANE DevOps Engineer"


def test_archive_rescues_blocked_page_but_respects_robots(monkeypatch):
    calls = []
    monkeypatch.setattr(triage, "archived_page", lambda _, warc: calls.append(warc) or HTML)
    result = triage.triage_one(Fake({URL: Page(URL, status=403)}), None, ITEM)
    assert result["category"] == "email" and result["via"] == "archive"
    result = triage.triage_one(Fake({URL: Page(URL, error="robots")}), None, ITEM)
    assert result["category"] == "robots_disallowed" and calls == [ITEM["warc"]]


def test_run_isolates_exceptions_and_observes_deadline(tmp_path, monkeypatch):
    def read(_, __, item, ___):
        if item["domain"] == "bad.example":
            raise OSError("unreachable in fixture")
        return {"domain": item["domain"], "category": "email"}

    monkeypatch.setattr(triage, "triage_one", read)
    destination = tmp_path / "triage-0.jsonl.gz"
    items = [ITEM, {"domain": "bad.example"}]
    assert triage.run(items, destination, workers=2, use_archive=False) == 2
    with gzip.open(destination, "rt") as fh:
        results = {row["domain"]: row for row in map(json.loads, fh)}
    assert results["bad.example"]["note"] == "OSError"
    assert results["ranenetwork.com"]["category"] == "email"
    assert triage.run(items, destination, deadline=time.monotonic() - 1) == 0
    with gzip.open(destination, "rt") as fh:
        assert not fh.read()
