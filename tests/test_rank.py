"""Rule-based fit score (radar/rank.py) and rescoring of saved matches."""
import csv
import os
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from radar import rank

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTO = os.path.join(ROOT, "profiles", "automotive")
NOW = datetime.now(timezone.utc)

GOOD = """About the role
Develop EPS steering controls software for our OEM customers.
What you'll do:
- Design Simulink and Stateflow models, generate code with Embedded Coder
- Integrate AUTOSAR software components with DaVinci Developer
- Run MIL/SIL testing against requirements in DOORS
Basic Qualifications
Bachelor's degree in EE with 5+ years of experience, or Master's degree with 3+ years of experience
Experience with ISO 26262, MISRA C and Polyspace
Preferred Qualifications
HIL testing with dSPACE
Benefits
Medical, dental and vision insurance. Automotive discounts.
We are an equal opportunity employer without regard to race, color or veteran status."""

LINUX = """Responsibilities: Develop Linux kernel device drivers and BSP for our SoC. Bring up new boards.
Requirements: 8+ years of experience with embedded Linux, Yocto, device drivers, FPGA and Verilog. C programming.
About Us: we are a leading automotive supplier."""


@pytest.fixture(scope="module")
def ranker():
    cfg = yaml.safe_load(open(os.path.join(AUTO, "config.yaml")))
    return rank.Ranker.load(cfg, AUTO)


def test_sections_split_and_boilerplate_dropped():
    s = rank.sections(GOOD)
    assert "Master's degree" in s["required"] and "dSPACE" in s["preferred"]
    assert "Simulink and Stateflow" in s["duties"]
    assert "equal opportunity" in s["boiler"] and "Automotive discounts" in s["boiler"]
    # a content line that starts like a header stays content
    s = rank.sections("Requirements\nExperience with Simulink and AUTOSAR\nKnowledge of ISO 26262")
    assert "Experience with Simulink" in s["required"] and "Knowledge of ISO 26262" in s["required"]


def test_inline_headers_in_one_long_paragraph():
    s = rank.sections("We build steering software. Responsibilities: write Simulink models. "
                      "Preferred Qualifications: HIL with dSPACE. Benefits: 401(k) match.")
    assert "Simulink" in s["duties"] and "dSPACE" in s["preferred"] and "401(k)" in s["boiler"]


@pytest.mark.parametrize("text,years", [
    ("BS with 5+ years of experience or MS with 3+ years of experience", 3),
    ("Bachelor's degree and 4+ years of experience; Master's degree and 2+ years of experience", 2),
    ("Minimum 7 years of experience in embedded software. 2+ years with AUTOSAR experience", 7),
    ("3-5 years of relevant experience in controls", 3),
    ("Our company has over 100 years of history. 4 years of experience required.", 4),
    ("No experience requirement listed.", None),
])
def test_years_are_read_with_the_masters_path(text, years):
    assert rank.years_required(text, "master")[0] == years


def test_target_job_scores_high_and_lists_what_it_has(ranker):
    f = ranker.score("Embedded Software Engineer - Steering Controls", GOOD, location="Auburn Hills, MI",
                     posted=NOW - timedelta(days=2), now=NOW)
    assert f.score >= 85
    assert {"Simulink", "AUTOSAR Classic", "ISO 26262", "Polyspace"} <= set(f.has)
    assert f.years == 3 and "MS path" in f.years_note
    assert "HIL testing" not in f.missing            # only preferred, so not listed as missing


def test_job_needing_skills_the_resume_lacks_scores_low(ranker):
    f = ranker.score("Senior Embedded Linux BSP Engineer", LINUX, location="Austin, TX", posted=NOW, now=NOW)
    assert f.score < 55
    assert {"Device drivers/BSP", "Embedded Linux/kernel"} <= set(f.missing)
    assert f.coverage < 0.5


def test_boilerplate_mentions_do_not_count(ranker):
    only_boiler = "Benefits\nWe use MATLAB, Simulink, AUTOSAR and ISO 26262 in our offices.\n"
    assert ranker.score("Software Engineer", only_boiler, now=NOW).got == 0


