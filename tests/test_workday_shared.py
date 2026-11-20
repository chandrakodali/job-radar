"""Shared Workday sweep: one slow sweep for all radars, read back by each radar."""
import os
import sys
from datetime import datetime, timedelta, timezone

import radar.ats as ATS
from radar.http import FetchError
from radar.models import Board, Job
from radar.workday_shared import dict_to_job, job_to_dict, load_cache, save_cache

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import workday_sweep as W  # noqa: E402

NOW = datetime.now(timezone.utc)


def J(i, days=1):
    return Job(ats="workday", board="acme|wd5|ext", job_id=str(i), company="Acme", title=f"Network Engineer {i}",
               location="Austin, TX", url=f"https://acme.wd5.myworkdayjobs.com/job/{i}",
               posted_at=NOW - timedelta(days=days), needs_detail=True, detail_ref=f"/job/{i}")


def test_round_trip(tmp_path):
    j = J(1)
    back = dict_to_job(job_to_dict(j))
    assert (back.uid, back.title, back.posted_at, back.needs_detail, back.detail_ref) == \
        (j.uid, j.title, j.posted_at, True, "/job/1")
    p = str(tmp_path / "w.json.gz")
    save_cache(p, {"boards": {"workday:acme|wd5|ext": {"at": "x", "jobs": [job_to_dict(j)]}}})
    assert load_cache(p)["boards"]["workday:acme|wd5|ext"]["jobs"][0]["job_id"] == "1"
    assert load_cache(str(tmp_path / "missing.gz")) == {"boards": {}}


def test_sweep_keeps_recent_jobs_oldest_boards_first_and_stops_on_rate_limits(monkeypatch):
    calls = []
    boards = [Board("workday", "new|wd1|a", "New"), Board("workday", "old|wd1|a", "Old"),
              Board("workday", "busy|wd1|a", "Busy")]

    def fake(b, **k):
        calls.append(b.board)
        if b.board.startswith("busy"):
            raise FetchError("rate limited", 429)
        return [J(1, days=2), J(2, days=40)]
    monkeypatch.setattr(ATS, "workday_list", fake)
    monkeypatch.setattr(W.time, "sleep", lambda s: None)
    cache = {"boards": {"workday:new|wd1|a": {"at": "2026-11-20T10:00", "jobs": []},
                        "workday:gone|wd1|a": {"at": "2026-01-01T00:00", "jobs": []}}}
    stats = W.sweep(cache, boards, budget_s=60, days=10, max_pages=5)
    assert calls[0] in ("old|wd1|a", "busy|wd1|a")                 # never-read boards before recently read
    assert "workday:gone|wd1|a" not in cache["boards"]              # boards no longer listed are dropped
    assert [d["job_id"] for d in cache["boards"]["workday:old|wd1|a"]["jobs"]] == ["1"]   # 40-day-old job left out
    assert stats["rate_limited"] == 1 and stats["ok"] == 2
