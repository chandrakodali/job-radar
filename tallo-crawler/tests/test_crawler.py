"""Offline integration coverage for crawl planning, persistence, and probe behavior."""

import asyncio
import csv
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import tallo_crawler
from tallo.browser import Card, CityLink, CityPage, JobPage, NavigationFailure, StopRun, TreePage
from tallo.crawler import BROWSE, LAYOUT_MESSAGE, SOFTWARE, Crawl, Settings, Stats, probe
from tallo.parsing import DEFAULT_KEYWORDS
from tallo.storage import Storage, read_json, write_json

CATEGORY = "computer-systems-engineer-or-architect"
CATEGORY_URL = f"{BROWSE}/{CATEGORY}"
AUSTIN = f"{CATEGORY_URL}/tx/austin"


class OfflineBrowser:
    """Return observed page data without starting Chromium or touching the network."""

    def __init__(self, tree=None, cities=None, jobs=None, *, max_pages=600):
        self.tree = tree or {}
        self.cities = cities or {}
        self.jobs = jobs or {}
        self.max_pages = max_pages
        self.pages_loaded = 0
        self.calls = []
        self.tree_options = []
        self.city_options = []
        self.job_options = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    def _visit(self, kind, url):
        if self.pages_loaded >= self.max_pages:
            raise StopRun("budget", "Offline page cap reached")
        self.pages_loaded += 1
        self.calls.append((kind, url))

    async def read_tree_links(self, url, **kwargs):
        self._visit("tree", url)
        self.tree_options.append(kwargs)
        links = self.tree[url]
        if isinstance(links, NavigationFailure):
            raise links
        return TreePage(links, "<main>Browse links</main>")

    async def read_cards(self, url, **kwargs):
        self._visit("city", url)
        self.city_options.append(kwargs)
        cards = self.cities[url]
        return CityPage(cards, "button with text View More", "<main>City listing</main>", False, False)

    async def read_job(self, url, **kwargs):
        self._visit("job", url)
        self.job_options.append(kwargs)
        return self.jobs[url]


def settings(directory: Path, **changes) -> Settings:
    initial = Settings(
        data_dir=directory,
        browser_config={
            "contact_email": "crawler@example.com",
            "keywords": DEFAULT_KEYWORDS,
            "probe": {"category": CATEGORY, "state": "tx", "city": "austin"},
        },
        categories=(CATEGORY,),
        states=(),
        core_cities=120,
        rotation=6,
        max_pages=600,
        since_hours=48,
        run_minutes=55,
        alert_threshold=45,
        dry_run=False,
        probe=False,
    )
    return replace(initial, **changes)


def tree_for(category, links):
    category_url = f"{BROWSE}/{category}"
    state_url = f"{category_url}/tx"
    return {
        category_url: [CityLink(state_url, "Texas", sum(link.count for link in links))],
        state_url: links,
    }


def card(job_id, *, title="DevOps Engineer I", date="2 hours ago"):
    return Card(f"https://tallo.com/talent/job/devops-engineer-{job_id}", title, date, f"{title} {date}")


def details(card, **changes):
    job = JobPage(
        title=card.title,
        company="Acme",
        location="Austin, TX (Hybrid)",
        posted_text=card.posted_text,
        employment_type="Full-time",
        pay="Not specified",
        description="Required qualifications: 2 years of experience with Linux.",
        apply_url="https://dejobs.org/listing",
        html="<main>Job description</main>",
    )
    return replace(job, **changes)


def csv_jobs(directory):
    with (directory / "tallo_matches.csv").open(encoding="utf-8-sig", newline="") as stream:
        return {row["job_id"]: row for row in csv.DictReader(stream)}


