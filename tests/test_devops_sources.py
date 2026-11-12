"""DevOps radar: the extra job feeds, the newest-first weekly report, and the senior / years / adjacent
title rules (all with fake data, no network)."""
import csv
from datetime import datetime, timedelta, timezone

import radar.sources as sources
from radar import report as R
from radar import store as S
from radar.match import Rules, assess, title_category
from radar.models import Board, Job
from radar.main import load_config

# the conftest replaces these on the module during tests; keep the real ones
from radar.sources import hn_jobs, remoteok_jobs, usajobs_jobs, workable_search_jobs, wwr_jobs  # noqa: E402

NOW = datetime.now(timezone.utc).replace(microsecond=0)
ISO = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_workable_search_pages_and_filters(monkeypatch):
    pages = {
        None: {"jobs": [
            {"id": "a", "title": "DevOps Engineer", "created": ISO(NOW - timedelta(days=1)), "url": "https://w/a",
             "location": {"city": "Austin", "subregion": "Texas", "countryName": "United States"},
             "locations": ["Austin, Texas, United States"], "company": {"title": "Acme"}, "workplace": "hybrid",
             "description": "<p>2+ years of experience with Terraform.</p>", "requirementsSection": "<p>AWS</p>"},
            {"id": "b", "title": "SRE", "created": ISO(NOW - timedelta(days=30)), "location": {"countryName": "United States"},
             "company": {"title": "Old Co"}}],
            "nextPageToken": "t2"},
        "t2": {"jobs": [
            {"id": "c", "title": "Cloud Engineer", "created": ISO(NOW - timedelta(hours=5)),
             "location": {"city": "Toronto", "countryName": "Canada"}, "company": {"title": "North"}},
            {"id": "d", "title": "Platform Engineer", "created": ISO(NOW - timedelta(hours=2)),
             "location": {"countryName": "United States"}, "locations": ["TELECOMMUTE"], "workplace": "remote",
             "company": {"title": "Remote Inc"}}]},
    }
    calls = []

    def fake(url, params=None, headers=None):
        calls.append(params.get("pageToken"))
        return pages[params.get("pageToken")]
    monkeypatch.setattr(sources, "get_json", fake)
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    jobs = {j.job_id: j for j in workable_search_jobs({"terms": ["devops"], "days": 7}, NOW)}
    assert calls == [None, "t2"]
    assert set(jobs) == {"a", "d"}                      # 30-day-old and Canadian jobs left out
    assert jobs["a"].company == "Acme" and jobs["a"].location == "Austin, Texas"
    assert "Terraform" in jobs["a"].description and "AWS" in jobs["a"].description
    assert jobs["d"].location == "Remote, US" and jobs["d"].workplace == "remote"
    assert jobs["a"].uid == "workable_search:all:a"


def test_workable_search_stops_when_token_is_ignored(monkeypatch):
    page = {"jobs": [{"id": "a", "title": "DevOps", "created": ISO(NOW), "location": {"countryName": "United States"},
                      "company": {"title": "A"}}], "nextPageToken": "again"}
    n = []
    monkeypatch.setattr(sources, "get_json", lambda url, params=None, headers=None: n.append(1) or page)
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    assert len(workable_search_jobs({"terms": ["devops"]}, NOW)) == 1 and len(n) == 2


