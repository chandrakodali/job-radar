"""Strict document fetches and isolated rendering for Tallo browse pages."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from fnmatch import translate as translate_glob
from html import unescape
import logging
import random
import re
import time
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import unquote, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

if TYPE_CHECKING:
    from playwright.async_api import APIRequestContext, Locator, Page, Route

LOG = logging.getLogger(__name__)
ORIGIN = "https://tallo.com"
ROBOTS_URL = f"{ORIGIN}/robots.txt"
TECHNOLOGY_URL = f"{ORIGIN}/talent/job/browse/technology"
SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"
BROWSE = re.compile(rf"{re.escape(TECHNOLOGY_URL)}/({SLUG})(?:/([a-z]{{2}})(?:/({SLUG}))?)?")
JOB = re.compile(rf"{re.escape(ORIGIN)}/talent/job/{SLUG}-(\d{{5,}})")
SEARCH = ("/talent/job/search", "/talent/apprenticeship/search")
SPACES = re.compile(r"\s+")
JOB_COUNT = re.compile(r"(?:^|\s|\()([0-9][0-9,]*)\s+jobs?\s*\)?\s*$", re.I)
DATE_LINE = re.compile(
    r"(?:just now|today|yesterday|\d+\+?\s+(?:minutes?|hours?|days?|weeks?)\s+ago|"
    r"[a-z]{3,9}\.?\s+\d{1,2},\s+\d{4}|\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d\d-\d\d)[.!]?",
    re.I,
)
LOCATION_LINE = re.compile(
    r"^(?:[^\d]{1,65},\s*[A-Z]{2}(?:\s|$)|(?i:remote(?:\s|$)|.+\((?:onsite|hybrid|remote)\)$))"
)
EMPLOYMENT_LINE = re.compile(r"(?:full[ -]time|part[ -]time|contract|temporary|permanent)", re.I)
CSP = (
    "sandbox allow-scripts allow-same-origin; default-src 'none'; "
    "script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
    "form-action 'none'; base-uri 'none'"
)
T = TypeVar("T")


class StopRun(RuntimeError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class URLDenied(RuntimeError):
    """The hard allowlist or robots rules forbid this URL."""


class NavigationFailure(RuntimeError):
    """A single attempted navigation failed."""


@dataclass(frozen=True, slots=True)
class CityLink:
    url: str
    label: str
    count: int | None


@dataclass(frozen=True, slots=True)
class TreePage:
    links: list[CityLink]
    html: str
    empty: bool = False


@dataclass(frozen=True, slots=True)
class Card:
    url: str
    title: str
    posted_text: str
    text: str


@dataclass(frozen=True, slots=True)
class CityPage:
    cards: list[Card]
    view_more: str
    html: str
    empty: bool
    newest_first: bool


@dataclass(frozen=True, slots=True)
class JobPage:
    title: str
    company: str
    location: str
    posted_text: str
    employment_type: str
    pay: str
    description: str
    apply_url: str
    html: str
    last_updated_text: str = ""


def normalize_url(value: str, base: str = ORIGIN) -> str | None:
    """Return a canonical same-origin candidate. The allowlist is separate."""
    value = value.strip()
    if not value or "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    try:
        parts = urlsplit(urljoin(base, value))
    except ValueError:
        return None
    if parts.scheme.lower() not in {"https", "http"} or parts.netloc.lower() != "tallo.com":
        return None
    if "%" in parts.path or not parts.path.startswith("/"):
        return None
    return f"{ORIGIN}{parts.path.rstrip('/')}"


def browse_parts(url: str) -> tuple[str, ...] | None:
    normalized = normalize_url(url)
    match = BROWSE.fullmatch(normalized or "")
    return tuple(part for part in match.groups() if part is not None) if match else None


def job_id(url: str) -> str | None:
    normalized = normalize_url(url)
    match = JOB.fullmatch(normalized or "")
    return match.group(1) if match else None


class RobotsPolicy(RobotFileParser):
    """Also honor longest matching rules and wildcard anchors on older Python."""

    def parse(self, lines: list[str]) -> None:
        super().parse(lines)
        self._policy_groups: list[tuple[list[str], list[tuple[re.Pattern[str], int, bool]]]] = []
        agents: list[str] = []
        rules: list[tuple[re.Pattern[str], int, bool]] = []
        has_rules = False
        for line in lines:
            field, _, value = line.split("#", 1)[0].partition(":")
            field, value = field.strip().casefold(), value.strip()
            if field == "user-agent" and value:
                if has_rules:
                    self._policy_groups.append((agents, rules))
                    agents, rules, has_rules = [], [], False
                agents.append(value.casefold())
            elif field in {"allow", "disallow"} and agents:
                has_rules = True
                if value:
                    parts = [unquote(part) for part in value.removesuffix("$").split("*")]
                    literals = ["".join(f"[{char}]" if char in "[*?" else char for char in part) for part in parts]
                    glob = "*".join(literals)
                    if not value.endswith("$"):
                        glob += "*"
                    size = sum(len(part.encode("utf-8")) for part in parts)
                    rules.append((re.compile(translate_glob(glob)), size, field == "allow"))
        if agents:
            self._policy_groups.append((agents, rules))

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if not super().can_fetch(user_agent, url):
            return False
        product = user_agent.casefold().split("/", 1)[0]
        matches = []
        for agents, rules in self._policy_groups:
            lengths = [0 if agent == "*" else len(agent) for agent in agents if agent == "*" or agent in product]
            if lengths:
                matches.append((max(lengths), rules))
        specificity = max((size for size, _ in matches), default=-1)
        path = urlsplit(url).path
        applicable = [
            (size, allow) for group_size, rules in matches if group_size == specificity
            for pattern, size, allow in rules if pattern.fullmatch(path)
        ]
        return max(applicable, default=(0, True))[1]


def url_allowed(url: str, robots: RobotFileParser, user_agent: str) -> bool:
    if normalize_url(url) != url:
        return False
    path = urlsplit(url).path
    if any(path == prefix or path.startswith(prefix + "/") for prefix in SEARCH):
        return False
    return bool(browse_parts(url) or job_id(url)) and robots.can_fetch(user_agent, url)


def challenge_text(raw: bytes, pattern: re.Pattern[str]) -> bool:
    prefix = raw[:65536].decode("utf-8", errors="replace")
    headings = re.findall(r"<(?:title|h1|h2)\b[^>]*>([^<]*)", prefix, re.I)
    if not headings:
        headings = [re.sub(r"<[^>]+>", " ", prefix)]
    return any(pattern.search(tidy(unescape(heading))) for heading in headings)


async def read_robots(
    request: APIRequestContext, user_agent: str, timeout: float, block_pattern: re.Pattern[str]
) -> RobotFileParser:
    # Bootstrap is the one request exempt from the document allowlist.
    response = await request.get(
        ROBOTS_URL, headers={"User-Agent": user_agent, "Accept": "text/plain"},
        max_redirects=0, max_retries=0, fail_on_status_code=False, timeout=timeout * 1000,
    )
    try:
        if response.status in {403, 429} or response.headers.get("cf-mitigated") == "challenge":
            raise StopRun("blocked", f"robots.txt returned HTTP {response.status} or a challenge")
        content = await response.body()
        if challenge_text(content, block_pattern):
            raise StopRun("blocked", "robots.txt returned an access challenge")
        if response.status != 200:
            raise StopRun("error", f"robots.txt returned HTTP {response.status}; no documents requested")
        if len(content) > 512 * 1024:
            raise StopRun("error", "robots.txt exceeded the size limit")
        try:
            rules = content.decode("utf-8-sig")
        except UnicodeError as exc:
            raise StopRun("error", "robots.txt is not valid UTF-8") from exc
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if rules.lstrip().startswith("<") or content_type == "text/html":
            raise StopRun("error", "robots.txt was served as markup")
    finally:
        await response.dispose()
    parser = RobotsPolicy(ROBOTS_URL)
    parser.parse(rules.splitlines())
    return parser


def tidy(text: str) -> str:
    return SPACES.sub(" ", text).strip()


def labeled_text(text: str, label: str) -> str:
    """Use only the specified label. In particular, Posted excludes Last Updated."""
    lines = [tidy(line) for line in text.splitlines() if line.strip()]
    prefix = re.compile(rf"^{re.escape(label)}(?:\s*:\s*|\s+)(.+)$", re.I)
    for index, line in enumerate(lines):
        match = prefix.match(line)
        if match:
            return match.group(1).strip()
        if line.rstrip(":").strip().casefold() == label.casefold() and index + 1 < len(lines):
            return lines[index + 1]
    return ""


def card_date(text: str) -> str:
    posted = labeled_text(text, "Posted")
    if posted:
        return posted
    skip_next = False
    for raw in text.splitlines():
        line = tidy(raw)
        if not line:
            continue
        if skip_next:
            skip_next = False
        elif line.rstrip(":").strip().casefold() == "last updated":
            skip_next = True
        elif DATE_LINE.fullmatch(line):
            return line
    return ""


def newest_first(cards: list[Card], parse_date: Callable[[str], datetime | None]) -> bool:
    dates = [parse_date(card.posted_text) for card in cards]
    if any(date is None for date in dates):
        return False
    known = [date for date in dates if date is not None]
    return len(set(known)) >= 2 and all(a >= b for a, b in zip(known, known[1:]))


# These scripts inspect rendered DOM only. All selectors used for site structure
# come from config.yaml. Relative hrefs are validated by Python before any use.
SNAPSHOT = """(selectors) => {
    const visible = (el) => el && el.getBoundingClientRect().width && el.getBoundingClientRect().height &&
        !['hidden', 'collapse'].includes(getComputedStyle(el).visibility);
    const nodes = (selector, root = document) =>
        Array.from(root.querySelectorAll(selector)).filter(visible);
    const value = (el) => el?.innerText?.trim() || '';
    const first = (selector, root) => value(nodes(selector, root)[0]);
    const links = (selector) => nodes(selector).map((el) =>
        ({href: el.getAttribute('href'), text: value(el)}));
    const treeLinks = nodes(selectors.tree_links).map((link) => {
        const parent = link.parentElement;
        let text = value(link);
        if (!/\\d\\s+jobs?\\s*\\)?$/i.test(text) && parent &&
                nodes(selectors.tree_links, parent).length === 1) {
            const group = value(parent);
            if (/\\d\\s+jobs?\\s*\\)?$/i.test(group)) text = group;
        }
        return {href: link.getAttribute('href'), text};
    });
    const isJob = (el) => {
        const href = el.getAttribute('href') || '';
        return /\\/talent\\/job\\/[^/?#]+-\\d{5,}\\/?(?:[?#]|$)/i.test(href)
            && !href.includes('/browse/');
    };
    const card = (link) => {
        const explicit = link.closest(selectors.card_container);
        const jobLinks = (el) => nodes(selectors.job_links, el).filter(isJob);
        const href = (el) => el.getAttribute('href').split(/[?#]/)[0].replace(/\\/$/, '');
        const ownLink = (el) => jobLinks(el).every((candidate) => href(candidate) === href(link));
        let root = explicit && ownLink(explicit) ? explicit : link;
        if (root === link) {
            for (let el = link.parentElement, depth = 0; el && depth < 5;
                 el = el.parentElement, depth++) {
                if (['BODY', 'MAIN', 'UL', 'OL', 'SECTION'].includes(el.tagName) || !ownLink(el)) {
                    break;
                }
                root = el;
            }
        }
        return {href: link.getAttribute('href'), title: first(selectors.card_title, root) ||
            value(link) || link.getAttribute('aria-label') || '', text: value(root)};
    };
    const jobBody = nodes(selectors.job_body)[0] || document.body;
    const block = nodes(selectors.block_nodes).map((el) => {
        const label = el.getAttribute('title') || '';
        return el.tagName === 'IFRAME' && /captcha/i.test(label) ? 'CAPTCHA challenge' : value(el) || label;
    });
    return {
        title: document.title,
        body: document.body?.innerText || '',
        block,
        links: treeLinks,
        cards: nodes(selectors.job_links).filter(isJob).map(card),
        job: {
            title: first(selectors.job_title),
            company: first(selectors.company),
            location: first(selectors.location),
            employment_type: first(selectors.employment_type),
            pay: first(selectors.pay),
            description: first(selectors.description),
            body: value(jobBody),
            apply: links(selectors.apply_link),
        },
    };
}"""


def extract_tree(url: str, rows: list[dict[str, str]]) -> list[CityLink]:
    parent = browse_parts(url)
    if parent is None:
        return []
    links: dict[str, CityLink] = {}
    for row in rows:
        target = normalize_url(row["href"], url)
        child = browse_parts(target) if target else None
        if target is None or child is None or child[:-1] != parent:
            continue
        label = tidy(row["text"])
        match = JOB_COUNT.search(label)
        count = int(match.group(1).replace(",", "")) if match else None
        if match:
            label = label[:match.start()].rstrip(" (")
        if target not in links or (links[target].count is None and count is not None):
            links[target] = CityLink(target, label, count)
    return list(links.values())


def extract_cards(url: str, rows: list[dict[str, str]]) -> list[Card]:
    cards: dict[str, Card] = {}
    for row in rows:
        target = normalize_url(row["href"], url)
        identifier = job_id(target) if target else None
        if target is None or identifier is None:
            continue
        card = Card(target, tidy(row["title"]), card_date(row["text"]), row["text"])
        if identifier not in cards or (not cards[identifier].title and card.title):
            cards[identifier] = card
    return list(cards.values())


def extract_job(
    url: str, fields: dict[str, Any], apply_pattern: re.Pattern[str], html: str = ""
) -> JobPage:
    body = fields["body"]
    lines = [tidy(line) for line in body.splitlines() if line.strip()]
    title = tidy(fields["title"])
    try:
        first_header = lines.index(title) + 1
    except ValueError:
        first_header = 0
    header_lines = lines[first_header:first_header + 6]
    location = tidy(fields["location"]) or labeled_text(body, "Location") or next(
        (line for line in header_lines if LOCATION_LINE.match(line)), ""
    )
    employment = tidy(fields["employment_type"]) or labeled_text(body, "Employment Type") or labeled_text(body, "Job Type")
    if not employment:
        employment = next((line for line in header_lines if EMPLOYMENT_LINE.fullmatch(line)), "")
    company = tidy(fields["company"]) or labeled_text(body, "Company")
    if not company and title and header_lines and location in header_lines[1:3]:
        # Typical unlabelled header: title, company, location.
        candidate = header_lines[0]
        if not re.match(r"^(?:Posted\b|Last Updated\b|Pay\b|Apply\b)", candidate, re.I):
            company = candidate
    apply_url = ""
    for link in fields["apply"]:
        if not apply_pattern.fullmatch(tidy(link["text"])):
            continue
        try:
            candidate = urljoin(url, link["href"])
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username:
            apply_url = candidate
            break
    return JobPage(
        title=title,
        company=company,
        location=location,
        posted_text=labeled_text(body, "Posted"),
        employment_type=employment,
        pay=tidy(fields["pay"]) or labeled_text(body, "Pay"),
        description=fields["description"] or body,
        apply_url=apply_url,
        html=html,
        last_updated_text=labeled_text(body, "Last Updated"),
    )


class Browser:
    """One worker and one offline Chromium page per approved HTTP document."""

    def __init__(self, config: Mapping[str, Any], *, max_pages: int):
        email = str(config.get("contact_email") or "").strip()
        if not email or not re.fullmatch(r"\S+", email):
            raise ValueError("A contact URL is required for the bot's User-Agent")
        self.user_agent = f"personal-job-search-bot/1.0 (+contact: {email})"
        self.selectors = dict(config["selectors"])
        self.patterns = {
            key: re.compile(self.selectors[key], re.I)
            for key in ("block_text", "empty_text", "view_more_text", "read_more_text", "apply_text")
        }
        budgets = config["budgets"]
        self.max_pages = max_pages
        self.delay_min = min(6.0, max(3.0, float(budgets.get("delay_min_seconds", 3))))
        self.delay_max = min(6.0, max(self.delay_min, float(budgets.get("delay_max_seconds", 6))))
        self.retry_delay = max(30.0, float(budgets.get("retry_5xx_seconds", 30)))
        self.navigation_timeout = min(45.0, float(budgets.get("navigation_timeout_seconds", 45)))
        self.interaction_timeout = float(budgets.get("interaction_timeout_seconds", 5))
        self.max_clicks = min(15, int(budgets.get("max_view_more_clicks", 15)))
        if self.navigation_timeout <= 0 or self.interaction_timeout <= 0 or self.max_clicks < 0:
            raise ValueError("Navigation and interaction timeouts must be positive and click cap nonnegative")
        self.pages_loaded = 0
        self.consecutive_failures = 0
        self.last_request_at: float | None = None
        self.stack = AsyncExitStack()
        self.page_lock = asyncio.Lock()

    async def __aenter__(self) -> Browser:
        from playwright.async_api import Error, TimeoutError as PlaywrightTimeout, async_playwright

        self.playwright_error = Error
        self.playwright_timeout = PlaywrightTimeout
        try:
            driver = await self.stack.enter_async_context(async_playwright())
            robots_client = await driver.request.new_context(user_agent=self.user_agent)
            try:
                self.robots = await read_robots(
                    robots_client, self.user_agent, self.navigation_timeout, self.patterns["block_text"]
                )
                self.last_request_at = time.monotonic()
            except Error as exc:
                raise StopRun("error", f"Could not read robots.txt: {exc}") from exc
            finally:
                await robots_client.dispose()
            browser = await driver.chromium.launch(
                headless=True,
                ignore_default_args=["--disable-popup-blocking"],
                args=[
                    "--disable-background-networking",
                    "--disable-dns-prefetch",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                ],
            )
            self.stack.push_async_callback(browser.close)
            self.context = await browser.new_context(
                user_agent=self.user_agent, offline=True, service_workers="block",
                accept_downloads=False,
            )
            self.stack.push_async_callback(self.context.close)
            await self.context.route("**/*", self._abort)
            await self.context.route_web_socket("**/*", self._close_socket)
        except BaseException:
            await self.stack.aclose()
            raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stack.aclose()

    @staticmethod
    async def _abort(route: Route) -> None:
        LOG.debug("Aborted browser-initiated request to %s", route.request.url)
        await route.abort()

    @staticmethod
    async def _close_socket(socket: Any) -> None:
        await socket.close()

    def _authorize(self, value: str) -> str:
        url = normalize_url(value)
        if url is None or not url_allowed(url, self.robots, self.user_agent):
            LOG.warning("Skipped URL forbidden by the allowlist or robots.txt: %s", value)
            raise URLDenied(value)
        if self.pages_loaded >= self.max_pages:
            raise StopRun("budget", f"Page-load cap reached ({self.max_pages})")
        return url

    def _pause_seconds(self, retry: bool) -> float:
        delay = max(random.uniform(self.delay_min, self.delay_max), self.retry_delay if retry else 0)
        rate = self.robots.request_rate(self.user_agent)
        robots_delay = float(self.robots.crawl_delay(self.user_agent) or 0)
        if rate and rate.requests > 0:
            robots_delay = max(robots_delay, rate.seconds / rate.requests)
        if self.last_request_at is not None:
            robots_delay -= time.monotonic() - self.last_request_at
        return max(delay, robots_delay)

    async def _fetch(self, value: str, *, retry: bool = False) -> tuple[bytes, str, float] | None:
        url = self._authorize(value)
        await asyncio.sleep(self._pause_seconds(retry))
        self.last_request_at = time.monotonic()
        self.pages_loaded += 1
        response = await self.context.request.get(
            url, headers={"User-Agent": self.user_agent, "Accept": "text/html"},
            max_redirects=0, max_retries=0, fail_on_status_code=False,
            timeout=self.navigation_timeout * 1000,
        )
        try:
            status = response.status
            if status in {403, 429} or response.headers.get("cf-mitigated") == "challenge":
                raise StopRun("blocked", f"HTTP {status} or challenge at {url}")
            raw = await response.body()
            # Catch common challenge responses before a 5xx retry or redirect error.
            if challenge_text(raw, self.patterns["block_text"]):
                raise StopRun("blocked", f"Access challenge at {url}")
            if 500 <= status < 600 and not retry:
                LOG.warning("HTTP %s at %s. Retrying once after %g seconds", status, url, self.retry_delay)
                return None
            if not 200 <= status < 300:
                raise NavigationFailure(f"HTTP {status} at {url}")
            content_type = response.headers.get("content-type", "text/html")
            if content_type.split(";", 1)[0].strip().lower() not in {"text/html", "application/xhtml+xml"}:
                raise NavigationFailure(f"Unexpected content type at {url}: {content_type}")
            return raw, content_type, self.last_request_at + self.navigation_timeout
        finally:
            await response.dispose()

    @asynccontextmanager
    async def _document(self, value: str) -> AsyncIterator[tuple[Page, str, float]]:
        async with self.page_lock:
            async with self._open_document(value) as document:
                yield document

    @asynccontextmanager
    async def _open_document(self, value: str) -> AsyncIterator[tuple[Page, str, float]]:
        url = self._authorize(value)
        try:
            document = await self._fetch(url)
            if document is None:
                document = await self._fetch(url, retry=True)
            assert document is not None
            raw, content_type, deadline = document
            page = await self.context.new_page()

            async def deliver(route: Route) -> None:
                if route.request.method == "GET" and route.request.is_navigation_request():
                    await route.fulfill(
                        status=200, body=raw, content_type=content_type,
                        headers={"content-security-policy": CSP, "x-dns-prefetch-control": "off"},
                    )
                else:
                    await route.abort()

            try:
                page.set_default_timeout(self.navigation_timeout * 1000)
                # Only this initial browser navigation can render the HTTP
                # document. No browser route performs a network request.
                await page.route(lambda request_url: request_url == url, deliver, times=1)
                await page.goto(url, wait_until="domcontentloaded", timeout=max(1, (deadline - time.monotonic()) * 1000))
                yield page, url, deadline
            finally:
                await page.close()
        except (self.playwright_error, TimeoutError, NavigationFailure) as exc:
            self.consecutive_failures += 1
            if self.consecutive_failures >= 3:
                raise StopRun("error", f"Three consecutive navigation failures. Last failure: {url}: {exc}") from exc
            raise NavigationFailure(f"{url}: {exc}") from exc
        else:
            self.consecutive_failures = 0

    async def _snapshot(self, page: Page) -> dict[str, Any]:
        snapshot = await page.evaluate(SNAPSHOT, self.selectors)
        top_lines = [line.strip() for line in snapshot["body"].splitlines() if line.strip()][:3]
        for line in [snapshot["title"], *snapshot["block"], *top_lines]:
            if self.patterns["block_text"].search(tidy(line)):
                raise StopRun("blocked", f"Access challenge at {page.url}: {tidy(line)[:160]}")
        return snapshot

    async def _until(
        self, sample: Callable[[], Awaitable[T]], ready: Callable[[T], bool], timeout: float
    ) -> T:
        end = time.monotonic() + timeout
        while True:
            result = await sample()
            if ready(result) or time.monotonic() >= end:
                return result
            await asyncio.sleep(min(0.25, max(0, end - time.monotonic())))

    def _empty(self, snapshot: dict[str, Any]) -> bool:
        return bool(self.patterns["empty_text"].search(snapshot["body"]))

    async def _control(self, page: Page, name: str) -> Locator | None:
        matches = page.locator(self.selectors[name]).filter(has_text=self.patterns[f"{name}_text"])
        for control in await matches.all():
            if await control.is_visible() and await control.is_enabled():
                return control
        return None

    async def _click(self, control: Locator) -> None:
        try:
            await control.click(timeout=self.interaction_timeout * 1000)
        except self.playwright_timeout as exc:
            LOG.debug("Interaction did not complete: %s", exc)

    async def read_tree_links(self, value: str, *, strict: bool = True) -> TreePage:
        async with self._document(value) as (page, url, deadline):
            snapshot = await self._until(
                lambda: self._snapshot(page),
                lambda state: bool(extract_tree(url, state["links"])) or self._empty(state),
                max(0, deadline - time.monotonic()),
            )
            links = extract_tree(url, snapshot["links"])
            empty = self._empty(snapshot)
            if strict and not links and not empty:
                raise NavigationFailure("No child browse links or empty-state text found before timeout")
            return TreePage(links, await page.content(), empty)

    async def read_cards(
        self, value: str, *, cutoff: datetime, parse_date: Callable[[str], datetime | None],
        max_clicks: int | None = None,
    ) -> CityPage:
        click_limit = self.max_clicks if max_clicks is None else min(self.max_clicks, max_clicks)
        async with self._document(value) as (page, url, deadline):
            snapshot = await self._until(
                lambda: self._snapshot(page),
                lambda state: bool(extract_cards(url, state["cards"])) or self._empty(state),
                max(0, deadline - time.monotonic()),
            )
            batch = extract_cards(url, snapshot["cards"])
            cards = list(batch)
            seen = {job_id(card.url) for card in cards}
            control = await self._control(page, "view_more")
            view_more = f"{self.selectors['view_more']} with text {self.selectors['view_more_text']}" if control else ""
            for _ in range(click_limit):
                if not control:
                    break
                dates = [parse_date(card.posted_text) for card in batch]
                if dates and newest_first(cards, parse_date) and all(date is not None and date < cutoff for date in dates):
                    break
                await self._click(control)
                snapshot = await self._until(
                    lambda: self._snapshot(page),
                    lambda state: any(job_id(card.url) not in seen for card in extract_cards(url, state["cards"])),
                    self.interaction_timeout,
                )
                batch = [card for card in extract_cards(url, snapshot["cards"]) if job_id(card.url) not in seen]
                if not batch:
                    break
                cards.extend(batch)
                seen.update(job_id(card.url) for card in batch)
                control = await self._control(page, "view_more")
            return CityPage(cards, view_more, await page.content(), self._empty(snapshot), newest_first(cards, parse_date))

    async def read_job(self, value: str, *, expand: bool = True) -> JobPage:
        async with self._document(value) as (page, url, deadline):
            def ready(state: dict[str, Any]) -> bool:
                fields = state["job"]
                return bool(fields["title"]) and bool(
                    fields["description"] or labeled_text(fields["body"], "Posted") or
                    any(self.patterns["apply_text"].fullmatch(tidy(link["text"])) for link in fields["apply"])
                )

            snapshot = await self._until(
                lambda: self._snapshot(page),
                ready,
                max(0, deadline - time.monotonic()),
            )
            if expand and not ready(snapshot):
                raise NavigationFailure("No job heading and description, Posted label, or Apply Offsite link found")
            if expand and (control := await self._control(page, "read_more")):
                previous = snapshot["job"]["description"] or snapshot["job"]["body"]
                await self._click(control)
                snapshot = await self._until(
                    lambda: self._snapshot(page),
                    lambda state: (state["job"]["description"] or state["job"]["body"]) != previous,
                    self.interaction_timeout,
                )
            return extract_job(url, snapshot["job"], self.patterns["apply_text"], await page.content())