def test_budget_mid_city_resumes_pending_job_without_reopening_city(tmp_path):
    first = card("12345")
    second = card("12346", title="Cloud Engineer")
    city = CityLink(AUSTIN, "Austin", 2)
    start = OfflineBrowser(
        tree_for(CATEGORY, [city]),
        {AUSTIN: [first, second]},
        {first.url: details(first)},
        max_pages=4,
    )
    cfg = settings(tmp_path)
    with Storage(tmp_path) as store:
        stats = Stats()
        with pytest.raises(StopRun) as stop:
            asyncio.run(Crawl(cfg, start, store, stats).run())
        assert stop.value.reason == "budget"
        assert store.has_seen("12345")
        assert not store.has_seen("12346")
        store.flush_csv()
    assert [kind for kind, _ in start.calls] == ["tree", "tree", "city", "job"]
    checkpoint = read_json(tmp_path / "checkpoint.json")
    assert checkpoint["position"] == 0
    pending_city, = checkpoint["pending"]
    assert pending_city["url"] == AUSTIN
    assert [candidate["url"] for candidate in pending_city["candidates"]] == [second.url]
    assert set(csv_jobs(tmp_path)) == {"12345"}

    end = OfflineBrowser(jobs={second.url: details(second, description="U.S. citizenship required.")})
    with Storage(tmp_path) as store:
        stats = Stats()
        asyncio.run(Crawl(cfg, end, store, stats).run())
        store.flush_csv()
    assert end.calls == [("job", second.url)]
    assert stats.cities == 1
    assert stats.jobs == {"blocked": 1}
    assert stats.alerts == []
    assert read_json(tmp_path / "checkpoint.json")["position"] == 1
    assert {job_id: row["status"] for job_id, row in csv_jobs(tmp_path).items()} == {
        "12345": "alert",
        "12346": "blocked",
    }


def test_budget_during_discovery_resumes_tree_after_day_rollover(tmp_path):
    texas = f"{CATEGORY_URL}/tx"
    california = f"{CATEGORY_URL}/ca"
    san_jose = f"{california}/san-jose"
    trees = {
        CATEGORY_URL: [CityLink(texas, "Texas", 10), CityLink(california, "California", 9)],
        california: [CityLink(san_jose, "San Jose", 9)],
        texas: [CityLink(AUSTIN, "Austin", 10)],
    }
    start = OfflineBrowser(trees, max_pages=2)
    cfg = settings(tmp_path)
    with Storage(tmp_path) as store:
        crawl = Crawl(cfg, start, store, Stats())
        crawl.day = "2026-11-07"
        with pytest.raises(StopRun) as stop:
            asyncio.run(crawl.run())
        assert stop.value.reason == "budget"
    assert start.calls == [("tree", CATEGORY_URL), ("tree", california)]

    ignored = card("12345", title="Marketing Manager")
    resumed = OfflineBrowser({texas: trees[texas]}, {san_jose: [ignored], AUSTIN: [ignored]})
    with Storage(tmp_path) as store:
        crawl = Crawl(cfg, resumed, store, Stats())
        crawl.day = "2026-11-08"
        asyncio.run(crawl.run())
    assert resumed.calls == [("tree", texas), ("city", AUSTIN), ("city", san_jose)]


def test_resumed_job_uses_original_observation_for_freshness(tmp_path):
    undated = card("12345", date="")
    dated = card("12346")
    start = OfflineBrowser(
        tree_for(CATEGORY, [CityLink(AUSTIN, "Austin", 2)]),
        {AUSTIN: [undated, dated]},
        max_pages=3,
    )
    cfg = settings(tmp_path)
    with Storage(tmp_path) as store:
        with pytest.raises(StopRun) as stop:
            asyncio.run(Crawl(cfg, start, store, Stats()).run())
        assert stop.value.reason == "budget"

    path = tmp_path / "checkpoint.json"
    checkpoint = read_json(path)
    unknown_candidate, dated_candidate = checkpoint["pending"][0]["candidates"]
    old = datetime.fromisoformat(unknown_candidate["first_seen"]) - timedelta(days=3)
    unknown_candidate["first_seen"] = old.isoformat()
    write_json(path, checkpoint)
    resumed = OfflineBrowser(
        jobs={undated.url: details(undated), dated.url: details(dated, posted_text="Not specified")},
    )
    with Storage(tmp_path) as store:
        asyncio.run(Crawl(cfg, resumed, store, Stats()).run())
        assert store.first_seen("12345") == old
        store.flush_csv()
    rows = csv_jobs(tmp_path)
    assert rows.keys() == {"12346"}
    assert rows["12346"]["posted_text"] == dated.posted_text
    assert datetime.fromisoformat(rows["12346"]["posted_utc"]) == datetime.fromisoformat(dated_candidate["posted"])


