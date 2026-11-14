"""Readers added for more hiring systems and big employers' career sites, and the board finder's patterns
(fake responses, no network)."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import radar.ats_more as M
from radar.models import Board
from radar.sources import yc_jobs  # the conftest stubs the module attribute; keep the real one
import radar.sources as S

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import discover_boards as D  # noqa: E402

NOW = datetime.now(timezone.utc)


def B(ats, board):
    return Board(ats=ats, board=board, company="")


def test_zoho(monkeypatch):
    rows = [{"id": "11", "Posting_Title": "DevOps Engineer", "City": "Austin", "State": "TX", "Country": "United States",
             "Publish": True, "Remote_Job": False},
            {"id": "12", "Posting_Title": "Hidden", "Publish": False}]
    page = '<input type="hidden" value="%s" id="jobs">' % json.dumps(rows).replace('"', "&#34;")
    monkeypatch.setattr(M, "_page", lambda url, **k: page)
    jobs = M.zoho_list(B("zoho", "acme"))
    assert [(j.job_id, j.title, j.location) for j in jobs] == [("11", "DevOps Engineer", "Austin, TX, United States")]
    assert jobs[0].url.startswith("https://acme.zohorecruit.com/jobs/Careers/11/")


def test_personio(monkeypatch):
    xml = """<?xml version="1.0"?><workzag-jobs><position><id>7</id><office>New York</office>
      <name>Site Reliability Engineer</name><createdAt>2026-11-10T10:00:00+00:00</createdAt>
      <jobDescriptions><jobDescription><name>Req</name><value><![CDATA[<p>2+ years of Kubernetes</p>]]></value></jobDescription></jobDescriptions>
      </position></workzag-jobs>"""
    monkeypatch.setattr(M, "_page", lambda url, **k: xml)
    j = M.personio_list(B("personio", "acme.jobs.personio.com"))[0]
    assert (j.title, j.location, j.url) == ("Site Reliability Engineer", "New York", "https://acme.jobs.personio.com/job/7")
    assert "Kubernetes" in j.description and j.posted_at.day == 10


def test_catsone(monkeypatch):
    pages = {"https://acme.catsone.com/robots.txt": "User-agent: *\nSitemap: https://sitemap.catsone.com/?siteID=1\n",
             "https://sitemap.catsone.com/?siteID=1": """<urlset><url><loc>https://acme.catsone.com/careers/2010-General/</loc></url>
             <url><loc>https://acme.catsone.com/careers/2010-General/jobs/1234-Cloud-Engineer-JP99/</loc></url></urlset>"""}
    monkeypatch.setattr(M, "_page", lambda url, **k: pages[url])
    jobs = M.catsone_list(B("catsone", "acme"))
    assert [(j.job_id, j.title) for j in jobs] == [("1234", "Cloud Engineer")]


def test_careerpuck(monkeypatch):
    monkeypatch.setattr(M, "get_json", lambda url, **k: {"jobs": [
        {"permalink": "abc", "status": "public", "title": "Platform Engineer", "content": "&lt;p&gt;Terraform&lt;/p&gt;",
         "locations": [{"city": "Denver", "state": "CO", "country": "United States"}]}]})
    j = M.careerpuck_list(B("careerpuck", "acme"))[0]
    assert (j.title, j.location, j.description) == ("Platform Engineer", "Denver, CO, United States", "Terraform")


def test_amazon(monkeypatch):
    rows = [{"id_icims": "1", "title": "DevOps Engineer", "posted_date": NOW.strftime("%B %d, %Y"),
             "normalized_location": "Seattle, Washington, USA", "job_path": "/en/jobs/1/devops",
             "basic_qualifications": "- 1+ years of Linux", "company_name": "Amazon Web Services"},
            {"id_icims": "2", "title": "Old Job", "posted_date": (NOW - timedelta(days=40)).strftime("%B %d, %Y")}]
    monkeypatch.setattr(M, "get_json", lambda url, **k: {"jobs": rows})
    monkeypatch.setattr(M.robots, "check", lambda url: None)
    jobs = M.amazon_list(B("amazon", "us"))
    assert [j.job_id for j in jobs] == ["1"]
    assert jobs[0].company == "Amazon Web Services" and "1+ years of Linux" in jobs[0].description
    assert jobs[0].url == "https://www.amazon.jobs/en/jobs/1/devops" and not jobs[0].company_guess


