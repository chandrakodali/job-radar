import csv
import json
import os

import pytest
import requests

from careers_scan import fetch as fetch_mod
from careers_scan import merge as merge_mod
from careers_scan import sources, watch
from careers_scan.fetch import Fetcher, Page, _error_kind, decode_body
from careers_scan.scan import FIELDS, Company


# ------------------------------------------------------------------ sources

def test_load_yc_filters_status(tmp_path):
    p = tmp_path / "all.json"
    p.write_text(json.dumps([
        {"name": "Live", "website": "https://live.com", "status": "Active", "batch": "W24", "stage": "Early"},
        {"name": "Dead", "website": "https://dead.com", "status": "Inactive"},
        {"name": "NoSite", "website": None, "status": "Active"},
        "garbage",
    ]))
    got = sources.load_yc(str(p))
    assert [c.name for c in got] == ["Live", "NoSite"]
    assert got[0].yc_batch == "W24" and got[0].stage == "Early"


def test_load_oss_tables(tmp_path):
    d = tmp_path / "categories"
    d.mkdir()
    (d / "db.md").write_text("""# DB
<table><tbody>
<tr><td><strong><a href="https://cossmology.com/organizations/x">X DB</a></strong></td><td>desc</td>
<td><a href="https://github.com/x/x">x</a></td><td><a href="https://xdb.io/">X DB</a></td><td></td><td></td></tr>
<tr><td>Broken row</td></tr>
</tbody></table>""")
    got = sources.load_oss(str(d))
    assert [(c.name, c.website) for c in got] == [("X DB", "https://xdb.io/")]


def test_load_remoteintech_front_matter_and_broken_yaml(tmp_path):
    d = tmp_path / "companies"
    d.mkdir()
    (d / "a.md").write_text('---\ntitle: "Alpha"\nwebsite: https://alpha.com\ncareers_url: https://alpha.com/jobs\n---\nBody')
    (d / "b.md").write_text("---\ntitle: Beta: The Company\nwebsite: beta.io\n---\n")  # invalid YAML (two colons)
    (d / "c.md").write_text("no front matter here")
    got = {c.name: c for c in sources.load_remoteintech(str(d))}
    assert got["Alpha"].careers_url == "https://alpha.com/jobs"
    assert any(c.website == "beta.io" for c in got.values())
    assert len(got) == 2


def test_load_extra_with_bom_comments_and_case(tmp_path):
    p = tmp_path / "extra.csv"
    p.write_text("﻿Name,Website,Careers_URL\n# comment line\nRANE,ranenetwork.com,https://ranenetwork.com/about/careers\n,,\n",
                 encoding="utf-8")
    got = sources.load_extra(str(p))
    assert len(got) == 1 and got[0].name == "RANE" and got[0].careers_url.endswith("/careers")


def test_merge_dedupes_and_combines_sources():
    lists = [
        [Company("Acme (YC)", "http://www.acme.com/about", sources="yc", yc_batch="S21")],
        [Company("Acme", "https://acme.com", careers_url="https://acme.com/careers", sources="remoteintech")],
        [Company("Acme Mine", "acme.com", sources="extra")],
        [Company("Nobody", "", sources="oss"), Company("Profile", "https://linkedin.com/company/p", sources="oss")],
    ]
    out, dropped = sources.merge(lists)
    assert dropped == 2
    assert len(out) == 1
    c = out[0]
    assert c.sources == "extra|yc|remoteintech"
    assert c.name == "Acme Mine"
    assert c.careers_url == "https://acme.com/careers" and c.yc_batch == "S21"


def test_sources_main_refuses_tiny_list(tmp_path):
    out = tmp_path / "companies.csv"
    out.write_text("keep me")
    assert sources.main(["--extra", str(tmp_path / "missing.csv"), "--out", str(out), "--min-companies", "5"]) == 1
    assert out.read_text() == "keep me"


# ------------------------------------------------------------------ merge

def _row(domain, cat, **kw):
    r = {k: "" for k in FIELDS}
    r.update(domain=domain, name=domain.split(".")[0].title(), category=cat, **kw)
    return r


