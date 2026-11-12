"""Tests for profiles and the automotive profile's rules, readers and a full offline run."""
from __future__ import annotations

import csv
import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from radar import ats as ATS
from radar import ats_more
from radar import dashboard as D
from radar import deep
from radar import main as M
from radar import profile as P
from radar import report as R
from radar import store as S
from radar.match import (Rules, assess, has_evidence, job_category, row_still_fits, skill_hits,
                         title_category)
from radar.models import Board, Job

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTO = os.path.join(ROOT, "profiles", "automotive")
NOW = datetime(2026, 11, 11, 18, 0, tzinfo=timezone.utc)

RESUME_JOB = ("Develop model-based controls in MATLAB/Simulink and Stateflow, generate production code with "
              "Embedded Coder or TargetLink and integrate AUTOSAR software components with DaVinci Developer. "
              "MISRA C compliance with Polyspace, ISO 26262, MIL/SIL testing, CAN bus. 3+ years of experience "
              "in embedded C. We participate in E-Verify.")
WEB_JOB = "Build web services in Python and React on AWS. 3+ years of experience. Git, CI/CD."


@pytest.fixture(scope="module")
def cfg():
    with open(os.path.join(AUTO, "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def rules(cfg):
    return Rules.from_config(cfg)


@pytest.fixture(scope="module")
def devops_rules():
    with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
        return Rules.from_config(yaml.safe_load(f))


def _job(**kw):
    base = dict(ats="greenhouse", board="acme", job_id="1", company="Acme", title="Embedded Software Engineer",
                location="Auburn Hills, MI", url="https://x", posted_at=NOW - timedelta(hours=3),
                workplace="hybrid", description=RESUME_JOB)
    base.update(kw)
    return Job(**base)


# ------------------------------------------------------------------ titles

@pytest.mark.parametrize("title", [
    "Embedded Software Engineer", "Senior Embedded Software Engineer - Steering", "Controls Software Engineer",
    "Model-Based Design Engineer", "AUTOSAR Integration Engineer", "ADAS Software Engineer", "EPS Software Engineer",
    "Steering Systems Engineer", "HIL Test Engineer", "Firmware Engineer II", "Powertrain Controls Engineer",
    "Software Engineer II, Vehicle Controls", "Motor Controls Engineer", "Staff Firmware Engineer",
    "Software Developer - Base Software", "MATLAB/Simulink Developer",
])
def test_core_titles(rules, title):
    assert title_category(title, rules) == "core"


@pytest.mark.parametrize("title", [
    "Controls Engineer", "Algorithm Engineer", "Functional Safety Engineer", "C++ Software Engineer",
    "Vehicle Integration Engineer", "Software Integration Engineer", "Flight Controls Engineer",
])
def test_wide_titles_anywhere(rules, title):
    assert title_category(title, rules) == "wide"


@pytest.mark.parametrize("title", ["Software Engineer", "Systems Engineer", "Test Engineer", "Engineer II",
                                   "Calibration Engineer", "Senior Engineer - Lane Keeping"])
def test_generic_titles_only_on_focus_boards(rules, title):
    assert title_category(title, rules) is None
    assert title_category(title, rules, focus=True) == "wide"


@pytest.mark.parametrize("title", [
    "Engineering Manager, Embedded Software", "Embedded Software Intern", "Mechanical Design Engineer",
    "Quality Control Engineer", "Embedded Finance Engineer", "Full Stack Software Engineer",
    "Design Release Engineer - Steering", "Process Controls Engineer", "PLC Controls Engineer",
    "Embedded Systems Engineer (m/w/d)", "Manufacturing Engineer", "Controls Technician", "Chief Engineer",
    "Data Scientist - ADAS", "Document Control Specialist",
])
def test_excluded_titles(rules, title):
    assert title_category(title, rules, focus=True, searched=True) is None


def test_keyword_search_hit_counts_whatever_the_title(rules):
    assert title_category("Technical Professional 3", rules) is None
    assert title_category("Technical Professional 3", rules, searched=True) == "wide"


def test_devops_profile_never_produces_wide(devops_rules):
    assert title_category("Software Engineer", devops_rules, focus=True, searched=True) is None
    assert title_category("DevOps Engineer", devops_rules) == "core"
    assert row_still_fits({"title": "DevOps Engineer", "category": "core"}, devops_rules)
    assert not row_still_fits({"title": "Senior DevOps Engineer", "category": "core"}, devops_rules)


# ------------------------------------------------------------------ skills evidence

def test_skill_hits_cover_the_resume_tools(rules):
    weight, strong, names = skill_hits(RESUME_JOB, rules)
    for tool in ("AUTOSAR", "Simulink", "Stateflow", "Embedded Coder", "TargetLink", "Polyspace", "DaVinci",
                 "MISRA", "ISO 26262", "MIL/SIL/HIL", "MATLAB", "CAN"):
        assert tool in names
    assert strong >= 9 and weight >= 30


def test_evidence_needs_a_strong_skill(rules):
    assert has_evidence(RESUME_JOB, rules)
    assert not has_evidence(WEB_JOB, rules)
    assert not has_evidence("PLC programming, control systems design, HMI. MATLAB a plus. Python, Git.", rules)
    assert has_evidence("Electric power steering software in embedded C on an automotive ECU", rules)


def test_skill_patterns_avoid_lookalikes(rules):
    names = skill_hits("Car doors and closures. We can do it. Basil pesto. Earnings were flat. "
                       "DaVinci Resolve video editing.", rules)[2]
    assert not {"DOORS", "CAN", "ISO 26262", "DaVinci"} & set(names)
    assert "DOORS" in skill_hits("Requirements in IBM DOORS", rules)[2]
    assert "CAN" in skill_hits("CAN/LIN communication, CAN FD", rules)[2]


def test_untitled_job_on_focus_board_needs_strong_text(rules):
    focus = Board("lever", "oem", "OEM", focus=True)
    other = Board("lever", "x", "X")
    j = _job(title="Technical Professional 3")
    assert job_category(j, focus, rules) == "wide"
    assert job_category(j, other, rules) is None
    assert job_category(_job(title="Technical Professional 3", description=WEB_JOB), focus, rules) is None
    assert job_category(_job(title="Plant Manager"), focus, rules) is None


def test_saved_wide_match_survives_rule_changes(rules):
    assert row_still_fits({"title": "Technical Professional 3", "category": "wide"}, rules)
    assert not row_still_fits({"title": "Engineering Manager", "category": "wide"}, rules)
    assert row_still_fits({"title": "Embedded Software Engineer", "category": "core"}, rules)


# ------------------------------------------------------------------ scoring

def test_strong_match_alerts_and_lists_tools(rules):
    a = assess(_job(), Board("greenhouse", "acme", "Acme", focus=True), rules, "core", now=NOW)
    assert a.status == "alert" and a.score >= 75
    assert a.flags[0].startswith("tools: AUTOSAR, Simulink") and "E-Verify" in a.flags and "3+ yrs exp" in a.flags


def test_senior_title_is_fine_but_staff_is_a_stretch(rules):
    b = Board("greenhouse", "acme", "Acme")
    senior = assess(_job(title="Senior Embedded Software Engineer"), b, rules, "core", now=NOW)
    staff = assess(_job(title="Staff Embedded Software Engineer"), b, rules, "core", now=NOW)
    assert "stretch: senior-level title" not in senior.flags
    assert "stretch: senior-level title" in staff.flags and staff.score == senior.score - 10


def test_years_required_against_four_years_of_experience(rules):
    b = Board("greenhouse", "acme", "Acme")
    def score(years):
        return assess(_job(description=RESUME_JOB.replace("3+ years", f"{years}+ years")), b, rules, "core", now=NOW)
    assert score(5).score > score(7).score > score(8).score
    assert score(9).status == "quiet"                       # too senior to alert on, still listed
    hidden = score(10)                                      # 10+ years: moved to the hidden list, with the reason
    assert hidden.status == "blocked" and "asks for 10+ years of experience" in hidden.flags


def test_target_title_without_any_tool_is_set_aside(rules):
    a = assess(_job(title="Senior Software Engineer, Embedded", description="Join our fintech platform team. Benefits."),
               Board("greenhouse", "acme", "Acme"), rules, "core", now=NOW)
    assert a.status == "blocked" and "none of your tools in the job text" in a.flags
    # A plainly embedded title counts as evidence itself, so a thin posting is listed, not hidden.
    thin = assess(_job(title="Embedded Software Engineer 1", description="Write C++ for our devices. Benefits."),
                  Board("jibe", "careers.garmin.com", "Garmin"), rules, "core", now=NOW)
    assert thin.status in ("alert", "quiet")
    unread = assess(_job(description=""), Board("greenhouse", "acme", "Acme"), rules, "core", now=NOW)
    assert unread.status in ("alert", "quiet") and "description not read" in unread.flags


def test_citizenship_requirement_is_blocked(rules):
    a = assess(_job(description=RESUME_JOB + " Must be a U.S. citizen."), Board("greenhouse", "acme", "Acme"),
               rules, "core", now=NOW)
    assert a.status == "blocked" and any(f.startswith("needs:") for f in a.flags)


def test_home_state_bonus_and_stale_flag(rules):
    b = Board("greenhouse", "acme", "Acme")
    mi = assess(_job(), b, rules, "core", now=NOW)
    tx = assess(_job(location="Austin, TX"), b, rules, "core", now=NOW)
    assert mi.score == tx.score + 5 or mi.score == 100
    old = assess(_job(posted_at=NOW - timedelta(days=90)), b, rules, "core", now=NOW)
    assert "open 90 days" in old.flags
    assert assess(_job(posted_at=NOW - timedelta(days=200)), b, rules, "core", now=NOW).status == "old"


# ------------------------------------------------------------------ readers: keyword search

def test_workday_search_sends_text_and_ignores_age(monkeypatch):
    calls = []

    def fake_post(url, payload, **kw):
        calls.append(payload)
        page = payload["offset"] // 20
        if page > 1:
            return {"jobPostings": []}
        return {"jobPostings": [{"title": f"Engineer {page}-{i}", "externalPath": f"/job/MI/E_{page}_{i}",
                                 "postedOn": "Posted 30+ Days Ago", "bulletFields": [f"R{page}{i}"],
                                 "locationsText": "Warren, Michigan"} for i in range(20)]}
    monkeypatch.setattr(ATS, "post_json", fake_post)
    b = Board("workday", "gm|wd5|Careers", "GM")
    found = ATS.workday_list(b, max_pages=3, search="AUTOSAR")
    assert len(found) == 40 and all(j.found_by == "AUTOSAR" for j in found)
    assert [c["searchText"] for c in calls] == ["AUTOSAR"] * 3
    calls.clear()
    listed = ATS.workday_list(b, max_pages=3)      # newest-first listing stops at the first old page
    assert len(listed) == 20 and len(calls) == 1 and listed[0].found_by == ""


def test_smartrecruiters_and_oracle_search_params(monkeypatch):
    seen = []
    monkeypatch.setattr(ATS, "get_json", lambda url, params=None, **kw: seen.append((url, params)) or
                        {"content": [], "items": [{"requisitionList": []}]})
    ATS.smartrecruiters_list(Board("smartrecruiters", "BoschGroup", "Bosch"), search="ISO 26262")
    assert seen[-1][1]["q"] == "ISO 26262" and seen[-1][1]["country"] == "us"
    ATS.oracle_list(Board("oracle", "x.fa.oraclecloud.com|CX_1", "Ford"), search="ISO 26262")
    assert 'keyword=%22ISO%2026262%22,' in seen[-1][0] and "sortBy=RELEVANCY" in seen[-1][0]
    ATS.oracle_list(Board("oracle", "x.fa.oraclecloud.com|CX_1", "Ford"))
    assert "keyword" not in seen[-1][0] and "POSTING_DATES_DESC" in seen[-1][0]


def test_greenhouse_content_in_list(monkeypatch):
    monkeypatch.setattr(ATS, "get_json", lambda url, params=None, **kw: {"jobs": [
        {"id": 7, "title": "Engineer II", "absolute_url": "https://g/7", "location": {"name": "Novi, MI"},
         "first_published": "2026-11-10T10:00:00Z", "content": "&lt;p&gt;AUTOSAR and Simulink&lt;/p&gt;",
         "offices": [{"name": "United States"}]}]} if params == {"content": "true"} else {"jobs": []})
    j = ATS.greenhouse_list(Board("greenhouse", "acme", "Acme"), content=True)[0]
    assert j.description == "AUTOSAR and Simulink" and j.countries == ["United States"]
    assert ATS.greenhouse_list(Board("greenhouse", "acme", "Acme")) == []


def test_sfrmk_search_page(monkeypatch):
    html = """<table><tr class="data-row"><td><a class="jobTitle-link" href="/job/Northville-Software-Engineer-MI-48167/123456700/">
    Software Engineer - Steering</a></td><td><span class="jobLocation">Northville, MI, US, 48167</span></td>
    <td><span class="jobDate">Nov 3, 2026</span></td></tr>
    <tr class="data-row"><td><a class="jobTitle-link" href="/job/Friedrichshafen-Entwickler/223456700/">Entwickler</a></td>
    <td><span class="jobLocation">Friedrichshafen, DE</span></td></tr></table>"""
    monkeypatch.setattr(ats_more, "_page", lambda url, **kw: html)
    jobs = ats_more.sfrmk_search(Board("sfrmk", "jobs.zf.com", "ZF"), "steering")
    assert [j.job_id for j in jobs] == ["123456700", "223456700"]
    assert jobs[0].title == "Software Engineer - Steering" and jobs[0].countries == ["US"] and jobs[0].found_by == "steering"
    assert jobs[0].url == "https://jobs.zf.com/job/Northville-Software-Engineer-MI-48167/123456700/"
    assert jobs[1].countries == ["DE"]


def test_deep_list_merges_searches_and_survives_failures(monkeypatch):
    b = Board("workday", "gm|wd5|Careers", "GM", focus=True)

    def fake(board, max_pages=5, max_age_days=7, search="", us_only=False):
        if search == "boom":
            raise ATS.FetchError("HTTP 500", 500)
        ids = {"": ["1", "2"], "AUTOSAR": ["2", "3"], "Simulink": ["3", "4"]}[search]
        return [Job("workday", board.board, i, "GM", f"Job {i}", found_by=search) for i in ids]
    monkeypatch.setattr(ATS, "workday_list", fake)
    stats = Counter()
    jobs = {j.job_id: j for j in deep.deep_list(b, ["AUTOSAR", "boom", "Simulink"], {}, stats)}
    assert sorted(jobs) == ["1", "2", "3", "4"]
    assert jobs["1"].found_by == "" and jobs["2"].found_by == "AUTOSAR" and jobs["4"].found_by == "Simulink"
    assert stats["searches"] == 2 and stats["search_failed"] == 1 and stats["search_hits"] == 4


def test_deep_list_stops_searching_when_rate_limited(monkeypatch):
    calls = []

    def fake(board, max_pages=5, max_age_days=7, search="", us_only=False):
        calls.append(search)
        if search:
            raise ATS.FetchError("rate limited", 429)
        return [Job("workday", board.board, "1", "GM", "Job 1")]
    monkeypatch.setattr(ATS, "workday_list", fake)
    jobs = deep.deep_list(Board("workday", "gm|wd5|Careers", "GM", focus=True), ["a", "b", "c"], {})
    assert len(jobs) == 1 and calls == ["", "a"]


# ------------------------------------------------------------------ profile files

def test_profile_paths_and_activation(monkeypatch):
    for mod, names in ((S, ("DATA", "STATE_PATH", "MATCHES_PATH", "REJECTED_PATH")), (R, ("REPORT_PATH",)),
                       (D, ("DOCS", "PAGE_PATH"))):
        for n in names:
            monkeypatch.setattr(mod, n, getattr(mod, n))   # restored after the test
    p = P.activate("automotive")
    assert S.STATE_PATH == os.path.join(ROOT, "data", "automotive", "state.json")
    assert S.MATCHES_PATH.endswith(os.path.join("data", "automotive", "matches.csv"))
    assert R.REPORT_PATH == os.path.join(ROOT, "REPORT_AUTOMOTIVE.md")
    assert D.PAGE_PATH == os.path.join(ROOT, "docs", "automotive", "index.html")
    assert os.path.exists(p["config"]) and os.path.exists(p["focus"])
    with pytest.raises(SystemExit):
        P.activate("../etc")
    with pytest.raises(SystemExit):
        P.activate("nosuchprofile")


def test_focus_board_list_is_well_formed():
    boards = S.load_focus_boards(os.path.join(AUTO, "companies.csv"))
    assert len(boards) >= 60 and all(b.focus for b in boards)
    assert len({b.key.lower() for b in boards}) == len(boards)
    for b in boards:
        assert b.ats in S.VALID_ATS and b.ats != "auto"
        if b.ats == "workday":
            assert len(b.board.split("|")) == 3, b.board
        if b.ats in ("oracle", "eightfold", "taleo"):
            assert "|" in b.board, b.board
    assert not any("nexteer" in b.company.lower() for b in boards)


def test_search_terms_and_strong_skills_line_up(cfg, rules):
    """Every keyword we search for should itself count as evidence, or the search finds jobs we then drop."""
    for term in cfg["search"]["terms"]:
        weight, _, names = skill_hits(f"Experience with {term} in an automotive ECU project", rules)
        assert weight >= 3 and names, term


# ------------------------------------------------------------------ full offline run with the profile

def _point_at_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(S, "MATCHES_PATH", str(tmp_path / "matches.csv"))
    monkeypatch.setattr(S, "REJECTED_PATH", str(tmp_path / "rejected.csv"))
    monkeypatch.setattr(S, "DATA", str(tmp_path))
    monkeypatch.setattr(S, "CAREERS_BOARDS_PATH", str(tmp_path / "none.csv"))
    monkeypatch.setattr(R, "REPORT_PATH", str(tmp_path / "REPORT_AUTOMOTIVE.md"))
    monkeypatch.setattr(D, "DOCS", str(tmp_path / "docs"))
    monkeypatch.setattr(D, "PAGE_PATH", str(tmp_path / "docs" / "index.html"))
    monkeypatch.setattr(M, "CONFIG_PATH", os.path.join(AUTO, "config.yaml"))
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_TOPIC", "GEMINI_API_KEY", "OPENROUTER_API_KEY",
                "GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "NVIDIA_API_KEY", "GITHUB_MODELS_TOKEN",
                "MISTRAL_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_full_profile_run(tmp_path, monkeypatch):
    _point_at_tmp(tmp_path, monkeypatch)
    focus_csv = tmp_path / "focus.csv"
    focus_csv.write_text("ats,board,company,priority\nworkday,oem|wd5|Careers,OEM Motors,\nlever,tier1,Tier One,\n")
    monkeypatch.setattr(M, "FOCUS_PATH", str(focus_csv))
    monkeypatch.setattr(M, "load_boards", lambda: [Board("lever", "tier1", "Tier One (general list)"),
                                                   Board("greenhouse", "saas", "SaaS Co"),
                                                   Board("greenhouse", "nexteer", "Nexteer Automotive")])
    monkeypatch.setattr(M, "load_discovered", lambda known, *a, **k: [])
    now = datetime.now(timezone.utc)
    recent = now - timedelta(days=2)
    searched = []

    def wd(board, max_pages=5, max_age_days=7, search="", us_only=False):
        searched.append(search)
        def j(i, title, **kw):
            return Job("workday", board.board, i, "OEM Motors", title, location="Warren, Michigan, United States",
                       url=f"https://oem/{i}", posted_at=recent, needs_detail=True, detail_ref=i, found_by=search, **kw)
        if not search:
            return [j("W1", "Embedded Controls Engineer"), j("W2", "Software Engineer"), j("W9", "Plant Manager")]
        if search == "AUTOSAR":
            return [j("W3", "Technical Professional 3"), j("W4", "Engineer - Seats")]
        return []

    wd_text = {"W1": RESUME_JOB, "W2": WEB_JOB, "W3": RESUME_JOB, "W4": "Seat frames. Python."}

    def wd_detail(job):
        if job.job_id == "W1" and not wd_detail.fail_once:
            wd_detail.fail_once = True
            raise ATS.FetchError("HTTP 503", 503)
        job.description = wd_text[job.job_id]
    wd_detail.fail_once = False

    lever_jobs = [
        Job("lever", "tier1", "L1", "Tier One", "Steering Software Engineer", location="Saginaw, MI", url="https://t/1",
            posted_at=recent, description=RESUME_JOB),
        Job("lever", "tier1", "L2", "Tier One", "Program Analyst", location="Troy, MI", url="https://t/2",
            posted_at=recent, description=RESUME_JOB),                      # no title match: kept on its text
        Job("lever", "tier1", "L3", "Tier One", "Embedded Software Engineer", location="Munich, Germany",
            url="https://t/3", posted_at=recent, description=RESUME_JOB),   # not in the US
    ]

    def gh(board, content=False):
        if board.board == "nexteer":
            raise AssertionError("the current employer must not be fetched")
        return [Job("greenhouse", "saas", "G1", "SaaS Co", "Software Engineer", location="Austin, TX",
                    url="https://s/1", posted_at=recent, description=RESUME_JOB),     # generic title, general board
                Job("greenhouse", "saas", "G2", "SaaS Co", "Firmware Engineer", location="Austin, TX",
                    url="https://s/2", posted_at=recent, description="Firmware in embedded C for our sensor. RTOS.")]

    monkeypatch.setattr(ATS, "workday_list", wd)
    monkeypatch.setitem(ATS.DETAILERS, "workday", wd_detail)
    monkeypatch.setitem(ATS.LISTERS, "lever", lambda b: [Job(**j.__dict__) for j in lever_jobs])
    monkeypatch.setitem(ATS.LISTERS, "greenhouse", gh)

    assert M.main(["--dry-run"]) == 0
    rows = {r["uid"].split(":")[-1]: r for r in csv.DictReader(open(tmp_path / "matches.csv"))}
    st = json.loads((tmp_path / "state.json").read_text())
    # first run searched every keyword on the Workday focus board
    cfg_terms = yaml.safe_load(open(os.path.join(AUTO, "config.yaml")))["search"]["terms"]
    assert sorted(searched) == sorted([""] + cfg_terms)
    assert set(rows) == {"W3", "L1", "L2", "G2"}
    assert rows["W3"]["category"] == "wide" and "AUTOSAR Classic" in rows["W3"]["has"]
    assert rows["L1"]["category"] == "core" and rows["L1"]["status"] == "alert" and int(rows["L1"]["score"]) >= 80
    assert rows["L1"]["company"] == "Tier One"                       # the focus list's row replaced the general one
    assert rows["L2"]["category"] == "wide" and rows["G2"]["category"] == "core"
    # W1's description failed once: not decided yet, retried next run. W2/W4 lacked the tools: dropped.
    assert "workday:oem|wd5|Careers:W1" not in st["seen"] and st["text_retries"] == {"workday:oem|wd5|Careers:W1": 1}
    assert "workday:oem|wd5|Careers:W2" in st["seen"] and "workday:oem|wd5|Careers:W4" in st["seen"]
    report = (tmp_path / "REPORT_AUTOMOTIVE.md").read_text()
    assert report.startswith("# Auto Radar: latest matches") and "Steering Software Engineer" in report
    page = (tmp_path / "docs" / "index.html").read_text()
    assert "<h1>Auto Radar</h1>" in page and '"jobradar.automotive.marks.v1"' in page and "All open" in page

    # second run: W1 is read now; a rotating slice of keywords is searched; L1 was taken down
    searched.clear()
    lever_jobs.pop(0)
    assert M.main(["--dry-run"]) == 0
    rows = {r["uid"].split(":")[-1]: r for r in csv.DictReader(open(tmp_path / "matches.csv"))}
    st = json.loads((tmp_path / "state.json").read_text())
    assert "W1" in rows and rows["W1"]["category"] == "core" and st["text_retries"] == {}
    assert len(searched) == 1 + 9 and st["term_cursor"] == 9
    assert st["closed"] == {"lever:tier1:L1": now.date().isoformat()}
    report = (tmp_path / "REPORT_AUTOMOTIVE.md").read_text()
    assert "Steering Software Engineer" not in report and "Embedded Controls Engineer" in report

    # third run: the posting is back, so it is listed again
    lever_jobs.insert(0, Job("lever", "tier1", "L1", "Tier One", "Steering Software Engineer", location="Saginaw, MI",
                             url="https://t/1", posted_at=recent, description=RESUME_JOB))
    assert M.main(["--dry-run", "--full-sweep"]) == 0
    st = json.loads((tmp_path / "state.json").read_text())
    assert st["closed"] == {} and "Steering Software Engineer" in (tmp_path / "REPORT_AUTOMOTIVE.md").read_text()


def test_wide_cap_defers_instead_of_dropping(tmp_path, monkeypatch):
    _point_at_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(M, "FOCUS_PATH", "")
    real = M.load_config()
    real["scoring"]["max_wide_per_run"] = 2
    monkeypatch.setattr(M, "load_config", lambda: real)
    monkeypatch.setattr(M, "load_boards", lambda: [Board("greenhouse", "robo", "Robo")])
    monkeypatch.setattr(M, "load_discovered", lambda known, *a, **k: [])
    now = datetime.now(timezone.utc)
    monkeypatch.setitem(ATS.LISTERS, "greenhouse", lambda b: [
        Job("greenhouse", "robo", str(i), "Robo", "Controls Engineer", location="Detroit, MI", url=f"https://r/{i}",
            posted_at=now - timedelta(days=1), needs_detail=True, detail_ref=str(i)) for i in range(5)])
    monkeypatch.setitem(ATS.DETAILERS, "greenhouse", lambda j: setattr(j, "description", RESUME_JOB))
    for expected in (2, 4, 5):
        assert M.main(["--dry-run"]) == 0
        assert len(list(csv.DictReader(open(tmp_path / "matches.csv")))) == expected


def test_ai_resume_check_reviews_new_jobs_and_saved_ones(tmp_path, monkeypatch):
    from radar import ai_fit
    _point_at_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(M, "FOCUS_PATH", "")
    real = M.load_config()
    real["ai"]["mode"] = "resume"
    monkeypatch.setattr(M, "load_config", lambda: real)
    monkeypatch.setattr(M, "load_boards", lambda: [Board("greenhouse", "robo", "Robo")])
    monkeypatch.setattr(M, "load_discovered", lambda known, *a, **k: [])
    monkeypatch.setattr(ai_fit, "PROVIDERS", {k: v[:5] + (0.0,) for k, v in ai_fit.PROVIDERS.items()})
    now = datetime.now(timezone.utc)
    names = ["A", "B", "C"]
    monkeypatch.setitem(ATS.LISTERS, "greenhouse", lambda b: [
        Job("greenhouse", "robo", n, "Robo", f"Controls Engineer {n}", location="Detroit, MI", url=f"https://r/{n}",
            posted_at=now - timedelta(days=1), needs_detail=True, detail_ref=n) for n in names])
    monkeypatch.setitem(ATS.DETAILERS, "greenhouse", lambda j: setattr(j, "description", RESUME_JOB))

    # no AI key yet: matches are saved unchecked
    assert M.main(["--dry-run"]) == 0
    rows = list(csv.DictReader(open(tmp_path / "matches.csv")))
    assert len(rows) == 3 and not any(r["ai"] for r in rows)

    sent = []

    def post(url, json=None, **kw):
        user = json["messages"][1]["content"]
        sent.append(user)
        bad = "Engineer C" in user
        answer = {"fit": "no" if bad else "yes", "score": 15 if bad else 90, "matched": ["Simulink"],
                  "missing": [], "min_years": 3, "level": "mid", "clearance": False, "sponsor": "no",
                  "role": "controls", "why": "web job" if bad else "steering controls", "evidence": "x"}
        class R:
            status_code, headers, text = 200, {}, ""
            def json(self):
                return {"choices": [{"message": {"content": __import__("json").dumps(answer)}}]}
        return R()
    monkeypatch.setattr(ai_fit.requests, "post", post)
    monkeypatch.setenv("NVIDIA_API_KEY", "test")
    names.append("D")   # a new job, checked as it is found
    assert M.main(["--dry-run"]) == 0
    rows = {r["uid"].split(":")[-1]: r for r in csv.DictReader(open(tmp_path / "matches.csv"))}
    assert set(rows) == {"A", "B", "D"}                       # C moved to the hidden list by the AI
    assert all(r["ai"] == "keep" and r["ai_score"] == "90" for r in rows.values())
    assert "has: Simulink" in rows["A"]["ai_why"] and len(sent) == 4
    rejected = list(csv.DictReader(open(tmp_path / "rejected.csv")))
    assert [r["reject_reason"] for r in rejected if r["uid"].endswith(":C")] == ["AI: not this kind of job (web job)"]
    report = (tmp_path / "REPORT_AUTOMOTIVE.md").read_text()
    assert "| Has |" in report and "steering controls" in rows["A"]["ai_why"]

    # third run: nothing left to check, nothing sent again
    sent.clear()
    assert M.main(["--dry-run"]) == 0
    assert sent == []


# ------------------------------------------------------------------ readers added for more employers

def test_workday_switches_to_us_results_when_a_board_has_too_many(monkeypatch):
    calls = []
    facets = [{"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "Location_Country", "descriptor": "Country",
         "values": [{"descriptor": "India", "id": "in1"}, {"descriptor": "United States of America", "id": "us1"}]}]}]

    def fake_post(url, payload, **kw):
        calls.append(payload)
        us = payload["appliedFacets"] == {"Location_Country": ["us1"]}
        where = "Troy, MI" if us else "Bangalore, India"
        n = 25 if us else 500
        start = payload["offset"]
        rows = [{"title": f"Engineer {i}", "externalPath": f"/job/x/E_{where[:2]}_{i}", "postedOn": "Posted Today",
                 "bulletFields": [f"{where[:2]}{i}"], "locationsText": where} for i in range(start, min(n, start + 20))]
        return {"total": n if start == 0 else 0, "jobPostings": rows, "facets": facets if start == 0 else []}
    monkeypatch.setattr(ATS, "post_json", fake_post)
    b = Board("workday", "aptiv|wd5|APTIV_CAREERS", "Aptiv")
    us = ATS.workday_list(b, max_pages=3, search="software", us_only=True)
    assert len(us) == 25 and all(j.location == "Troy, MI" for j in us)       # all US results, none skipped
    assert calls[0]["appliedFacets"] == {} and calls[1]["appliedFacets"] == {"Location_Country": ["us1"]}
    calls.clear()
    plain = ATS.workday_list(b, max_pages=3, search="software")              # the DevOps radar is unchanged
    assert len(plain) == 60 and all(c["appliedFacets"] == {} for c in calls)
    assert ATS.workday_us_facet([{"facetParameter": "jobFamilyGroup", "values": [{"descriptor": "IT", "id": "1"}]}]) is None
    assert ATS._wd_hosts("nxp||careers")[0] == "https://nxp.myworkdayjobs.com/wday/cxs/nxp/careers"