def test_tiktok_us_only(monkeypatch):
    city = lambda name, country: {"en_name": name, "parent": {"en_name": country}}
    monkeypatch.setattr(M, "post_json", lambda url, body, **k: {"data": {"job_post_list": [
        {"id": "1", "title": "SRE", "city_info": city("San Jose", "United States of America"), "requirement": "BS"},
        {"id": "2", "title": "SRE", "city_info": city("Singapore", "Singapore")}]}})
    jobs = M.tiktok_list(B("tiktok", "us"))
    assert [(j.job_id, j.location) for j in jobs] == [("1", "San Jose, United States of America")]


def test_yc_jobs(monkeypatch):
    props = {"props": {"jobPostings": [{"id": 5, "title": "DevOps Engineer", "url": "/companies/acme/jobs/x-devops",
                                         "location": "San Francisco, CA, US / Remote", "companyName": "Acme"}]}}
    page = '<div data-page="%s"></div>' % json.dumps(props).replace('"', "&quot;")
    monkeypatch.setattr(S, "get_text", lambda url, **k: page)
    monkeypatch.setattr(S.time, "sleep", lambda s: None)
    j = yc_jobs({"roles": ["devops"]})[0]
    assert (j.company, j.title, j.workplace) == ("Acme", "DevOps Engineer", "remote")
    assert j.url == "https://www.ycombinator.com/companies/acme/jobs/x-devops"


def test_board_finder_patterns():
    fns = {a: ex for a, _, ex in D.SOURCES}
    cases = {
        "https://aapc.applytojob.com/apply/EKX5RzRMrE": ("jazzhr", "aapc"),
        "http://jobs.jobvite.com/aarete/job/o5Pfzfws/apply": ("jobvite", "aarete"),
        "https://abudhabi-nyu.icims.com/jobs/14817/x/job": ("icims", "abudhabi-nyu.icims.com"),
        "https://www.icims.com/": ("icims", None),
        "https://jobs.dayforcehcm.com/en-US/acv/CANDIDATEPORTAL": ("dayforce", "acv|CANDIDATEPORTAL"),
        "https://ally.avature.net/careers/JobDetail/x/123": ("avature", "ally.avature.net|careers"),
        "https://eabc.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/5": ("oracle", "eabc.fa.us2.oraclecloud.com|CX_1"),
        "https://3rtalent.zohorecruit.com/jobs/Careers": ("zoho", "3rtalent"),
        "https://acme.jobs.personio.de/": ("personio", "acme.jobs.personio.de"),
        "https://app.careerpuck.com/job-board/academia/job/6e5": ("careerpuck", "academia"),
        "https://join.com/companies/02100/15023229-x": ("join", "02100"),
        "https://acato.homerun.co/": ("homerun", "acato"),
    }
    for url, (ats, want) in cases.items():
        assert fns[ats](urlsplit(url)) == want, url


def test_job_page_without_structured_data_uses_page_text(monkeypatch):
    from radar.models import Job
    page = ("<html><body><nav>Jobs Home</nav><main><h1>Java Developer</h1><h2>Requirements</h2>"
            "<p>3+ years of Java and Spring Boot</p></main><footer>Privacy</footer></body></html>")
    monkeypatch.setattr(M, "_page", lambda url, **k: page)
    j = Job(ats="jazzhr", board="acme", job_id="1", company="Acme", title="Java Developer", location="", url="u")
    M._ld_detail(j)
    assert "3+ years of Java" in j.description and "Privacy" not in j.description and "Jobs Home" not in j.description