def test_single_deferred_state_does_not_prevent_other_city_crawls(tmp_path):
    texas = f"{CATEGORY_URL}/tx"
    california = f"{CATEGORY_URL}/ca"
    trees = {
        CATEGORY_URL: [CityLink(texas, "Texas", 10), CityLink(california, "California", 9)],
        california: NavigationFailure("simulated failed state navigation"),
        texas: [CityLink(AUSTIN, "Austin", 10)],
    }
    posting = card("12345")
    browser = OfflineBrowser(trees, {AUSTIN: [posting]}, {posting.url: details(posting)})
    with Storage(tmp_path) as store:
        stats = Stats()
        with pytest.raises(StopRun) as stop:
            asyncio.run(Crawl(settings(tmp_path), browser, store, stats).run())
        assert stop.value.reason == "error"
        assert store.has_seen("12345")
    assert browser.calls == [
        ("tree", CATEGORY_URL), ("tree", california), ("tree", texas), ("city", AUSTIN), ("job", posting.url),
    ]
    assert stats.cities == 1


def test_core_and_rotating_cities_cover_every_slice_software_last(tmp_path):
    primary = [CityLink(f"{CATEGORY_URL}/tx/city-{n}", f"City {n}", 30 - n) for n in range(12)]
    software_url = f"{BROWSE}/{SOFTWARE}"
    software = [CityLink(f"{software_url}/tx/city-{n}", f"City {n}", 8 - n) for n in range(8)]
    all_links = primary + software
    core = primary[0].url
    cfg = settings(tmp_path, categories=(SOFTWARE, CATEGORY), core_cities=1, rotation=3)
    tree = {**tree_for(CATEGORY, primary), **tree_for(SOFTWARE, software)}
    ignored = card("12345", title="Marketing Manager")
    rotating = []
    for position in range(cfg.rotation):
        browser = OfflineBrowser(tree, {link.url: [ignored] for link in all_links})
        with Storage(tmp_path) as store:
            asyncio.run(Crawl(cfg, browser, store, Stats()).run())
        city_visits = [url for kind, url in browser.calls if kind == "city"]
        assert city_visits[0] == core
        assert city_visits == sorted(city_visits, key=lambda url: url.startswith(software_url))
        if position == 0:
            tree_visits = [url for kind, url in browser.calls if kind == "tree"]
            assert tree_visits == [CATEGORY_URL, f"{CATEGORY_URL}/tx", software_url, f"{software_url}/tx"]
        else:
            assert all(kind == "city" for kind, _ in browser.calls)
        rotating.extend(city_visits[1:])
        assert read_json(tmp_path / "checkpoint.json")["position"] == (position + 1) % cfg.rotation
    assert sorted(rotating) == sorted(link.url for link in all_links if link.url != core)


def test_three_layout_misses_save_html_and_stop(tmp_path):
    links = [CityLink(f"{CATEGORY_URL}/tx/city-{n}", f"City {n}", 10) for n in range(4)]
    browser = OfflineBrowser(tree_for(CATEGORY, links), {link.url: [] for link in links})
    with Storage(tmp_path) as store:
        with pytest.raises(StopRun) as stop:
            asyncio.run(Crawl(settings(tmp_path), browser, store, Stats()).run())
    assert stop.value.reason == "error"
    assert str(stop.value) == LAYOUT_MESSAGE
    assert [url for kind, url in browser.calls if kind == "city"] == [link.url for link in links[:3]]
    snapshots = sorted((tmp_path / "probe" / "layout").glob("*.html"))
    assert len(snapshots) == 3
    assert all(path.read_text(encoding="utf-8") == "<main>City listing</main>" for path in snapshots)