def test_newer_successfactors_sites_are_searched_through_their_json_service(monkeypatch):
    monkeypatch.setattr(ats_more.robots, "check", lambda url: None)
    monkeypatch.setattr(ats_more, "get_text", lambda url, **kw: "<body class='unify'>j2w.SearchResultsUnify.x()</body>")
    posts = []

    def fake_post(url, payload, **kw):
        posts.append((url, payload))
        if payload["pageNumber"]:
            return {"jobSearchResult": [], "totalJobs": 1}
        return {"totalJobs": 1, "jobSearchResult": [{"response": {
            "id": "5488", "unifiedStandardTitle": "Embedded Controls Engineer", "urlTitle": "Embedded-Controls-Engineer",
            "jobLocationShort": ["Fargo, ND, USA, "], "country_obj": ["United States"], "unifiedStandardStart": "9/1/26"}}]}
    monkeypatch.setattr(ats_more, "post_json", fake_post)
    jobs = ats_more.sfrmk_search(Board("sfrmk", "join.cnh.com", "CNH"), "Simulink")
    assert posts[0][0] == "https://join.cnh.com/services/recruiting/v1/jobs" and posts[0][1]["keywords"] == "Simulink"
    j = jobs[0]
    assert (j.title, j.location, j.countries, j.found_by) == ("Embedded Controls Engineer", "Fargo, ND, USA",
                                                              ["United States"], "Simulink")
    assert j.url == "https://join.cnh.com/job/Embedded-Controls-Engineer/5488-en_US" and j.posted_at.year == 2026