def test_hn_who_is_hiring(monkeypatch):
    rules = Rules.from_config(load_config())
    story = {"title": "Ask HN: Who is hiring? (November 2026)", "objectID": "500", "created_at": ISO(NOW - timedelta(days=11))}
    item = {"children": [
        {"id": 1, "created_at": ISO(NOW - timedelta(days=2)),
         "text": "Acme Robotics | Senior DevOps Engineer, Site Reliability Engineer | Austin, TX | ONSITE<p>We run Kubernetes."},
        {"id": 2, "created_at": ISO(NOW - timedelta(days=2)),
         "text": "Paper Co | Account Executive | NYC<p>Sales."},
        {"id": 3, "created_at": ISO(NOW - timedelta(days=20)), "text": "Old Co | DevOps Engineer | Remote"},
        {"id": 4, "created_at": ISO(NOW - timedelta(days=1)),
         "text": "Cloudy (YC W24) | REMOTE (US)<p>We're hiring:<p>- Platform Engineer<p>- Designer"},
    ]}
    monkeypatch.setattr(sources, "get_json",
                        lambda url, params=None, headers=None: {"hits": [story]} if "search" in url else item)
    jobs = hn_jobs({"days": 7}, lambda t: title_category(t, rules) in ("core", "bridge"), NOW)
    got = {(j.company, j.title) for j in jobs}
    assert got == {("Acme Robotics", "Senior DevOps Engineer"), ("Acme Robotics", "Site Reliability Engineer"),
                   ("Cloudy", "Platform Engineer")}
    acme = [j for j in jobs if j.company == "Acme Robotics"][0]
    assert acme.location == "Austin, TX" and acme.url == "https://news.ycombinator.com/item?id=1"


def test_remoteok_and_wwr(monkeypatch):
    data = [{"legal": "notice"},
            {"id": "11", "date": ISO(NOW - timedelta(days=1)), "company": "Acme", "position": "DevOps Engineer",
             "location": "Worldwide", "tags": ["aws"], "url": "https://remoteok.com/remote-jobs/11", "description": "x"},
            {"id": "12", "date": ISO(NOW - timedelta(days=1)), "company": "EU Co", "position": "SRE",
             "location": "Berlin, Germany", "url": "u"},
            {"id": "13", "date": ISO(NOW - timedelta(days=9)), "company": "Old", "position": "SRE", "url": "u"}]
    monkeypatch.setattr(sources, "get_json", lambda url, params=None, headers=None: data)
    jobs = {j.job_id: j for j in remoteok_jobs({"days": 7}, NOW)}
    assert set(jobs) == {"11", "12"} and jobs["11"].location.startswith("Remote, US")
    assert jobs["12"].location == "Berlin, Germany"   # the US filter drops it later
    rss = f"""<?xml version="1.0"?><rss><channel>
      <item><title>Acme: DevOps Engineer</title><region>USA Only</region><link>https://weworkremotely.com/x</link>
        <pubDate>{(NOW - timedelta(days=1)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate><description>&lt;p&gt;AWS&lt;/p&gt;</description></item>
      <item><title>Old: SRE</title><link>https://weworkremotely.com/y</link>
        <pubDate>{(NOW - timedelta(days=12)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>
    </channel></rss>"""
    monkeypatch.setattr(sources, "get_text", lambda url, **k: rss)
    w = wwr_jobs({"days": 7}, NOW)
    assert len(w) == 1 and w[0].company == "Acme" and w[0].title == "DevOps Engineer"
    assert w[0].location == "USA Only" and w[0].url == "https://weworkremotely.com/x" and w[0].description == "AWS"


