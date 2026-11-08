"""Tests for parsers, matching, scoring and a full offline run. Run: python -m pytest -q"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from radar import ats as ATS
from radar import main as M
from radar import report as R
from radar import store as S
from radar.match import (Rules, assess, blocker_hit, classify_location, required_years,
                         title_category)
from radar.models import Board, Job

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime(2026, 11, 7, 18, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def rules():
    with open(os.path.join(ROOT, "config.yaml")) as f:
        return Rules.from_config(yaml.safe_load(f))


# ------------------------------------------------------------------ parsers (payload shapes from live APIs)

def test_greenhouse_list_and_detail(monkeypatch):
    payload = {"jobs": [{"absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1", "id": 1,
                         "location": {"name": "Remote, United States"}, "title": "Associate DevOps Engineer",
                         "updated_at": "2026-11-06T15:25:07-05:00", "first_published": "2026-11-06T10:00:00-05:00",
                         "company_name": "Acme"}], "meta": {"total": 1}}
    detail = {"content": "&lt;p&gt;Requires 2+ years of experience with AWS.&lt;/p&gt;",
              "offices": [{"name": "United States of America"}]}
    calls = []

    def fake_get(url, params=None):
        calls.append(url)
        return detail if url.endswith("/jobs/1") else payload

    monkeypatch.setattr(ATS, "get_json", fake_get)
    jobs = ATS.greenhouse_list(Board("greenhouse", "acme", "Acme"))
    assert len(jobs) == 1 and jobs[0].title == "Associate DevOps Engineer"
    assert jobs[0].posted_at == datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)
    assert jobs[0].workplace == "remote" and jobs[0].needs_detail
    ATS.greenhouse_detail(jobs[0])
    assert "2+ years of experience" in jobs[0].description
    assert jobs[0].countries == ["United States of America"]


def test_greenhouse_eu_host(monkeypatch):
    seen = []
    monkeypatch.setattr(ATS, "get_json", lambda url, params=None: seen.append(url) or {"jobs": []})
    ATS.greenhouse_list(Board("greenhouse", "eu:acme", "Acme"))
    assert seen[0] == "https://boards-api.greenhouse.io/v1/boards/acme/jobs"  # EU boards use the same API host


def test_lever_list(monkeypatch):
    payload = [{"id": "abc", "text": "Site Reliability Engineer", "categories": {"location": "Austin, TX",
                "allLocations": ["Austin, TX"]}, "createdAt": 1786469891368, "descriptionPlain": "Build infra.",
                "lists": [{"text": "Requirements", "content": "<li>1+ years of experience</li>"}],
                "additionalPlain": "", "country": "US", "workplaceType": "hybrid",
                "hostedUrl": "https://jobs.lever.co/acme/abc"}]
    monkeypatch.setattr(ATS, "get_json", lambda url, params=None: payload)
    j = ATS.lever_list(Board("lever", "acme", "Acme"))[0]
    assert j.workplace == "hybrid" and j.countries == ["US"] and "1+ years of experience" in j.description
    assert j.posted_at.year == 2026


def test_lever_error_object(monkeypatch):
    monkeypatch.setattr(ATS, "get_json", lambda url, params=None: {"ok": False, "error": "Document not found"})
    with pytest.raises(ATS.FetchError):
        ATS.lever_list(Board("lever", "nope", "Nope"))


def test_ashby_list(monkeypatch):
    payload = {"jobs": [
        {"id": "1", "title": "Platform Engineer", "location": "North America", "secondaryLocations": [],
         "publishedAt": "2026-11-07T08:49:37.626+00:00", "isListed": True, "isRemote": True, "workplaceType": "Remote",
         "address": {"postalAddress": {"addressCountry": "United States"}}, "jobUrl": "https://jobs.ashbyhq.com/acme/1",
         "descriptionPlain": "Kubernetes and Terraform."},
        {"id": "2", "title": "Hidden", "isListed": False},
    ], "apiVersion": "1"}
    monkeypatch.setattr(ATS, "get_json", lambda url, params=None: payload)
    jobs = ATS.ashby_list(Board("ashby", "acme", "Acme"))
    assert len(jobs) == 1 and jobs[0].countries == ["United States"] and jobs[0].workplace == "remote"


def test_smartrecruiters_list_and_detail(monkeypatch):
    listing = {"offset": 0, "limit": 100, "totalFound": 1, "content": [
        {"id": "744", "name": "Cloud Support Engineer", "company": {"identifier": "Acme", "name": "Acme"},
         "releasedDate": "2026-11-07T01:28:23.332Z", "ref": "https://api.smartrecruiters.com/v1/companies/acme/postings/744",
         "location": {"city": "Atlanta", "region": "Georgia", "country": "us", "remote": False, "hybrid": True,
                      "fullLocation": "Atlanta, Georgia, United States"}}]}
    detail = {"postingUrl": "https://jobs.smartrecruiters.com/Acme/744-cloud-support",
              "jobAd": {"sections": {"jobDescription": {"text": "<p>Help customers</p>"},
                                     "qualifications": {"text": "<ul><li>Must be a U.S. citizen</li></ul>"}}}}
    monkeypatch.setattr(ATS, "get_json", lambda url, params=None: detail if url.endswith("/744") else listing)
    j = ATS.smartrecruiters_list(Board("smartrecruiters", "acme", "Acme"))[0]
    assert j.workplace == "hybrid" and j.url == "https://jobs.smartrecruiters.com/Acme/744"
    ATS.smartrecruiters_detail(j)
    assert "U.S. citizen" in j.description and j.url.endswith("cloud-support")


def test_workday_paging_and_detail(monkeypatch):
    page1 = {"total": 25, "jobPostings": [
        {"title": f"Job {i}", "externalPath": f"/job/US-TX/Job_{i}", "locationsText": "Austin, TX",
         "postedOn": "Posted Today", "bulletFields": [f"R{i}"]} for i in range(20)]}
    page2 = {"total": 25, "jobPostings": [
        {"title": "DevOps Engineer", "externalPath": "/job/US-TX/DevOps_R99", "locationsText": "2 Locations",
         "postedOn": "Posted 3 Days Ago", "bulletFields": ["R99"]}]}
    posts = []

    def fake_post(url, payload):
        posts.append(payload["offset"])
        return page1 if payload["offset"] == 0 else page2

    monkeypatch.setattr(ATS, "post_json", fake_post)
    jobs = ATS.workday_list(Board("workday", "acme|wd5|Ext", "Acme"))
    assert posts == [0, 20] and len(jobs) == 21
    j = jobs[-1]
    assert j.url == "https://acme.wd5.myworkdayjobs.com/Ext/job/US-TX/DevOps_R99" and j.job_id == "R99"
    monkeypatch.setattr(ATS, "get_json", lambda url, params=None: {"jobPostingInfo": {
        "jobDescription": "<p>0-2 years of experience</p>", "location": "Austin, TX",
        "additionalLocations": ["Remote, USA"], "startDate": "2026-11-05",
        "country": {"descriptor": "United States of America"}}})
    ATS.workday_detail(j)
    assert j.countries == ["United States of America"] and j.posted_at.date().isoformat() == "2026-11-05"


def test_workday_stops_on_old_pages(monkeypatch):
    old = {"jobPostings": [{"title": "x", "externalPath": f"/job/a/{i}", "postedOn": "Posted 30+ Days Ago"}
                           for i in range(20)]}
    calls = []
    monkeypatch.setattr(ATS, "post_json", lambda url, p: calls.append(p["offset"]) or old)
    ATS.workday_list(Board("workday", "acme|wd1|Ext", "Acme"))
    assert calls == [0]


@pytest.mark.parametrize("text,days", [("Posted Today", 0), ("Posted Yesterday", 1), ("Posted 3 Days Ago", 3),
                                       ("Posted 30+ Days Ago", 31), ("", None)])
def test_workday_days(text, days):
    assert ATS.workday_days_ago(text) == days


# ------------------------------------------------------------------ matching

@pytest.mark.parametrize("title,expected", [
    ("DevOps Engineer", "core"), ("Associate Site Reliability Engineer", "core"), ("SRE - New Grad", "core"),
    ("Cloud Support Engineer", "core"), ("Platform Engineer II", "core"), ("Linux Systems Administrator", "bridge"),
    ("NOC Technician", "bridge"), ("Data Center Technician", "bridge"), ("Production Engineer", "bridge"),
    ("Senior DevOps Engineer", None), ("Sr. SRE", None), ("Staff Platform Engineer", None),
    ("DevOps Engineer Intern", None), ("Software Engineer", None), ("Systems Engineer (Missile Defense)", None),
    ("Infrastructure Engineer, TS/SCI", None), ("Platform Engineering Manager", None), ("Cloud Engineer 3", None),
    ("Solutions Architect - DevOps", None), ("Embedded Systems Engineer", None),
    ("Associate, Corporate Paralegal - Global Infrastructure", None), ("Maintenance Reliability Engineer", None),
    ("Power panels - Design Release Engineer", None), ("Test Infrastructure Technician I", None),
    ("Documentation Platform Engineer", None), ("Build Engineer, Castings", None),
    ("Software Engineer, Infrastructure", "core"), ("HPC Operations Engineer - EDA Infrastructure", "core"),
    ("Release Engineer", "bridge"), ("Database Reliability Engineer", "core"),
])
def test_titles(rules, title, expected):
    assert title_category(title, rules) == expected


@pytest.mark.parametrize("text,countries,expected", [
    ("Remote, United States", [], "us"), ("Austin, TX", [], "us"), ("Remote", [], "unknown"),
    ("Bangalore, IN", [], "non_us"), ("Berlin, DE", [], "non_us"), ("Wilmington, DE", [], "us"),
    ("Toronto, ON", [], "non_us"), ("Remote, Canada; Remote, United States", [], "us"),
    ("North America", ["US"], "us"), ("Europe", ["European Union"], "non_us"), ("2 Locations", ["Ukraine"], "non_us"),
    ("Singapore, Singapore", ["SG"], "non_us"), ("Remote - US", [], "us"), ("Manchester, NH", [], "us"),
    ("Lebanon, NH", [], "us"), ("Panama City, FL", [], "us"), ("Luxembourg (Siège Lux House)", [], "non_us"),
])
def test_location(text, countries, expected):
    assert classify_location(text, countries) == expected


def test_years():
    assert required_years("You have 3+ years of experience with Linux and 1 year with AWS") == 3
    assert required_years("0-2 years of professional experience") == 0
    assert required_years("Minimum of 5 years experience in DevOps") == 5
    assert required_years("We were founded 10 years ago") is None
    assert required_years("") is None


def test_blockers(rules):
    assert blocker_hit("Applicants must be a U.S. citizen.", rules)
    assert blocker_hit("Active Secret clearance required", rules)
    assert blocker_hit("This role is subject to ITAR", rules)
    assert blocker_hit("No security clearance required for this role.", rules) is None
    assert blocker_hit("We consider applicants regardless of citizenship status.", rules) is None


def _job(**kw):
    base = dict(ats="greenhouse", board="acme", job_id="1", company="Acme", title="DevOps Engineer",
                location="Austin, TX", url="https://x", posted_at=NOW - timedelta(hours=3), workplace="hybrid",
                description="1+ years of experience. Employer participates in E-Verify.")
    base.update(kw)
    return Job(**base)


def test_assess_alert(rules):
    a = assess(_job(), Board("greenhouse", "acme", "Acme"), rules, "core", now=NOW)
    assert a.status == "alert" and a.score >= 70 and "E-Verify" in a.flags


def test_assess_blocked(rules):
    a = assess(_job(description="Must be a US citizen. 1+ years of experience"), Board("greenhouse", "acme", "Acme"),
               rules, "core", now=NOW)
    assert a.status == "blocked"


def test_assess_old_and_senior_years(rules):
    b = Board("greenhouse", "acme", "Acme")
    assert assess(_job(posted_at=NOW - timedelta(days=40)), b, rules, "core", now=NOW).status == "old"
    a = assess(_job(description="8+ years of experience with Kubernetes"), b, rules, "core", now=NOW)
    assert a.status == "quiet" and a.years == 8


def test_assess_no_sponsorship_flag(rules):
    a = assess(_job(description="We are unable to sponsor visas now or in the future. 1 year of experience"),
               Board("greenhouse", "acme", "Acme"), rules, "core", now=NOW)
    assert "no future H-1B sponsorship" in a.flags and a.status == "alert"


# ------------------------------------------------------------------ full offline run

def test_full_run(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(S, "MATCHES_PATH", str(tmp_path / "matches.csv"))
    monkeypatch.setattr(S, "DATA", str(tmp_path))
    monkeypatch.setattr(R, "REPORT_PATH", str(tmp_path / "REPORT.md"))
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_TOPIC"):
        monkeypatch.delenv(var, raising=False)
    boards = [Board("greenhouse", "acme", "Acme"), Board("lever", "beta", "Beta"),
              Board("greenhouse", "gone", "Gone"), Board("greenhouse", "rtx", "RTX")]
    monkeypatch.setattr(M, "load_boards", lambda: boards)
    now = datetime.now(timezone.utc)
    state = {"jobs": [
        _job(job_id="1", title="Associate DevOps Engineer", posted_at=now - timedelta(hours=2), needs_detail=True,
             detail_ref="d1", description=""),
        _job(job_id="2", title="Senior SRE", posted_at=now - timedelta(hours=2)),
        _job(job_id="3", title="Cloud Engineer", location="London, UK", posted_at=now - timedelta(hours=2)),
    ]}

    def gh(b):
        if b.board == "gone":
            raise ATS.FetchError("HTTP 404", 404)
        if b.board == "rtx":
            raise AssertionError("excluded company must not be fetched")
        return [Job(**{**j.__dict__}) for j in state["jobs"]]

    def lever(b):
        return [_job(ats="lever", board="beta", job_id="L1", company="Beta", title="NOC Technician",
                     posted_at=now - timedelta(hours=1), description="Must be a U.S. citizen")]

    def gh_detail(j):
        j.description = "0-1 years of experience. E-Verify employer."
        j.countries = ["United States"]

    monkeypatch.setitem(ATS.LISTERS, "greenhouse", gh)
    monkeypatch.setitem(ATS.LISTERS, "lever", lever)
    monkeypatch.setitem(ATS.DETAILERS, "greenhouse", gh_detail)

    assert M.main(["--dry-run"]) == 0
    st = json.loads((tmp_path / "state.json").read_text())
    assert st["bootstrapped"] and "greenhouse:acme:1" in st["seen"] and "lever:beta:L1" in st["seen"]
    assert "greenhouse:acme:2" not in st["seen"]          # senior title never stored
    assert st["fails"]["greenhouse:gone"]["n"] == 1
    rows = list(csv.DictReader(open(tmp_path / "matches.csv")))
    by = {r["uid"]: r for r in rows}
    assert set(by) == {"greenhouse:acme:1", "lever:beta:L1"}
    assert by["greenhouse:acme:1"]["status"] == "alert"
    assert by["lever:beta:L1"]["status"] == "blocked"
    rep = (tmp_path / "REPORT.md").read_text()
    assert "Associate DevOps Engineer" in rep and "Hidden from alerts" in rep

    # second run: a new job appears; it is new, the board is baselined, so it would alert
    state["jobs"].append(_job(job_id="4", title="Site Reliability Engineer I", posted_at=now - timedelta(minutes=20),
                              needs_detail=True, detail_ref="d4", description=""))
    sent = []
    monkeypatch.setattr(M.Notifier, "configured", property(lambda self: True))
    monkeypatch.setattr(M.Notifier, "send_job", lambda self, m: sent.append(m["uid"]))
    monkeypatch.setattr(M.Notifier, "send_text", lambda self, *a, **k: None)
    assert M.main([]) == 0
    assert sent == ["greenhouse:acme:4"]
    rows = list(csv.DictReader(open(tmp_path / "matches.csv")))
    assert len(rows) == 3   # nothing duplicated

    # third run: three 404s disable the dead board
    M.main(["--dry-run"])
    M.main(["--dry-run"])
    st = json.loads((tmp_path / "state.json").read_text())
    assert st["fails"]["greenhouse:gone"].get("disabled_until")


# ------------------------------------------------------------------ newer job systems (payload shapes from live APIs)

def test_oracle_list_and_detail(monkeypatch):
    listing = {"items": [{"requisitionList": [
        {"Id": "202606590", "Title": "Cloud Engineer I", "PostedDate": "2026-11-07", "PrimaryLocation": "Chicago, IL, United States",
         "PrimaryLocationCountry": "US", "WorkplaceType": "Hybrid", "secondaryLocations": []}]}]}
    detail = {"items": [{"ExternalDescriptionStr": "<p>Build cloud</p>", "ExternalQualificationsStr": "<li>1+ years of experience</li>",
                         "PrimaryLocationCountry": "US", "ExternalPostedStartDate": "2026-11-07T00:24:14+00:00"}]}
    monkeypatch.setattr(ATS, "get_json", lambda url, params=None: detail if "Details" in url else listing)
    j = ATS.oracle_list(Board("oracle", "eedu.fa.em3.oraclecloud.com|CX_1003", "Acme"))[0]
    assert j.url == "https://eedu.fa.em3.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1003/job/202606590"
    assert j.workplace == "hybrid" and j.countries == ["US"]
    ATS.oracle_detail(j)
    assert "1+ years of experience" in j.description and j.posted_at.hour == 0


def test_workable_list(monkeypatch):
    page = {"nextPage": None, "results": [
        {"id": 1, "shortcode": "ABC123", "title": "DevOps Engineer", "remote": False, "isInternal": False,
         "locations": [{"city": "Austin", "region": "Texas", "country": "United States", "countryCode": "US", "hidden": False}],
         "published": "2026-11-06T00:00:00.000Z", "workplace": "on_site"}]}
    monkeypatch.setattr(ATS, "post_json", lambda url, payload: page)

    def no_widget(url, params=None):
        raise ATS.FetchError("HTTP 404", 404)
    monkeypatch.setattr(ATS, "get_json", no_widget)
    j = ATS.workable_list(Board("workable", "acme", "Acme"))[0]
    assert j.url == "https://apply.workable.com/acme/j/ABC123/" and j.countries == ["US"] and j.location.startswith("Austin")
    monkeypatch.setattr(ATS, "get_json", lambda url, params=None: {"description": "<p>x</p>", "requirements": "<p>2 years of experience</p>"})
    ATS.workable_detail(j)
    assert "2 years of experience" in j.description


def test_rippling_list_and_detail(monkeypatch):
    listing = {"items": [{"id": "u1", "name": "Site Reliability Engineer", "url": "https://ats.rippling.com/acme/jobs/u1",
                          "locations": [{"name": "Santa Clara, CA", "countryCode": "US", "workplaceType": "ON_SITE"}]}]}
    detail = {"description": {"role": "<p>3+ years of experience</p>"}, "createdOn": "2026-11-05T18:05:44.354000-07:00"}
    monkeypatch.setattr(ATS, "get_json", lambda url, params=None: detail if url.endswith("/u1") else listing)
    j = ATS.rippling_list(Board("rippling", "acme", "Acme"))[0]
    assert j.workplace == "onsite" and j.posted_at is None
    ATS.rippling_detail(j)
    assert j.posted_at is not None and "3+ years" in j.description


def test_bamboohr_list_and_detail(monkeypatch):
    listing = {"result": [{"id": "774", "jobOpeningName": "Systems Administrator", "location": {"city": None, "state": None},
                           "atsLocation": {"country": "United States", "state": "California", "city": "San Jose"},
                           "isRemote": None, "locationType": "1"}]}
    detail = {"result": {"jobOpening": {"description": "<p>0-2 years of experience</p>", "datePosted": "2026-11-06"}}}
    monkeypatch.setattr(ATS, "get_json", lambda url, params=None: detail if url.endswith("/detail") else listing)
    j = ATS.bamboohr_list(Board("bamboohr", "acme", "Acme"))[0]
    assert j.location == "San Jose, California, United States" and j.url == "https://acme.bamboohr.com/careers/774"
    ATS.bamboohr_detail(j)
    assert j.posted_at.date().isoformat() == "2026-11-06"


def test_recruitee_list(monkeypatch):
    data = {"offers": [{"id": 9, "slug": "devops", "title": "DevOps Engineer", "status": "published",
                        "created_at": "2026-10-30 15:29:49 UTC", "careers_url": "https://acme.recruitee.com/o/devops",
                        "locations": [{"city": "Boston", "state": "Massachusetts", "country": "United States", "country_code": "US"}],
                        "description": "<p>Kubernetes</p>", "remote": False}]}
    monkeypatch.setattr(ATS, "get_json", lambda url, params=None: data)
    j = ATS.recruitee_list(Board("recruitee", "acme", "Acme"))[0]
    assert j.posted_at == datetime(2026, 10, 30, 15, 29, 49, tzinfo=timezone.utc) and j.countries == ["US"]


def test_workday_site_host(monkeypatch):
    calls = []
    monkeypatch.setattr(ATS, "post_json", lambda url, p: calls.append(url) or {"jobPostings": [
        {"title": "Engineer I", "externalPath": "/job/AZ/Engineer-I_R1", "postedOn": "Posted Today", "bulletFields": ["R1"]}]})
    j = ATS.workday_list(Board("workday", "@microchiphr|wd5|External", "Microchip"))[0]
    assert calls[0] == "https://wd5.myworkdaysite.com/wday/cxs/microchiphr/External/jobs"
    assert j.url == "https://wd5.myworkdaysite.com/recruiting/microchiphr/External/job/AZ/Engineer-I_R1"


def test_workable_widget_feed(monkeypatch):
    data = {"name": "Acme", "jobs": [{"title": "Cloud Engineer", "shortcode": "XYZ9", "telecommuting": True,
             "published_on": "2026-11-06", "city": "Denver", "state": "Colorado", "country": "United States",
             "locations": [{"country": "United States", "countryCode": "US", "city": "Denver", "region": "Colorado", "hidden": False}]}]}
    monkeypatch.setattr(ATS, "get_json", lambda url, params=None: data)
    j = ATS.workable_list(Board("workable", "acme", "Acme"))[0]
    assert j.job_id == "XYZ9" and j.workplace == "remote" and j.countries == ["US"]
    assert j.location == "Denver, Colorado, United States" and j.posted_at.date().isoformat() == "2026-11-06"