def test_mcloud_dspace_and_jibe_readers(monkeypatch):
    monkeypatch.setattr(ats_more.robots, "check", lambda url: None)
    seen = []

    def fake_get(url, params=None, **kw):
        seen.append((url, params))
        if "m-cloud" in url:
            return {"totalHits": 1, "queryResult": [{
                "id": 24112984, "title": "Software Architect - Safety Embedded", "entity_status": "Open",
                "primary_city": "Auburn Hills", "primary_state": "MI", "primary_country": "US",
                "open_date": "2026-11-05T10:02:21.587Z", "description": "<p>AUTOSAR and ISO 26262</p>",
                "url": "https://careers.stellantis.com/job/24112984/software-architect"}]}
        return {"totalCount": 1, "jobs": [{"data": {"slug": "34042", "title": "Staff Embedded Software Engineer",
                                                     "full_location": "Palo Alto, California", "country_code": "US",
                                                     "description": "<p>RTOS</p>", "posted_date": "2026-11-01"}}]}
    monkeypatch.setattr(ats_more, "get_json", fake_get)
    st = ats_more.mcloud_list(Board("mcloud", "2016|careers.stellantis.com", "Stellantis"), search="AUTOSAR")
    assert seen[-1][1]["SearchText"] == "AUTOSAR" and seen[-1][1]["facet"] == "primary_country:US"
    assert st[0].location == "Auburn Hills, MI, US" and "ISO 26262" in st[0].description and st[0].found_by == "AUTOSAR"
    rv = ats_more.jibe_list(Board("jibe", "careers.rivian.com", "Rivian"), search="RTOS")
    assert seen[-1][1]["keywords"] == "RTOS" and "sortBy" not in seen[-1][1] and rv[0].found_by == "RTOS"
    assert {"jibe", "mcloud"} <= set(deep.SEARCHERS)

    page = ('[{"filterterms":["term-ort-4"],"href":"/en/inc/home/career/jobfinder/stellen.cfm?fuseaction=einzel&jid=38016&t=x",'
            '"popularity":0,"timestamp":"0","location":"Wixom, MI","code":"INC-ADSE",'
            '"title":"Embedded Software Developer - Automated Driving","description":"INC"},'
            '{"filterterms":[],"href":"/en/inc/x.cfm?fuseaction=einzel&jid=1&t=y","location":"Paderborn","code":"P",'
            '"title":"Softwareentwickler","description":""}]')
    monkeypatch.setattr(ats_more, "get_text", lambda url, **kw: page)
    ds = ats_more.dspace_list(Board("dspace", "www.dspace.com|en/inc/home/career/jobfinder/stellen.cfm", "dSPACE"))
    assert [(j.job_id, j.title, j.location, j.countries) for j in ds] == [
        ("38016", "Embedded Software Developer - Automated Driving", "Wixom, MI", ["US"]),
        ("1", "Softwareentwickler", "Paderborn", [])]
    assert ds[0].url.startswith("https://www.dspace.com/en/inc/home/career/jobfinder/stellen.cfm?fuseaction=einzel&jid=38016")


