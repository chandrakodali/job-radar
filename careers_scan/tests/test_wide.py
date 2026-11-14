"""US-wide sweep: Common Crawl index query, careers-URL scoring, hint scanner, archive copy, page signals."""
import gzip
from datetime import datetime
from urllib.parse import urlsplit

import pytest

from careers_scan.fetch import Page
from careers_scan.urls import registrable_domain
from careers_scan.wide import ccindex, hint


def R(url, lang="eng", status=200, mime="text/html", query=None):
    u = urlsplit(url)
    host = u.hostname
    dom = registrable_domain(host)
    return (url, host, host.rsplit(".", 1)[-1], dom, dom, u.path or "/", query, datetime(2026, 10, 20, 12, 0, 0),
            status, mime, lang, "crawl-data/x.warc.gz", 100, 200)


def test_sweep_file_finds_one_careers_page_per_site(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    urls = ["https://www.ranenetwork.com/about/career-page", "https://www.ranenetwork.com/about",
            "https://acme.com/careers", "https://acme.com/jobs", "https://acme.com/careers/devops-engineer",
            "https://acme.com/careers/site-reliability-engineer/", "https://acme.com/blog/careers-in-tech",
            "https://acme.com/blog/jobs", "https://careers.widgets.io/", "https://foo.co.uk/careers",
            "https://bar.de/jobs", "https://bar.xn--p1ai/careers", "https://www.indeed.com/jobs",
            "https://beta.org/employment-opportunities.html", "https://beta.org/en/company/work-with-us/",
            "https://gamma.com/jobs/1234", "https://delta.com/a/b/c/careers", "https://epsilon.ai/hiring",
            "https://eta.com/jobs/platform-engineer", "https://theta.com/Careers/"]
    rows = [R(u) for u in urls] + [R("https://baz.com/jobs", query="page=2"), R("https://qux.com/careers", lang="deu"),
                                   R("https://zeta.com/careers", mime="application/pdf")]
    con = duckdb.connect()
    con.execute("CREATE TABLE t (url VARCHAR, url_host_name VARCHAR, url_host_tld VARCHAR, "
                "url_host_registered_domain VARCHAR, url_host_private_domain VARCHAR, url_path VARCHAR, "
                "url_query VARCHAR, fetch_time TIMESTAMP, fetch_status SMALLINT, content_mime_detected VARCHAR, "
                "content_languages VARCHAR, warc_filename VARCHAR, warc_record_offset INTEGER, "
                "warc_record_length INTEGER)")
    con.executemany("INSERT INTO t VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    p = str(tmp_path / "part-00000.zstd.parquet")
    con.execute(f"COPY t TO '{p}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    got = ccindex.sweep_file(con, p)
    assert set(got) == {"ranenetwork.com", "acme.com", "widgets.io", "beta.org", "gamma.com", "epsilon.ai",
                        "eta.com", "theta.com"}
    assert got["ranenetwork.com"]["url"] == "https://www.ranenetwork.com/about/career-page"
    a = got["acme.com"]
    assert a["url"] == "https://acme.com/careers" and a["alt_url"] == "https://acme.com/jobs"
    assert a["n_detail"] == 2 and a["detail_url"].startswith("https://acme.com/careers/")
    assert a["warc"] == "crawl-data/x.warc.gz 100 200" and a["fetch_time"] == "2026-10-20"
    assert got["widgets.io"]["url"] == "https://careers.widgets.io/"
    assert got["beta.org"]["url"].endswith("/employment-opportunities.html")
    assert got["beta.org"]["alt_url"].endswith("/work-with-us/")
    assert got["eta.com"]["url"] == "https://eta.com/jobs" and got["eta.com"]["warc"] == ""   # derived landing
    assert got["gamma.com"]["url"] == "https://gamma.com/jobs"
    assert got["theta.com"]["url"] == "https://theta.com/Careers/"

    out = tmp_path / "cc-0.csv.gz"
    assert ccindex.write_csv(str(out), got.values()) == 8
    merged = {}
    for d in ccindex.read_csv(str(out)):
        ccindex.merge_into(merged, d)
    ccindex.merge_into(merged, {"domain": "acme.com", "url": "http://www.acme.com/careers/", "score": 12,
                                "n_detail": 3, "n_landing": 1})
    assert merged["acme.com"]["url"] == "https://acme.com/careers" and merged["acme.com"]["n_detail"] == 5
    assert merged["acme.com"]["alt_url"] == "https://acme.com/jobs"


def test_classify_path():
    c = ccindex.classify_path
    assert c("acme.com", "acme.com", "/careers")[:2] == ("landing", 12)
    assert c("acme.com", "acme.com", "/careers.html")[0] == "landing"
    assert c("acme.com", "acme.com", "/join-us")[0] == "landing"
    assert c("acme.com", "acme.com", "/careers/1006") == ("detail", 12, "/careers")
    assert c("acme.com", "acme.com", "/blog/jobs") is None
    assert c("acme.com", "acme.com", "/careers/page") is None
    assert c("acme.com", "acme.com", "/") is None and c("jobs.acme.com", "acme.com", "/")[0] == "landing"


class Fake:
    def __init__(self, pages):
        self.pages, self.calls = {k.rstrip("/").lower(): v for k, v in pages.items()}, []

    def page(self, url, robots=True):
        self.calls.append(url)
        v = self.pages.get(url.rstrip("/").lower())
        if v is None:
            return Page(url=url, status=404, ctype="text/html", text="<title>404 Not Found</title>")
        return v if isinstance(v, Page) else Page(url=url, status=200, ctype="text/html", text=v)

    def json(self, url):
        raise AssertionError("the hint scanner must not call job-board APIs")


RANE = """<html><head><title>RANE Careers</title></head><body><h1>Careers</h1>
<h3>RANE Enterprise Account Manager</h3><p>Virtual role, open to applicants authorized to work in the US.</p>
<p>Please send your CV along with any other application documents to
<a href="mailto:careers@ranenetwork.com?subject=RANE%20Careers%20Application">careers@ranenetwork.com</a></p>
<p>To apply, please email careers@ranenetwork.com.</p></body></html>"""


def scan(pages, domain, url, alt=""):
    f = Fake(pages)
    row, page = hint.HintScanner(f, probe=False).scan_hint(domain, url, alt)
    return row, page, f


def test_hint_rane_email_page():
    u = "https://www.ranenetwork.com/about/career-page"
    row, page, f = scan({u: RANE}, "ranenetwork.com", u)
    assert row["category"] == "email" and row["apply_emails"] == "careers@ranenetwork.com"
    assert row["careers_url"] == u and "RANE Enterprise Account Manager" in row["roles_sample"]
    assert f.calls == [u] and page["html"] == RANE
    assert hint.us_class(page["text"]) == "us" and hint.guess_name(page["html"], page["title"], "ranenetwork.com") == "RANE"


def test_hint_other_outcomes():
    row, _, _ = scan({"https://acme.com/careers": '<title>Careers</title><a href="https://boards.greenhouse.io/acme">Open roles</a>'},
                     "acme.com", "https://acme.com/careers")
    assert row["category"] == "ats" and row["ats"] == "greenhouse"
    pages = {"https://beta.org/careers": '<title>Careers at Beta</title><h1>Open positions</h1>'
                                         '<a href="/careers/devops-engineer">DevOps Engineer</a>',
             "https://beta.org/careers/devops-engineer": "<title>DevOps Engineer - Beta</title>"
                                                         "<p>To apply, email your resume to jobs@beta.org</p>"}
    row, _, f = scan(pages, "beta.org", "https://beta.org/careers")
    assert row["category"] == "email" and row["apply_emails"] == "jobs@beta.org" and len(f.calls) == 2
    row, _, _ = scan({}, "gone.com", "https://gone.com/careers")
    assert row["category"] == "unreachable"
    wall = Page(url="https://wall.com/careers", status=403, ctype="text/html", text="<title>Just a moment...</title>")
    row, _, _ = scan({"https://wall.com/careers": wall}, "wall.com", "https://wall.com/careers")
    assert row["category"] == "blocked"
    moved = Page(url="https://jobs.lever.co/delta", status=200, ctype="text/html", text="<title>Delta jobs</title>")
    row, _, _ = scan({"https://delta.com/careers": moved}, "delta.com", "https://delta.com/careers")
    assert row["category"] == "ats" and row["ats"] == "lever"
    row, _, f = scan({"https://alt.com/jobs": '<title>Jobs</title><p>Send your resume to hr@alt.com</p>'},
                     "alt.com", "https://alt.com/careers", alt="https://alt.com/jobs")
    assert row["category"] == "email" and len(f.calls) == 2


def test_archive_copy_and_signals():
    html = "<title>Careers</title><p>Email jobs@acme.com — café</p>".encode()
    http = b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n" + html
    rec = b"WARC/1.0\r\nWARC-Type: response\r\nWARC-Target-URI: https://acme.com/careers\r\n\r\n" + http + b"\r\n\r\n"
    text = hint.parse_warc_record(gzip.compress(rec))
    assert "café" in text and "jobs@acme.com" in text
    row, _ = hint.HintScanner(hint.OnePageFetcher("https://acme.com/careers", text), probe=False).scan_hint(
        "acme.com", "https://acme.com/careers")
    assert row["category"] == "email"
    assert hint.archived_page(None, "bad") == ""
    assert hint.us_class("123 Main St, Austin, TX 78701. Call (512) 555-1212. Equal Opportunity Employer.") == "us"
    assert hint.us_class("Acme Ltd, London, United Kingdom. Salary £40,000. +44 20 7946 0000") == "non_us"
    assert hint.us_class("We build software.") == "unknown"
    g = hint.guess_name
    assert g('<meta property="og:site_name" content="RANE Network">', "RANE Careers", "ranenetwork.com") == "RANE Network"
    assert g("", "Careers | Acme Corp", "acme.com") == "Acme Corp" and g("", "Jobs at Initech", "initech.com") == "Initech"
    assert g("", "Careers", "blue-sky.io") == "Blue Sky"
    assert hint.TECH_RE.search("Junior DevOps Engineer") and not hint.TECH_RE.search("Enterprise Account Manager")
