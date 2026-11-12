"""Job-alert emails and Adzuna (radar/sources.py), with fake data."""
import csv
from datetime import datetime, timedelta, timezone

from radar import sources

NOW = datetime.now(timezone.utc)

LINKEDIN = """<table>
<tr><td><a href="https://www.linkedin.com/comm/jobs/view/4051234567/?trackingId=x&amp;refId=y"><img src="logo.png"></a></td>
<td><a href="https://www.linkedin.com/comm/jobs/view/4051234567/?trackingId=x">Embedded Software Engineer - Steering</a>
<p>Nexus Mobility Systems</p><p>Auburn Hills, MI (Hybrid)</p><p>Actively recruiting</p></td></tr>
<tr><td><a href="https://www.linkedin.com/comm/jobs/view/4059999999/?trackingId=z">AUTOSAR Integration Engineer</a>
<p>Tier One Inc · Troy, MI</p></td></tr>
<tr><td><a href="https://www.linkedin.com/comm/jobs/search?keywords=x">See all jobs</a></td></tr>
</table>"""

INDEED = """<div><a href="https://www.indeed.com/rc/clk?jk=abc123def456&amp;from=ja">Controls Software Engineer</a>
<br>Big Truck Co<br>Detroit, MI<br>$110,000 - $140,000 a year</div>"""


def test_linkedin_alert_is_parsed():
    jobs = sources.parse_alert(LINKEDIN, NOW)
    by = {j.job_id: j for j in jobs}
    assert set(by) == {"4051234567", "4059999999"}
    j = by["4051234567"]
    assert j.title == "Embedded Software Engineer - Steering" and j.company == "Nexus Mobility Systems"
    assert j.location == "Auburn Hills, MI (Hybrid)" and j.url == "https://www.linkedin.com/jobs/view/4051234567/"
    assert by["4059999999"].company == "Tier One Inc" and by["4059999999"].location == "Troy, MI"
    assert j.uid == "email:linkedin:4051234567"


def test_indeed_alert_is_parsed():
    (j,) = sources.parse_alert(INDEED, NOW)
    assert (j.title, j.company, j.location, j.job_id) == ("Controls Software Engineer", "Big Truck Co", "Detroit, MI", "abc123def456")


def test_no_mailbox_secrets_means_no_reading():
    assert sources.read_alert_emails({}, env={}) == []


def test_adzuna_stays_inside_free_limits(monkeypatch):
    calls = []

    def fake(url, params=None, **kw):
        calls.append(params["what"])
        return {"results": [{"id": len(calls), "title": "Embedded <strong>AUTOSAR</strong> Engineer",
                             "company": {"display_name": "Acme"}, "location": {"display_name": "Novi, MI"},
                             "description": "AUTOSAR and Simulink...", "redirect_url": "https://adzuna/x",
                             "created": "2026-11-10T10:00:00Z"}]}
    monkeypatch.setattr(sources, "get_json", fake)
    state = {}
    env = {"ADZUNA_APP_ID": "i", "ADZUNA_APP_KEY": "k"}
    cfg = {"terms": ["a", "b", "c"], "calls_per_run": 2}
    jobs = sources.adzuna_jobs(state, cfg, NOW, env=env)
    assert calls == ["a", "b"] and jobs[0].title == "Embedded AUTOSAR Engineer" and jobs[0].company == "Acme"
    assert state["adzuna_cursor"] == 2 and state["adzuna_calls"]["d"] == 2
    sources.adzuna_jobs(state, cfg, NOW, env=env)
    assert calls[2:] == ["c", "a"]                      # rotates through the terms
    state["adzuna_calls"]["m"] = sources.LIMITS["m"]       # monthly budget used up: no more calls
    assert sources.adzuna_jobs(state, cfg, NOW, env=env) == [] and len(calls) == 4
    assert sources.adzuna_jobs({}, cfg, NOW, env={}) == []


def test_alert_jobs_join_the_run_and_skip_jobs_the_boards_list(tmp_path, monkeypatch):
    from radar import ats as ATS
    from radar import main as M
    from radar.models import Board, Job
    from tests.test_automotive import RESUME_JOB, _point_at_tmp
    _point_at_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(M, "FOCUS_PATH", "")
    monkeypatch.setattr(M, "load_boards", lambda: [Board("greenhouse", "tier", "Tier One Inc")])
    monkeypatch.setattr(M, "load_discovered", lambda known, *a, **k: [])
    monkeypatch.setitem(ATS.LISTERS, "greenhouse", lambda b: [
        Job("greenhouse", "tier", "9", "Tier One Inc", "AUTOSAR Integration Engineer", location="Troy, MI",
            url="https://t/9", posted_at=NOW, description=RESUME_JOB)])
    monkeypatch.setattr(M._sources, "read_alert_emails", lambda state, days=4: sources.parse_alert(LINKEDIN, NOW))
    assert M.main(["--dry-run"]) == 0
    rows = {r["uid"]: r for r in csv.DictReader(open(tmp_path / "matches.csv"))}
    # the LinkedIn copy of the Tier One job is skipped (its own board has it); the other one is listed
    assert set(rows) == {"greenhouse:tier:9", "email:linkedin:4051234567"}
    r = rows["email:linkedin:4051234567"]
    assert r["company"] == "Nexus Mobility Systems" and "description not read" in r["flags"]
