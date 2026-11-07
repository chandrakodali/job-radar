"""Policy and offline rendering tests. All documents are synthetic."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
from types import SimpleNamespace
from urllib.robotparser import RobotFileParser

import pytest
import yaml

from tallo.browser import (
    ORIGIN, TECHNOLOGY_URL, Browser, NavigationFailure, RobotsPolicy, StopRun, URLDenied,
    browse_parts, card_date, job_id, newest_first, normalize_url, url_allowed,
)
from tallo import browser as browser_module

FIXTURES = Path(__file__).parent / "fixtures"
CATEGORY = f"{TECHNOLOGY_URL}/computer-systems-engineer-or-architect"
STATE = f"{CATEGORY}/tx"
CITY = f"{STATE}/austin"
JOB = f"{ORIGIN}/talent/job/devops-engineer-32184730"
NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def robots(text="User-agent: *\nAllow: /"):
    parser = RobotFileParser(f"{ORIGIN}/robots.txt")
    parser.parse(text.splitlines())
    return parser


def config():
    with (Path(__file__).parent.parent / "config.yaml").open(encoding="utf-8") as handle:
        result = yaml.safe_load(handle)
    result["contact_email"] = "test@example.org"
    return result


def test_normalization_and_job_id():
    assert normalize_url(f"{JOB}/?ref=card#details") == JOB
    assert normalize_url("/talent/job/devops-engineer-32184730") == JOB
    assert normalize_url(JOB.replace("https://", "http://")) == JOB
    assert job_id(JOB) == "32184730"
    assert job_id(f"{ORIGIN}/talent/job/devops-engineer-123") is None
    assert browse_parts(f"{CATEGORY}/dc/washington") == (
        "computer-systems-engineer-or-architect", "dc", "washington"
    )
    assert normalize_url("//tallo.com.evil.example/talent/job/devops-engineer-32184730") is None
    assert normalize_url("https://example.org@tallo.com/talent/job/devops-engineer-32184730") is None


@pytest.mark.parametrize("path", [
    "/talent/job/search", "/talent/job/search?q=devops", "/talent/job/search/nested",
    "/talent/apprenticeship/search", "/talent/job/%73earch", "/robots.txt",
    "/talent/job/browse/technology", "/assets/chunk.js",
])
def test_allowlist_forbids_search_and_other_requests(path):
    assert not url_allowed(f"{ORIGIN}{path}", robots(), "personal-job-search-bot")


def test_robots_disallows_an_otherwise_valid_url():
    parser = robots("User-agent: personal-job-search-bot\nDisallow: /talent/job/browse/technology/\n")
    assert not url_allowed(CITY, parser, "personal-job-search-bot/1.0")
    assert url_allowed(JOB, parser, "personal-job-search-bot/1.0")


def test_robots_longest_match_can_disallow_after_broad_allow():
    parser = RobotsPolicy()
    parser.parse(["User-agent: *", "Allow: /talent/job/", f"Disallow: {CITY.removeprefix(ORIGIN)}"])
    assert not url_allowed(CITY, parser, "personal-job-search-bot/1.0")
    assert url_allowed(JOB, parser, "personal-job-search-bot/1.0")


def test_robots_wildcards_and_end_anchors_use_the_applicable_group():
    parser = RobotsPolicy()
    parser.parse([
        "User-agent: *", "Allow: /",
        "User-agent: personal-job-search-bot",
        "Disallow: /talent/job/*-32184730$",
    ])
    assert not url_allowed(JOB, parser, "personal-job-search-bot/1.0")
    assert url_allowed(f"{JOB}0", parser, "personal-job-search-bot/1.0")
    assert url_allowed(JOB, parser, "otherbot/1.0")


def test_card_date_never_uses_last_updated():
    assert card_date("DevOps Engineer\nLast Updated: just now") == ""
    assert card_date("DevOps Engineer\nLast Updated:\njust now") == ""
    assert card_date("DevOps Engineer\nLast Updated: just now\nPosted:\n3 days ago") == "3 days ago"


def parse_date(value):
    ages = {"2 hours ago": 2, "yesterday": 24, "3 days ago": 72}
    return NOW - timedelta(hours=ages[value]) if value in ages else None


def test_newest_first_requires_evidence():
    sample = [SimpleNamespace(posted_text=value) for value in ("2 hours ago", "yesterday", "3 days ago")]
    assert newest_first(sample, parse_date)
    assert not newest_first(sample[::-1], parse_date)
    assert not newest_first([sample[0], sample[0]], parse_date)
    assert not newest_first([sample[0], SimpleNamespace(posted_text="")], parse_date)


class FakeResponse:
    def __init__(self, status, body=b"<h1>Tallo</h1>"):
        self.status = status
        self.headers = {"content-type": "text/html"}
        self._body = body
        self.disposed = False

    async def body(self):
        return self._body

    async def dispose(self):
        self.disposed = True


class FakeRequest:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def fake_browser(monkeypatch, *responses, max_pages=600):
    instance = Browser(config(), max_pages=max_pages)
    instance.playwright_error = OSError
    instance.robots = robots()
    request = FakeRequest(*responses)
    instance.context = SimpleNamespace(request=request)
    delays = []

    async def sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(browser_module.asyncio, "sleep", sleep)
    return instance, request, delays


def test_request_refuses_search_before_transport(monkeypatch):
    instance, request, _ = fake_browser(monkeypatch)
    with pytest.raises(URLDenied):
        asyncio.run(instance._fetch(f"{ORIGIN}/talent/job/search?q=DevOps"))
    assert not request.calls
    assert not instance.pages_loaded


@pytest.mark.parametrize("status", [403, 429])
def test_http_block_stops_without_retry(monkeypatch, status):
    response = FakeResponse(status)
    instance, request, _ = fake_browser(monkeypatch, response)
    with pytest.raises(StopRun) as raised:
        asyncio.run(instance._fetch(CITY))
    assert raised.value.reason == "blocked"
    assert len(request.calls) == 1
    assert response.disposed


def test_challenge_page_stops_before_5xx_retry(monkeypatch):
    response = FakeResponse(503, b"<title>Tallo</title><h1>Unusual traffic detected</h1>")
    instance, request, _ = fake_browser(monkeypatch, response)
    with pytest.raises(StopRun) as raised:
        asyncio.run(instance._fetch(CITY))
    assert raised.value.reason == "blocked"
    assert len(request.calls) == 1


@pytest.mark.parametrize("status, reason", [(302, "error"), (403, "blocked"), (503, "error")])
def test_robots_bootstrap_fails_closed_without_redirect_or_retry(status, reason):
    response = FakeResponse(status)
    request = FakeRequest(response)
    agent = "personal-job-search-bot/1.0 (+contact: test@example.org)"
    pattern = Browser(config(), max_pages=1).patterns["block_text"]
    with pytest.raises(StopRun) as raised:
        asyncio.run(browser_module.read_robots(request, agent, 45, pattern))
    assert raised.value.reason == reason
    assert len(request.calls) == 1
    url, kwargs = request.calls[0]
    assert url == f"{ORIGIN}/robots.txt"
    assert kwargs["max_redirects"] == kwargs["max_retries"] == 0
    assert kwargs["headers"]["User-Agent"] == agent
    assert response.disposed


def test_retry_and_budget_use_same_checked_transport(monkeypatch):
    instance, request, delays = fake_browser(monkeypatch, FakeResponse(502), FakeResponse(200), max_pages=2)

    async def run():
        assert await instance._fetch(CITY) is None
        assert await instance._fetch(CITY, retry=True)
        with pytest.raises(StopRun) as raised:
            await instance._fetch(JOB)
        assert raised.value.reason == "budget"

    asyncio.run(run())
    assert 3 <= delays[0] <= 6
    assert delays[1] == 30
    assert instance.pages_loaded == 2
    assert [url for url, _ in request.calls] == [CITY, CITY]
    for _, args in request.calls:
        assert args["max_redirects"] == args["max_retries"] == 0
        assert args["headers"]["User-Agent"] == "personal-job-search-bot/1.0 (+contact: test@example.org)"


def test_no_redirect_and_three_failures_stop(monkeypatch):
    redirects = [FakeResponse(302) for _ in range(3)]
    for response in redirects:
        response.headers["location"] = f"{ORIGIN}/talent/job/search"
    instance, request, _ = fake_browser(monkeypatch, *redirects)

    async def run():
        for _ in range(2):
            with pytest.raises(NavigationFailure):
                await instance.read_job(JOB)
        with pytest.raises(StopRun) as raised:
            await instance.read_job(JOB)
        assert raised.value.reason == "error"
        assert "Three consecutive" in str(raised.value)

    asyncio.run(run())
    assert [url for url, _ in request.calls] == [JOB, JOB, JOB]


def test_offline_chromium_renders_and_expands_fixtures(monkeypatch):
    pytest.importorskip("playwright")

    async def run():
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            # Headless shell installs can omit the full Chromium executable.
            executable = Path(playwright.chromium.executable_path)
            shell = next(
                (part.with_name(part.name.replace("chromium-", "chromium_headless_shell-"))
                 for part in executable.parents if part.name.startswith("chromium-")),
                None,
            )
            if not executable.exists() and (shell is None or not shell.exists()):
                pytest.skip("Install Chromium with python -m playwright install chromium for the offline DOM test")

        documents = {
            STATE: (FIXTURES / "state.html").read_bytes(),
            CITY: (FIXTURES / "city.html").read_bytes(),
            JOB: (FIXTURES / "job.html").read_bytes(),
        }
        requests = []
        aborted = []
        abort = Browser._abort

        async def fetch(self, url, *, retry=False):
            requests.append(url)
            return documents[url], "text/html; charset=utf-8", time.monotonic() + 8

        async def record_abort(route):
            aborted.append(route.request.url)
            await abort(route)

        async def bootstrap(*_args):
            return robots()

        monkeypatch.setattr(browser_module, "read_robots", bootstrap)
        monkeypatch.setattr(Browser, "_fetch", fetch)
        monkeypatch.setattr(Browser, "_abort", staticmethod(record_abort))
        async with Browser(config(), max_pages=8) as browser:
            tree = await browser.read_tree_links(STATE)
            assert [(link.url, link.label, link.count) for link in tree.links] == [
                (CITY, "Austin", 1203), (f"{STATE}/fort-cavazos", "Fort Cavazos", 1)
            ]
            probe = await browser.read_cards(CITY, cutoff=NOW - timedelta(hours=48), parse_date=parse_date, max_clicks=0)
            assert [card.title for card in probe.cards] == ["DevOps Engineer I", "Platform Engineer II"]
            assert probe.view_more
            city = await browser.read_cards(CITY, cutoff=NOW - timedelta(hours=48), parse_date=parse_date)
            assert [job_id(card.url) for card in city.cards] == ["32184730", "32184729", "32184728"]
            assert city.newest_first
            assert city.cards[0].posted_text == "2 hours ago"
            job = await browser.read_job(JOB)
            assert (job.company, job.location, job.employment_type) == (
                "Example Systems", "Austin, TX (Hybrid)", "Full-time"
            )
            assert job.posted_text == "2 hours ago"
            assert job.last_updated_text == "just now"
            assert job.pay == "Not specified"
            assert "2 years of Linux experience" in job.description
            assert job.apply_url == "https://example.org/job?source=tallo"
            assert not browser.context.pages
            async with browser._document(CITY) as (page, _, _):
                from playwright.async_api import Error
                with pytest.raises(Error):
                    await page.goto(f"{ORIGIN}/talent/job/search")

        assert requests == [STATE, CITY, CITY, JOB, CITY]
        assert f"{ORIGIN}/talent/job/search" in aborted

    asyncio.run(run())