def test_card_and_detail_filters_save_reposts_blocked_and_no_sponsorship(tmp_path):
    sponsored = card("12345", date="")
    repost = card("12346")
    too_senior = card("12347")
    blocked = card("12348", title="Cloud Engineer")
    old_card = card("12349", date="3 days ago")
    old_post = card("12350")
    excluded_title = card("12351", title="Senior DevOps Engineer")
    foreign = card("12352")
    unknown = card("12353", title="Linux Systems Administrator", date="")
    cards = [sponsored, repost, too_senior, blocked, old_card, old_post, excluded_title, foreign, unknown]
    jobs = {
        sponsored.url: details(sponsored, posted_text="just now", description="4 to 8 years of experience. No OPT/CPT."),
        repost.url: details(repost),
        too_senior.url: details(too_senior, description="5 years of experience required."),
        blocked.url: details(blocked, description="Ability to obtain a security clearance."),
        old_post.url: details(old_post, posted_text="3 days ago", last_updated_text="just now"),
        foreign.url: details(foreign, location="Toronto, Canada"),
        unknown.url: details(unknown, description="Help maintain Linux services."),
    }
    browser = OfflineBrowser(
        tree_for(CATEGORY, [CityLink(AUSTIN, "Austin", len(cards))]),
        {AUSTIN: cards},
        jobs,
    )
    with Storage(tmp_path) as store:
        stats = Stats()
        asyncio.run(Crawl(settings(tmp_path), browser, store, stats).run())
        store.flush_csv()
    assert {url for kind, url in browser.calls if kind == "job"} == jobs.keys()
    rows = csv_jobs(tmp_path)
    assert rows.keys() == {"12345", "12346", "12348", "12353"}
    assert rows["12345"]["years_required"] == "4"
    assert "no sponsorship" in rows["12345"]["flags"]
    assert "no sponsorship -15" in rows["12345"]["score_reasons"]
    assert rows["12346"]["status"] == "repost"
    assert rows["12348"]["status"] == "blocked"
    assert rows["12353"]["posted_utc"] == ""
    assert "date unknown" in rows["12353"]["flags"]
    assert rows["12353"]["apply_url"] == "https://dejobs.org/listing"
    assert rows["12353"]["apply_domain"] == "dejobs.org"
    assert stats.jobs["too_senior"] == 1
    assert all(row["job_id"] not in {"12346", "12348"} for row in stats.alerts)


def test_cli_dry_run_writes_nothing_and_sends_nothing(tmp_path, monkeypatch):
    cfg = settings(tmp_path / "data", dry_run=True)
    posting = card("12345")
    browser = OfflineBrowser(
        tree_for(CATEGORY, [CityLink(AUSTIN, "Austin", 1)]),
        {AUSTIN: [posting]},
        {posting.url: details(posting)},
    )
    monkeypatch.setattr(tallo_crawler, "Browser", lambda *args, **kwargs: browser)
    monkeypatch.setenv("NTFY_TOPIC", "offline-test")
    monkeypatch.setattr(tallo_crawler, "send_alerts", lambda *args: pytest.fail("Dry run attempted a phone alert"))
    assert asyncio.run(tallo_crawler.execute(cfg)) == 0
    assert browser.pages_loaded == 4
    assert not cfg.data_dir.exists()


def test_probe_uses_four_documents_and_saves_snapshots(tmp_path, caplog):
    posting = card("12345")
    browser = OfflineBrowser(
        tree_for(CATEGORY, [CityLink(AUSTIN, "Austin", 1)]),
        {AUSTIN: [posting]},
        {posting.url: details(posting)},
        max_pages=4,
    )
    caplog.set_level("INFO")
    stats = Stats()
    asyncio.run(probe(settings(tmp_path, probe=True), browser, stats))
    assert browser.calls == [
        ("tree", CATEGORY_URL), ("tree", f"{CATEGORY_URL}/tx"), ("city", AUSTIN), ("job", posting.url),
    ]
    assert browser.tree_options == [{"strict": False}, {"strict": False}]
    assert browser.city_options[0]["max_clicks"] == 0
    assert browser.job_options == [{"expand": False}]
    assert {path.name for path in (tmp_path / "probe").glob("*.html")} == {
        "category.html", "state.html", "city.html", "job.html",
    }
    assert posting.url in caplog.text
    assert "View More" in caplog.text
    assert "parsed=" in caplog.text
