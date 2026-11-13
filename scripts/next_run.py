"""Keep the radars on time. GitHub's own schedule (cron) often skips runs on busy days, so at the end of
each DevOps radar run this waits for the next half-hour slot and starts the next run itself, using the
run's own GITHUB_TOKEN (allowed to start workflow_dispatch runs). It also starts the Auto Radar every ~3 hours
and the IT Entry-Level Radar every ~hour. The cron schedules stay on as a backup that restarts the chain.

    python scripts/next_run.py          needs GH_TOKEN and GITHUB_REPOSITORY
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

API = "https://api.github.com"
SLOTS = (7, 37)            # minutes past the hour for the DevOps radar (same as its cron)
AUTO_EVERY = timedelta(hours=3)
FRESHERS_EVERY = timedelta(hours=1)


def gh(method: str, path: str, **kw):
    r = requests.request(method, API + path, timeout=30, headers={
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"}, **kw)
    r.raise_for_status()
    return r.json() if r.content else {}


def next_slot(now: datetime) -> datetime:
    for h in (0, 1):
        for m in SLOTS:
            t = (now + timedelta(hours=h)).replace(minute=m, second=0, microsecond=0)
            if t > now + timedelta(minutes=2):
                return t
    return now + timedelta(minutes=30)


def last_started(repo: str, workflow: str) -> datetime | None:
    runs = gh("GET", f"/repos/{repo}/actions/workflows/{workflow}/runs", params={"per_page": 5}).get("workflow_runs", [])
    times = [datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) for r in runs
             if r.get("conclusion") not in ("cancelled", "skipped")]
    return max(times) if times else None


def dispatch(repo: str, workflow: str) -> None:
    gh("POST", f"/repos/{repo}/actions/workflows/{workflow}/dispatches", json={"ref": "main"})
    print(f"Started {workflow}")


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    now = datetime.now(timezone.utc)
    for workflow, every in (("radar-auto.yml", AUTO_EVERY), ("radar-freshers.yml", FRESHERS_EVERY)):
        last = last_started(repo, workflow)
        if last is None or now - last > every - timedelta(minutes=10):
            dispatch(repo, workflow)
    t = next_slot(now)
    print(f"Next DevOps run at {t:%H:%M} UTC; waiting {int((t - now).total_seconds() // 60)} min")
    time.sleep(max(0.0, (t - datetime.now(timezone.utc)).total_seconds()))
    dispatch(repo, "radar.yml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