def test_required_hil_is_missing(ranker):
    f = ranker.score("Controls Engineer", "Qualifications: 4+ years of experience in controls, Simulink, CAN, "
                     "C/C++. HIL experience required.", now=NOW)
    assert "HIL testing" in f.missing and "CAN/CAN FD" in f.has


def test_text_store_keeps_days_and_reloads(tmp_path):
    st = rank.TextStore(str(tmp_path / "texts"), keep_days=10)
    st.put("a:1", "text one")
    st.save("2026-11-01")
    st.put("a:2", "text two")
    st.save("2026-11-20")            # the 2026-11-01 file is now older than 10 days
    again = rank.TextStore(str(tmp_path / "texts")).load()
    assert again.get("a:2") == "text two" and again.get("a:1") == ""


def test_rescoring_moves_jobs_to_hidden_and_back(tmp_path, monkeypatch):
    """A rules change applies to saved jobs: a job that no longer fits is hidden, and comes back."""
    from radar import ats as ATS
    from radar import main as M
    from radar.models import Board, Job
    from tests.test_automotive import RESUME_JOB, _point_at_tmp
    _point_at_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(M, "FOCUS_PATH", "")
    monkeypatch.setattr(M, "load_boards", lambda: [Board("greenhouse", "robo", "Robo")])
    monkeypatch.setattr(M, "load_discovered", lambda known, *a, **k: [])
    monkeypatch.setitem(ATS.LISTERS, "greenhouse", lambda b: [
        Job("greenhouse", "robo", n, "Robo", f"Controls Engineer {n}", location="Detroit, MI", url=f"https://r/{n}",
            posted_at=NOW - timedelta(days=1), description=RESUME_JOB) for n in ("A", "B")])
    assert M.main(["--dry-run"]) == 0
    rows = {r["uid"][-1]: r for r in csv.DictReader(open(tmp_path / "matches.csv"))}
    assert set(rows) == {"A", "B"} and rows["A"]["has"] and rows["A"]["detail"].startswith("skills")
    assert os.listdir(tmp_path / "texts")

    real_score = rank.Ranker.score

    def no_tools_for_a(self, title, desc, **kw):
        f = real_score(self, title, desc, **kw)
        if title.endswith(" A"):
            f.got, f.has = 0, []
        return f
    monkeypatch.setattr(rank.Ranker, "score", no_tools_for_a)
    assert M.main(["--dry-run"]) == 0
    rows = {r["uid"][-1]: r for r in csv.DictReader(open(tmp_path / "matches.csv"))}
    hidden = [r for r in csv.DictReader(open(tmp_path / "rejected.csv")) if r["uid"].endswith(":A")]
    assert set(rows) == {"B"} and "none of your tools" in hidden[-1]["reject_reason"]

    monkeypatch.setattr(rank.Ranker, "score", real_score)
    assert M.main(["--dry-run"]) == 0
    rows = {r["uid"][-1]: r for r in csv.DictReader(open(tmp_path / "matches.csv"))}
    assert set(rows) == {"A", "B"}
    assert not [r for r in csv.DictReader(open(tmp_path / "rejected.csv")) if r["uid"].endswith(":A")]


def test_ai_off_ignores_ai_keys(tmp_path, monkeypatch):
    from radar import ai, ai_fit
    from radar import ats as ATS
    from radar import main as M
    from radar.models import Board, Job
    from tests.test_automotive import RESUME_JOB, _point_at_tmp
    _point_at_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(M, "FOCUS_PATH", "")
    monkeypatch.setattr(M, "load_boards", lambda: [Board("greenhouse", "robo", "Robo")])
    monkeypatch.setattr(M, "load_discovered", lambda known, *a, **k: [])
    monkeypatch.setitem(ATS.LISTERS, "greenhouse", lambda b: [
        Job("greenhouse", "robo", "1", "Robo", "Controls Engineer", location="Detroit, MI", url="https://r/1",
            posted_at=NOW, description=RESUME_JOB)])
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    called = []
    monkeypatch.setattr(ai.requests, "post", lambda *a, **k: called.append(1))
    monkeypatch.setattr(ai_fit.requests, "post", lambda *a, **k: called.append(1))
    assert M.main(["--dry-run"]) == 0
    assert called == [] and len(list(csv.DictReader(open(tmp_path / "matches.csv")))) == 1