def test_usajobs_marks_citizenship_and_clearance(monkeypatch):
    def item(pid, who, clearance):
        return {"MatchedObjectDescriptor": {
            "PositionID": pid, "PositionTitle": "IT Specialist (DevOps)", "PositionURI": f"https://usajobs.gov/{pid}",
            "OrganizationName": "Agency", "PositionLocationDisplay": "Washington, DC",
            "PositionLocation": [{"CountryCode": "United States", "CountrySubDivisionCode": "DC"}],
            "PublicationStartDate": (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.0000"),
            "UserArea": {"Details": {"WhoMayApply": {"Name": who}, "SecurityClearance": clearance,
                                     "HiringPath": ["public"] if "citizen" in who.lower() else ["student"],
                                     "MajorDuties": ["Run CI/CD"]}}}}
    data = {"SearchResult": {"SearchResultCountAll": 2, "SearchResultItems": [
        item("A1", "United States Citizens", "Secret"), item("B2", "Students", "Not Required")]}}
    seen = []
    monkeypatch.setattr(sources, "get_json", lambda url, params=None, headers=None: seen.append(headers) or data)
    assert usajobs_jobs({"keywords": ["devops"]}, env={}) == []          # no key: off
    jobs = {j.job_id: j for j in usajobs_jobs({"keywords": ["devops"]},
                                              env={"USAJOBS_API_KEY": "k", "USAJOBS_EMAIL": "me@x"})}
    assert seen[0]["Authorization-Key"] == "k" and seen[0]["User-Agent"] == "me@x"
    assert "Requires U.S. citizenship." in jobs["A1"].description and "Secret security clearance" in jobs["A1"].description
    assert "citizenship" not in jobs["B2"].description.lower().split("who may apply")[0]
    rules = Rules.from_config(load_config())
    b = Board("usajobs", "federal", "Agency")
    assert assess(jobs["A1"], b, rules, "core", now=NOW).status == "blocked"


def test_adzuna_limits_are_per_profile(monkeypatch):
    calls = []
    monkeypatch.setattr(sources, "get_json", lambda url, params=None, headers=None: calls.append(1) or {"results": []})
    state = {"adzuna_calls": {"day": NOW.date().isoformat(), "d": 45, "week": NOW.strftime("%G-W%V"), "w": 45,
                              "month": NOW.strftime("%Y-%m"), "m": 45}}
    env = {"ADZUNA_APP_ID": "i", "ADZUNA_APP_KEY": "k"}
    sources.adzuna_jobs(state, {"terms": ["devops"], "calls_per_run": 1, "limits": {"d": 45}}, NOW, env=env)
    assert calls == []
    sources.adzuna_jobs(state, {"terms": ["devops"], "calls_per_run": 1, "limits": {"d": 46}}, NOW, env=env)
    assert calls == [1]


# ------------------------------------------------------------------ rules

def _job(**kw):
    base = dict(ats="greenhouse", board="acme", job_id="1", company="Acme", title="DevOps Engineer",
                location="Austin, TX", url="https://x", posted_at=NOW - timedelta(hours=3), description="")
    base.update(kw)
    return Job(**base)


def test_adjacent_titles_need_devops_work():
    rules = Rules.from_config(load_config())
    b = Board("greenhouse", "acme", "Acme")
    assert title_category("Network Engineer", rules) == "bridge"
    routing = "Configure Cisco routers and switches, BGP, OSPF, firewalls. " * 10
    assert assess(_job(title="Network Engineer", description=routing), b, rules, "bridge", now=NOW).status == "blocked"
    devopsy = routing + " Automate with Python and Ansible; deploy on AWS."
    assert assess(_job(title="Network Engineer", description=devopsy), b, rules, "bridge", now=NOW).status != "blocked"
    # a short or missing text is not enough to judge: kept
    assert assess(_job(title="Network Engineer", description=""), b, rules, "bridge", now=NOW).status != "blocked"


def test_location_read_from_text_when_board_says_remote():
    rules = Rules.from_config(load_config())
    b = Board("greenhouse", "acme", "Acme")
    a = assess(_job(location="Remote", description="This role is remote, open to candidates anywhere in the US. "
                                                    "2+ years of experience."), b, rules, "core", now=NOW)
    assert "location unclear" not in a.flags


# ------------------------------------------------------------------ newest-first report

def _row(uid, title, company, posted, location="Austin, TX", status="quiet", score=60, ats="greenhouse", flags=""):
    return {"first_seen": (posted + timedelta(minutes=30)).isoformat(), "posted": posted.isoformat(), "score": score,
            "status": status, "category": "core", "company": company, "title": title, "location": location,
            "workplace": "", "flags": flags, "url": f"https://x/{uid}", "ats": ats, "uid": uid}


def test_newest_report(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "MATCHES_PATH", str(tmp_path / "m.csv"))
    monkeypatch.setattr(S, "REJECTED_PATH", str(tmp_path / "r.csv"))
    rows = [
        _row("greenhouse:a:1", "DevOps Engineer", "Acme", NOW - timedelta(hours=2), score=40),
        _row("greenhouse:a:2", "DevOps Engineer", "Acme Inc", NOW - timedelta(hours=30), location="Denver, CO"),
        _row("adzuna:us:9", "DevOps Engineer", "ACME", NOW - timedelta(hours=1), ats="adzuna", location="Austin, TX"),
        _row("lever:b:1", "Site Reliability Engineer", "Beta", NOW - timedelta(days=3), score=95),
        _row("lever:b:2", "SRE", "Beta", NOW - timedelta(days=9)),                     # posted too long ago
        _row("lever:b:3", "Cloud Engineer", "Beta", NOW - timedelta(days=1), status="blocked",
             flags="asks for 7+ years of experience"),
    ]
    S.append_matches(rows)
    with open(S.MATCHES_PATH, newline="", encoding="utf-8") as f:
        saved = list(csv.DictReader(f))
    assert len(saved) == 6
    md = R.build_newest_report("America/Chicago", 100, now=NOW, view={"days": 7}, extra_lines=["Coverage check: x", ""])
    assert "**2 jobs**, newest first" in md                  # Acme's three listings are one row
    assert "2 repeat listings" in md and "+1 more" in md     # merged; the other city is noted
    assert md.index("Acme") < md.index("Site Reliability Engineer")   # newest first, not by score
    assert "https://x/greenhouse:a:1" in md and "adzuna:us:9" not in md   # the employer's own link wins
    assert "SRE](" not in md                                  # 9 days old
    assert "## Left out" in md and "asks for 7+ years" in md and "Coverage check" in md
    assert "Jobs by Adzuna" in md


# ------------------------------------------------------------------ SRE titles

SRE_TITLES = {
    "Site Reliability Engineer": "core", "SRE II": "core", "Senior SRE": "core", "SREs - Cloud Platform": "core",
    "Site Reliability Engineer - Voice Platform": "core", "Site-Reliability Engineer": "core",
    "Site Reliability Engineer, SAP Cloud": "core", "SRE - Manufacturing Systems": "core",
    "Software Engineer, Site Reliability": "core", "Software Engineer - Reliability": "core",
    "Reliability Engineer, Cloud Infrastructure": "core", "Infrastructure Reliability Engineer": "core",
    "Application Reliability Engineer": "core", "GPU Reliability Engineer": "core",
    "Database Reliability Engineer": "core", "Systems Development Engineer, AWS": "core",
    "Production Engineer, Infrastructure": "core", "Systems Engineer - SRE": "core",
    "Cloud Resiliency Engineer": "core", "DevOps/SRE Engineer": "core",
    "Reliability Engineer": "bridge", "Production Engineer": "bridge", "Technical Operations Engineer": "bridge",
    "TechOps Engineer": "bridge", "NOC Engineer": "bridge", "Monitoring Engineer": "bridge",
    "Staff Site Reliability Engineer": None, "SRE Lead": None, "Principal SRE": None, "SRE Intern": None,
    "Site Reliability Engineer IV": None, "SRE Manager": None, "Site Reliability Engineer - TS/SCI": None,
    "Mechanical Reliability Engineer": None, "Equipment Reliability Engineer, Manufacturing": None,
    "Reliability Technician": None,
}


def test_sre_titles():
    rules = Rules.from_config(load_config())
    assert {t: title_category(t, rules) for t in SRE_TITLES} == SRE_TITLES


def test_plain_reliability_engineer_needs_sre_work():
    rules = Rules.from_config(load_config())
    b = Board("greenhouse", "acme", "Acme")
    plant = ("Lead root cause analysis on rotating equipment, improve uptime, vibration analysis, CMMS, "
             "on-call rotation for plant outages, preventive maintenance with SAP PM. ") * 4
    assert assess(_job(title="Reliability Engineer", description=plant), b, rules, "bridge", now=NOW).status == "blocked"
    sre = ("Own SLOs and error budgets for our services on Kubernetes, write postmortems, "
           "automate with Python and Terraform on AWS. ") * 4
    assert assess(_job(title="Reliability Engineer", description=sre), b, rules, "bridge", now=NOW).status != "blocked"