def test_merge_outputs_and_carry_over(tmp_path):
    root = tmp_path / "careers_scan"
    (root / "data").mkdir(parents=True)
    companies = root / "data" / "companies.csv"
    with open(companies, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "website", "careers_url", "sources", "yc_batch", "stage"])
        for d in ("a.com", "b.com", "c.com", "d.com"):
            w.writerow([d, "https://" + d, "", "yc", "", ""])
    # previous run knew d.com; this run's shard 1 is missing
    with open(root / "data" / "classified.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerow(_row("d.com", "email", apply_emails="jobs@d.com", careers_url="https://d.com/careers"))
        w.writerow(_row("gone.com", "email"))
    shards = tmp_path / "out"
    (shards / "shard-0").mkdir(parents=True)
    with open(shards / "shard-0" / "shard-0.jsonl", "w") as fh:
        for r in (_row("a.com", "email", apply_emails="careers@a.com", careers_url="https://a.com/careers",
                       mentions_engineering="yes"),
                  _row("b.com", "ats", ats="lever"), _row("c.com", "careers_page_unclear", careers_url="https://c.com/jobs")):
            fh.write(json.dumps(r) + "\n")
        fh.write("not json\n")
    assert merge_mod.main(["--companies", str(companies), "--shards-dir", str(shards), "--shards", "2",
                           "--root", str(root)]) == 0
    rows = list(csv.DictReader(open(root / "data" / "classified.csv")))
    assert [r["domain"] for r in rows] == ["a.com", "d.com", "b.com", "c.com"]  # email first, gone.com dropped
    nb = list(csv.DictReader(open(root / "data" / "no_job_board.csv")))
    assert {r["domain"] for r in nb} == {"a.com", "d.com"}
    wl = list(csv.DictReader(open(root / "data" / "watchlist.csv")))
    assert {r["domain"] for r in wl} == {"a.com", "d.com", "c.com"}
    text = (root / "SUMMARY.md").read_text()
    assert "Shards 1 did not finish" in text and "careers@a.com" in text and "(eng)" in text


def test_merge_with_no_shards_fails(tmp_path):
    companies = tmp_path / "c.csv"
    companies.write_text("name,website\n")
    assert merge_mod.main(["--companies", str(companies), "--shards-dir", str(tmp_path / "none"),
                           "--shards", "3", "--root", str(tmp_path)]) == 1


# ------------------------------------------------------------------ watch

PAGE_V1 = """<html><title>Careers</title><body><nav><a href="/">Home</a></nav><h1>Careers</h1>
<p>Open roles</p><h3>Account Manager</h3><p>Email careers@acme.com</p><footer>© 2026 Acme</footer></body></html>"""
PAGE_V2 = PAGE_V1.replace("<h3>Account Manager</h3>", "<h3>Account Manager</h3><h3>DevOps Engineer</h3>")


class OnePage:
    def __init__(self, html):
        self.html = html

    def page(self, url, robots=True):
        return Page(url=url, status=200, text=self.html)


def test_page_lines_drop_chrome_and_noise():
    lines = watch.page_lines(PAGE_V1)
    assert "Account Manager" in lines
    assert not any("©" in x or x == "Home" for x in lines)


def test_watch_baseline_then_change(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "ROOT", str(tmp_path))
    (tmp_path / "snapshots").mkdir()
    item = {"domain": "acme.com", "name": "Acme", "url": "https://acme.com/careers"}
    state = {}
    r1 = watch.check_one(OnePage(PAGE_V1), item, state)
    assert r1["status"] == "baseline"
    with open(watch._snap_path("acme.com"), "w") as fh:
        fh.write("\n".join(r1["new_lines"]) + "\n")
    r2 = watch.check_one(OnePage(PAGE_V2), item, state)
    assert r2["status"] == "changed" and r2["added"] == ["DevOps Engineer"]
    assert watch.is_interesting(r2["added"])
    r3 = watch.check_one(OnePage("<html><div id=root></div></html>"), item, state)
    assert r3["status"].startswith("no readable") and state["acme.com"]["fails"] == 1


def test_watch_main_without_watchlist(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "ROOT", str(tmp_path))
    assert watch.main([]) == 0


def test_watch_main_writes_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "ROOT", str(tmp_path))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "watchlist.csv").write_text("domain,name,url,category\nacme.com,Acme,https://acme.com/careers,email\n")
    html = {"v": PAGE_V1}
    monkeypatch.setattr(watch, "Fetcher", lambda: OnePage(html["v"]))
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert watch.main([]) == 0
    assert (tmp_path / "snapshots" / "acme.com.txt").exists()
    html["v"] = PAGE_V2
    assert watch.main([]) == 0
    assert "DevOps Engineer" in (tmp_path / "CHANGES.md").read_text()
    log = list(csv.DictReader(open(tmp_path / "data" / "changes_log.csv")))
    assert log[-1]["looks_like_new_posting"] == "yes"