def test_calibration_agreement_and_tuning(tmp_path):
    from radar import calibrate
    (tmp_path / "labels.csv").write_text("label,uid\napply,a\nno,b\nmaybe,c\n,d\n")
    (tmp_path / "marks.json").write_text('{"e": {"s": "applied"}, "f": {"s": "skipped"}, "g": {"s": "saved"}}')
    labels = calibrate.load_labels(str(tmp_path))
    assert labels == {"a": 1.0, "b": 0.0, "c": 0.5, "e": 1.0, "f": 0.0, "g": 1.0}
    assert calibrate.handled_uids(str(tmp_path)) == {"b", "e", "f"}
    assert calibrate.concordance([(90, 1.0), (50, 0.0), (70, 0.5)]) == 1.0
    assert calibrate.concordance([(50, 1.0), (90, 0.0)]) == 0.0
    # verdicts that follow the title, not the skills: tuning should move weight to the title
    pl = [({"skills": 0.9, "title": t, "experience": 0.8, "domain": 0.8, "similarity": 0.5, "logistics": 0.7}, t)
          for t in (0.0, 0.2, 0.5, 0.8, 1.0)] * 3
    pl += [({"skills": 0.2, "title": 1.0, "experience": 0.8, "domain": 0.8, "similarity": 0.5, "logistics": 0.7}, 1.0)]
    w, q = calibrate.tune(pl, dict(rank.DEFAULT_WEIGHTS), tries=1500)
    assert q > calibrate.concordance([(rank.combine(p, rank.DEFAULT_WEIGHTS), v) for p, v in pl])


def test_full_workday_read_is_complete_and_skips_searches(monkeypatch):
    from radar import ats as ATS
    from radar import deep
    from radar.models import Board
    calls = []

    def fake_post(url, payload, **kw):
        calls.append(payload)
        start, n = payload["offset"], 45
        rows = [{"title": f"Engineer {i}", "externalPath": f"/job/x/E_{i}", "postedOn": "Posted 30+ Days Ago",
                 "bulletFields": [f"R{i}"], "locationsText": "Troy, MI"} for i in range(start, min(n, start + 20))]
        return {"total": n if start == 0 else 0, "jobPostings": rows, "facets": []}
    monkeypatch.setattr(ATS, "post_json", fake_post)
    b = Board("workday", "oem|wd5|Careers", "OEM", focus=True)
    jobs = deep.deep_list(b, ["AUTOSAR", "Simulink"], {"full_list": ["workday"], "full_max_pages": 150})
    assert len(jobs) == 45 and jobs.complete
    assert all(p["searchText"] == "" for p in calls)          # no keyword searches after a full read
    # without full reading: old jobs end the listing and keyword searches run
    calls.clear()
    jobs = deep.deep_list(b, ["AUTOSAR"], {"list_max_pages": 10, "list_max_age_days": 30})
    assert not jobs.complete and any(p["searchText"] == "AUTOSAR" for p in calls)


def test_coverage_file_and_broken_board_warning(tmp_path, monkeypatch):
    from radar import main as M
    from radar import store as S
    from radar.models import Board
    monkeypatch.setattr(S, "MATCHES_PATH", str(tmp_path / "m.csv"))
    focus = [Board("workday", "a|wd1|x", "Alpha", focus=True), Board("lever", "b", "Beta", focus=True)]
    state = {"coverage_prev": {"lever:b": 40}}
    warn = M.write_coverage(str(tmp_path / "coverage.csv"), focus,
                            {"workday:a|wd1|x": {"listed": 120, "complete": True}, "lever:b": {"listed": 0}},
                            state, NOW, 60)
    rows = list(csv.DictReader(open(tmp_path / "coverage.csv")))
    assert {r["company"]: r["jobs_listed"] for r in rows} == {"Alpha": "120", "Beta": "0"}
    assert len(warn) == 1 and "Beta" in warn[0] and state["coverage_prev"]["workday:a|wd1|x"] == 120
