import csv
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from careers_scan import apply_list, radar_link, watch
from careers_scan.fetch import Page
from careers_scan.roles import (extract_roles, infra_category, is_careers_url, is_title, load_rules, mailto,
                                read_job_page)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NOW = datetime(2026, 11, 11, 13, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def rules():
    return load_rules(os.path.join(ROOT_DIR, "config.yaml"))[0]


CAREERS = """<html><body><nav><a href="/careers">Careers</a><a href="/jobs">Jobs</a></nav>
<h1>Careers at Acme</h1><p>We are hiring great people. Join our team!</p>
<h2>Open positions</h2>
<ul>
<li><a href="/careers/devops-engineer"><h3>DevOps Engineer</h3><span>Remote (US)</span></a></li>
<li><a href="/careers/sre"><h3>Senior Site Reliability Engineer</h3><span>Austin, TX</span></a></li>
<li><div class=job><strong>Cloud Infrastructure Engineer – New York, NY</strong><p>Send your CV to jobs@acme.com</p></div></li>
<li><a href="/careers/marketing">Marketing Manager</a></li>
<li><strong>Junior Platform Engineer</strong> <a href="https://forms.gle/abc">Apply</a></li>
<li><a href="mailto:hiring@acme.com?subject=SRE">Associate SRE</a></li>
</ul>
<h3>Meet our Platform Engineer team</h3>
<p>Our infrastructure engineer works with Kubernetes.</p>
<ul><li>5+ years of experience in DevOps, SRE, or Infrastructure Engineering</li>
<li>Experience with cloud platforms (GCP, AWS)</li></ul>
<p>2019 DevOps Leader of the Year</p><p>Isaac Van Doren (Platform Engineer</p>
<p>AWS Certified Developer</p><p>DevOps CI/CD Cloud Network Docker Kubernetes Infrastructure AWS Azure GCP</p>
<footer>Kubernetes Engineer jobs in our footer</footer>
</body></html>"""


def test_extract_roles_keeps_titles_and_their_own_details(rules):
    roles = {r.title: r for r in extract_roles(CAREERS, "https://acme.com/careers")}
    assert set(roles) == {"DevOps Engineer", "Senior Site Reliability Engineer", "Cloud Infrastructure Engineer",
                          "Marketing Manager", "Junior Platform Engineer", "Associate SRE"}
    d = roles["DevOps Engineer"]
    assert d.url == "https://acme.com/careers/devops-engineer" and d.location == "Remote (US)" and d.emails == []
    assert roles["Senior Site Reliability Engineer"].location == "Austin, TX"
    c = roles["Cloud Infrastructure Engineer"]
    assert c.location == "New York, NY" and c.emails == ["jobs@acme.com"] and c.url == "https://acme.com/careers"
    assert roles["Junior Platform Engineer"].apply_url == "https://forms.gle/abc"
    assert roles["Associate SRE"].emails == ["hiring@acme.com"]
    # The radar classifies senior infrastructure titles before checking experience.
    infra = {t for t in roles if infra_category(t, rules)}
    assert infra == {
        "DevOps Engineer", "Senior Site Reliability Engineer", "Cloud Infrastructure Engineer",
        "Junior Platform Engineer", "Associate SRE",
    }


@pytest.mark.parametrize("text, ok", [
    ("DevOps Engineer", True), ("Site Reliability Engineer (SRE)", True), ("Platform Engineer (Rust)", True),
    ("Founding Infrastructure Engineer", True), ("DevOps / SRE", True),
    ("DevOps", False), ("Cloud & DevOps", False), ("DevOps Team", False),
    ("5+ years of experience in DevOps", False), ("Experience with Kubernetes", False),
    ("2019 DevOps Leader of the Year", False), ("Isaac Van Doren (Platform Engineer", False),
    ("AWS Certified Developer", False), ("Meet our Platform Engineer team", False),
    ("Why join us as a DevOps Engineer?", False), ("Subject line: DevOps Engineer Application", False),
    ("Our infrastructure engineer works with Kubernetes.", False), ("Careers", False),
    ("QA Automated Testing Manual QA Engineer Playwright Cypress Selenium WebdriverIO CI/CD", False),
])
def test_is_title(text, ok):
    assert is_title(text) is ok


def test_is_careers_url():
    assert is_careers_url("https://acme.com/careers")
    assert is_careers_url("https://careers.acme.com/")
    assert not is_careers_url("https://acme.com/")
    assert not is_careers_url("https://acme.com")


def test_read_job_page():
    html = """<html><title>DevOps Engineer</title><body><h1>DevOps Engineer</h1><p>Location: Chicago, IL</p>
    <p>We need 1-2 years of AWS experience.</p><p>To apply, email your resume to jobs@acme.com or
    fill <a href="https://forms.gle/x1">this form</a>.</p><p>Questions? sales@other.com</p></body></html>"""
    d = read_job_page(html, "https://acme.com/careers/devops", {"acme.com"})
    assert d["location"] == "Chicago, IL"
    assert d["emails"] == ["jobs@acme.com"]
    assert d["apply_url"] == "https://forms.gle/x1"
    assert "AWS experience" in d["description"] and not d["blocked"]


def test_mailto_link_has_subject():
    m = mailto("jobs@acme.com", "DevOps Engineer", "Acme & Co")
    assert m.startswith("mailto:jobs@acme.com?subject=Application%3A%20DevOps%20Engineer")
    assert " " not in m and "&" not in m.split("?", 1)[1].replace("%26", "")


# ------------------------------------------------------------------ apply list

class Pages:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def page(self, url, robots=True):
        self.calls.append(url)
        v = self.pages.get(url)
        if v is None:
            return Page(url=url, status=404, text="")
        return Page(url=url, status=200, text=v)


def _write_classified(root, rows):
    os.makedirs(os.path.join(root, "data"), exist_ok=True)
    fields = ["domain", "name", "apply_emails", "form_url", "doc_url", "yc_batch", "stage"]
    with open(os.path.join(root, "data", "classified.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def test_build_and_save_apply_list(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT_DIR)
    root = str(tmp_path)
    _write_classified(root, [{"domain": "acme.com", "name": "Acme", "apply_emails": "careers@acme.com",
                              "yc_batch": "W24", "stage": "Early"},
                             {"domain": "euro.de", "name": "Euro GmbH"}, {"domain": "gov.us", "name": "GovCo"}])
    results = [
        {"domain": "acme.com", "name": "Acme", "url": "https://acme.com/careers", "final_url": "https://acme.com/careers",
         "roles": extract_roles(CAREERS, "https://acme.com/careers")},
        {"domain": "euro.de", "name": "Euro", "url": "https://euro.de/jobs",
         "roles": extract_roles("<h3>DevOps Engineer</h3><p>Berlin, Germany</p>", "https://euro.de/jobs")},
        {"domain": "gov.us", "name": "GovCo", "url": "https://gov.us/careers",
         "roles": extract_roles('<a href="/careers/sre1"><h3>Cloud Engineer</h3></a>', "https://gov.us/careers")},
    ]
    pages = Pages({
        "https://acme.com/careers/devops-engineer": "<h1>DevOps Engineer</h1><p>0-1 years of experience. "
                                                     "Remote within the US.</p>",
        "https://gov.us/careers/sre1": "<h1>Cloud Engineer</h1><p>Location: Washington, DC</p>"
                                       "<p>Active Secret clearance required.</p>",
    })
    rows, new, had = apply_list.build(results, pages, root, now=NOW)
    assert not had and len(new) == len(rows)
    by = {(x["company"], x["title"]): x for x in rows}
    devops = by[("Acme", "DevOps Engineer")]
    assert devops["apply_kind"] == "email" and devops["apply"] == "careers@acme.com"   # company address
    assert devops["job_url"] == "https://acme.com/careers/devops-engineer" and devops["status"] in ("alert", "quiet")
    assert "0-2 yrs exp" in devops["flags"]
    assert by[("Acme", "Cloud Infrastructure Engineer")]["apply"] == "jobs@acme.com"  # address in its own card
    assert by[("Acme", "Junior Platform Engineer")]["apply_kind"] == "email"         # company email beats form
    assert by[("Euro GmbH", "DevOps Engineer")]["status"] == "outside_us"
    assert by[("GovCo", "Cloud Engineer")]["status"] == "blocked"
    senior = by[("Acme", "Senior Site Reliability Engineer")]
    assert senior["status"] in ("alert", "quiet")
    assert "senior title, years not stated" in senior["flags"]
    assert ("Acme", "Marketing Manager") not in by
    assert "https://acme.com/careers/devops-engineer" in pages.calls  # job page was opened

    apply_list.save(rows, root, now=NOW)
    md = open(os.path.join(root, "ROLES.md")).read()
    assert "mailto:careers@acme.com?subject=" in md and "## Outside the US (1)" in md and "clearance" in md
    assert list(csv.DictReader(open(os.path.join(root, "data", "roles.csv"))))[0]["status"] in ("alert", "quiet")

    rows2, new2, had2 = apply_list.build(results, pages, root, now=NOW + timedelta(hours=12))
    assert had2 and new2 == []
    assert all(x["first_seen"] == by[(x["company"], x["title"])]["first_seen"] for x in rows2)


def test_state_forgets_roles_gone_for_60_days(tmp_path):
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "data"))
    old = (NOW - timedelta(days=61)).isoformat()
    with open(os.path.join(root, "data", "roles_state.json"), "w") as fh:
        json.dump({"x.com|devops engineer": {"first_seen": old, "last_seen": old}, "bad": {}}, fh)
    apply_list.save([], root, now=NOW)
    assert json.load(open(os.path.join(root, "data", "roles_state.json"))) == {}


def test_watch_alerts_only_new_roles_after_first_run(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT_DIR)
    monkeypatch.setattr(watch, "ROOT", str(tmp_path))
    _write_classified(str(tmp_path), [{"domain": "acme.com", "name": "Acme", "apply_emails": "careers@acme.com"}])
    (tmp_path / "data" / "watchlist.csv").write_text("domain,name,url,category\nacme.com,Acme,https://acme.com/careers,email\n")
    html = {"v": "<h1>Careers</h1><ul><li><h3>DevOps Engineer</h3></li></ul><p>" + "x " * 40 + "</p>"}

    class F:
        def page(self, url, robots=True):
            return Page(url=url, status=200, text=html["v"])
    monkeypatch.setattr(watch, "Fetcher", lambda: F())
    sent = []

    class N:
        configured = True

        def __init__(self, *a, **k):
            pass

        def send_text(self, body, title="", click=""):
            sent.append((title, body))
    import radar.notify
    monkeypatch.setattr(radar.notify, "Notifier", N)
    assert watch.main([]) == 0
    assert sent == []                                   # first run: record what's open, no alerts
    assert "DevOps Engineer" in (tmp_path / "ROLES.md").read_text()
    html["v"] = html["v"].replace("<li><h3>DevOps Engineer</h3></li>",
                                  "<li><h3>DevOps Engineer</h3></li><li><h3>Junior Cloud Engineer</h3></li>")
    assert watch.main([]) == 0
    assert len(sent) == 1 and "Junior Cloud Engineer" in sent[0][1] and "careers@acme.com" in sent[0][1]
    assert watch.main([]) == 0
    assert len(sent) == 1                               # nothing new: no repeat alert


# ------------------------------------------------------------------ radar hand-off

@pytest.mark.parametrize("url, expected", [
    ("https://boards.greenhouse.io/acme", ("greenhouse", "acme")),
    ("https://job-boards.eu.greenhouse.io/acme/jobs/1", ("greenhouse", "eu:acme")),
    ("https://boards.greenhouse.io/embed/job_board/js?for=acme", ("greenhouse", "acme")),
    ("https://boards-api.greenhouse.io/v1/boards/acme/jobs", ("greenhouse", "acme")),
    ("https://boards-api.greenhouse.io/v1/boards/", None),
    ("https://jobs.lever.co/acme/123", ("lever", "acme")),
    ("https://jobs.eu.lever.co/acme", ("lever", "eu:acme")),
    ("https://api.lever.co/v0/postings/acme?mode=json", ("lever", "acme")),
    ("https://jobs.ashbyhq.com/Acme.com/abc", ("ashby", "Acme.com")),
    ("https://api.ashbyhq.com/posting-api/job-board/acme", ("ashby", "acme")),
    ("https://jobs.ashbyhq.com/", None),
    ("https://apply.workable.com/acme/", ("workable", "acme")),
    ("https://apply.workable.com/j/ABC123", None),
    ("https://jobs.smartrecruiters.com/AcmeInc/123", ("smartrecruiters", "AcmeInc")),
    ("https://acme.bamboohr.com/careers", ("bamboohr", "acme")),
    ("https://acme.recruitee.com/", ("recruitee", "acme")),
    ("https://acme.breezy.hr/p/1", ("breezy", "acme")),
    ("https://acme.applytojob.com/apply", ("jazzhr", "acme")),
    ("https://ats.rippling.com/acme/jobs", ("rippling", "acme")),
    ("https://acme.wd5.myworkdayjobs.com/en-US/External", ("workday", "acme|wd5|External")),
    ("https://jobs.dover.com/Acme/123", ("dover", "Acme")),
    ("https://www.lever.co/", None),
    ("not a url", None),
])
def test_radar_board(url, expected):
    assert radar_link.radar_board(url) == expected


def test_radar_link_build_skips_known_and_duplicates():
    rows = [{"category": "ats", "ats_url": "https://jobs.lever.co/acme", "name": "Acme", "domain": "acme.com"},
            {"category": "ats_probe", "ats_url": "https://jobs.ashbyhq.com/newco", "name": "NewCo", "domain": "newco.ai"},
            {"category": "ats", "ats_url": "https://jobs.ashbyhq.com/NewCo", "name": "NewCo", "domain": "newco.ai"},
            {"category": "email", "ats_url": "https://jobs.lever.co/zzz", "name": "Z", "domain": "z.com"},
            {"category": "ats", "ats_url": "https://grnh.se/abc", "name": "Short", "domain": "s.com"},
            {"category": "ats", "ats_url": "https://20210212011927_km2w.applytojob.com/apply", "name": "J", "domain": "j.com"}]
    out = radar_link.build(rows, {"lever:acme"})
    assert [(x["ats"], x["board"]) for x in out] == [("ashby", "newco")]


def test_real_false_positives_from_first_role_run():
    page = """<html><body><h1>Careers</h1>
    <div class="card"><h3>DevOps Engineer</h3><p>Kubernetes, Cloud</p><a href="https://acme.notion.site/DevOps-1">Read</a></div>
    <div class="card"><h3>Digital Community Manager</h3><a href="https://acme.notion.site/Community-2">Read</a></div>
    <div class="card"><h3>Infrastructure Engineer</h3><p>Hybrid &amp; Multi-Cloud</p></div>
    <ul class="menu"><li><a href="/solutions/by-role/devops--sre">DevOps / SRE</a></li>
    <li><a href="/cybersecurity-solutions/?hire=Site%20Reliability%20Engineers">Site Reliability Engineers (SRE)</a></li></ul>
    <h2>OpenSRE's SRE Agent is now Open Source</h2>
    </body></html>"""
    roles = {r.title: r for r in extract_roles(page, "https://acme.com/careers")}
    assert "DevOps / SRE" not in roles and "Site Reliability Engineers (SRE)" not in roles
    assert not any("Open Source" in t for t in roles)
    assert roles["DevOps Engineer"].location == ""            # "Kubernetes, Cloud" is not a place
    assert roles["DevOps Engineer"].apply_url == "https://acme.notion.site/DevOps-1"
    assert roles["Infrastructure Engineer"].location == ""
    assert roles["Infrastructure Engineer"].apply_url == ""   # not the community manager's link
    # a product page passed as the careers page yields nothing
    assert extract_roles("<h3>DevOps Engineer</h3>", "https://acme.com/solutions/devops") == []


@pytest.mark.parametrize("text, ok", [
    ("Remote (US)", True), ("Austin, TX", True), ("Berlin, Germany", True), ("London, United Kingdom", True),
    ("Hybrid - New York", True), ("On-site in San Diego, CA", True), ("Seattle, WA, USA (Remote ok)", True),
    ("Fully remote", True), ("Remote within the US", True), ("Remote - EMEA", True),
    ("Kubernetes, Cloud", False), ("Hybrid & Multi-Cloud", False), ("Member of Technical Staff, In", False),
    ("Program manager, engineering", False), ("Platform Engineer", False), ("", False),
])
def test_looks_like_location(text, ok):
    from careers_scan.roles import looks_like_location
    assert looks_like_location(text) is ok


def test_company_document_link_is_not_used_for_other_roles(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT_DIR)
    root = str(tmp_path)
    _write_classified(root, [{"domain": "daytona.io", "name": "Daytona",
                              "doc_url": "https://daytonaio.notion.site/Digital-Community-Manager-1",
                              "form_url": ""}])
    results = [{"domain": "daytona.io", "name": "Daytona", "url": "https://daytona.io/careers",
                "roles": extract_roles("<h3>Infrastructure Engineer</h3>", "https://daytona.io/careers")}]
    rows, _, _ = apply_list.build(results, Pages({}), root, now=NOW)
    assert rows[0]["apply_kind"] == "page" and rows[0]["apply"] == "https://daytona.io/careers"