# ------------------------------------------------------------------ fetch

def test_decode_body():
    assert decode_body("café".encode("utf-8"), "text/html; charset=utf-8") == "café"
    assert decode_body("café".encode("cp1252"), "text/html; charset=ISO-8859-1") == "café"
    assert decode_body(b'<meta charset="windows-1252"><p>caf\xe9</p>', "text/html") .endswith("café</p>")
    assert decode_body("café".encode("utf-8"), "text/html") == "café"
    assert decode_body(b"caf\xe9", "text/html") == "café"            # not UTF-8: cp1252 fallback
    assert decode_body(b"x", "text/html; charset=bogus-charset") == "x"


def test_error_kinds():
    assert _error_kind(requests.exceptions.SSLError("x")) == "ssl"
    assert _error_kind(requests.exceptions.ConnectTimeout("x")) == "timeout"
    assert _error_kind(requests.exceptions.TooManyRedirects("x")) == "redirects"
    assert _error_kind(requests.exceptions.ConnectionError("Failed to resolve 'nope.invalid'")) == "dns"
    assert _error_kind(requests.exceptions.ConnectionError("Connection refused")) == "connect"
    assert _error_kind(requests.exceptions.InvalidURL("x")) == "invalid_url"


@pytest.mark.parametrize("robots_page, path, expected", [
    (Page(url="", status=200, text="User-agent: *\nDisallow: /private\n"), "/careers", "allow"),
    (Page(url="", status=200, text="User-agent: *\nDisallow: /\n"), "/careers", "deny"),
    (Page(url="", status=200, text="User-agent: job-radar\nDisallow: /\n\nUser-agent: *\nAllow: /\n"), "/x", "deny"),
    (Page(url="", status=404, text=""), "/careers", "allow"),
    (Page(url="", status=503, text=""), "/careers", "deny"),
    (Page(url="", error="dns"), "/careers", "dns"),
    (Page(url="", status=200, text="<html><body>home page served for robots.txt</body></html>"), "/careers", "allow"),
])
def test_robots_states(monkeypatch, robots_page, path, expected):
    f = Fetcher(host_delay=0)
    monkeypatch.setattr(f, "_raw_get", lambda url, accept: robots_page)
    assert f.robots_state("https://acme.com" + path) == expected


def test_page_checks_robots_of_redirect_target(monkeypatch):
    f = Fetcher(host_delay=0)
    calls = []

    def raw(url, accept):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return Page(url=url, status=200, text="User-agent: *\nDisallow: /\n" if "careers." in url else "")
        return Page(url="https://careers.acme.com/", status=200, text="<p>jobs</p>")
    monkeypatch.setattr(f, "_raw_get", raw)
    pg = f.page("https://acme.com/careers")
    assert pg.error == "robots"


def test_pacing_is_per_host():
    f = Fetcher(host_delay=0.2)
    import time
    t = time.monotonic()
    f._pace("a.com")
    f._pace("b.com")
    assert time.monotonic() - t < 0.15
    f._pace("a.com")
    assert time.monotonic() - t >= 0.18


def test_fetch_module_has_no_network_at_import():
    assert fetch_mod.USER_AGENT.startswith("Mozilla/5.0 (compatible; job-radar")


def test_merge_moves_job_board_websites_to_careers_url():
    out, dropped = sources.merge([[Company("Swif.ai", "https://www.ycombinator.com/companies/swif-ai/jobs", sources="remoteintech"),
                                   Company("Other", "https://www.ycombinator.com/companies/other/jobs", sources="remoteintech")]])
    assert dropped == 0 and len(out) == 2
    assert all(c.website == "" and c.careers_url for c in out)


def test_merge_folds_link_only_entry_into_named_company():
    out, _ = sources.merge([[Company("Swif.ai", "https://www.swif.ai", sources="yc")],
                            [Company("Swif.ai", "https://www.ycombinator.com/companies/swif-ai/jobs", sources="remoteintech")]])
    assert len(out) == 1
    assert out[0].website == "https://www.swif.ai/" and out[0].careers_url.endswith("/swif-ai/jobs")
    assert out[0].sources == "yc|remoteintech"