def test_review_fixes_davinci_foreign_places_and_unreadable_pages(rules, monkeypatch):
    # "da Vinci" the surgical robot is not Vector DaVinci
    assert "DaVinci" not in skill_hits("Work on the da Vinci surgical system in embedded C.", rules)[2]
    assert "DaVinci" in skill_hits("AUTOSAR configuration with DaVinci Developer and CANoe.", rules)[2]
    assert "DaVinci" in skill_hits("Experience with da Vinci Configurator.", rules)[2]
    from radar.match import classify_location
    for place in ("Padua, Veneto, IT", "King Abdullah Economic City, 02", "東京都中央区", "Acuña, COA, MX", "Vantaa, Uusimaa, FI"):
        assert classify_location(place) == "non_us", place
    for place in ("Auburn Hills, MI", "Columbus, IN", "Troy, Michigan, US", "PA 17111, United States"):
        assert classify_location(place) == "us", place
    for t in ("Staff Software Engineer - Postgres Control Plane", "Care Management Assistant II - BSW - Reidsville"):
        assert title_category(t, rules, focus=True) is None, t

    # SuccessFactors sitemap: a US state + ZIP in the address marks a US job; a German state + postcode does not
    assert ats_more._rmk_slug_place("Harrisburg-Embedded-Software-Test-Engineer-PA-17111") == ("PA 17111, United States", ["US"])
    assert ats_more._rmk_slug_place("Ulm-Embedded-Software-Expert-BW-89081") == ("", [])

    # A job page whose text is filled in by script: title from the page head, text left unread (not the menus)
    monkeypatch.setattr(ats_more.robots, "check", lambda url: None)
    page = ("<html><head><title>Embedded Software Test Engineer Job Details | TE Connectivity</title></head>"
            "<body class='unify'><div id='header'>Careers Home Search Jobs AUTOSAR Simulink menu</div></body></html>")
    monkeypatch.setattr(ats_more, "get_text", lambda url, **kw: page)
    j = Job(ats="sfrmk", board="careers.te.com", job_id="1", company="TE", title="Harrisburg Embedded Software Test Engineer PA 17111",
            title_guess=True, detail_ref="https://careers.te.com/job/x/1/", needs_detail=True)
    ats_more.sfrmk_detail(j)
    assert j.title == "Embedded Software Test Engineer" and j.description == ""
