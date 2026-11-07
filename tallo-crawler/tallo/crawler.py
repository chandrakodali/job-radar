"""Resumable discovery and job processing."""
from __future__ import annotations

import hashlib
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .browser import Browser, NavigationFailure, StopRun, URLDenied, browse_parts, job_id
from .parsing import (
    TitleMatch,
    classify_authorization,
    classify_location,
    classify_title,
    extract_years,
    parse_posted,
    requires_current_enrollment,
    score_job,
)
from .storage import Storage, read_json, write_json, write_text

LOG = logging.getLogger(__name__)
BROWSE = "https://tallo.com/talent/job/browse/technology"
SOFTWARE = "software-developer-or-engineer"
CHICAGO = ZoneInfo("America/Chicago")
LAYOUT_MESSAGE = "Layout changed: run --probe and update config.yaml"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    browser_config: dict
    categories: tuple[str, ...]
    states: tuple[str, ...]
    core_cities: int
    rotation: int
    max_pages: int
    since_hours: float
    run_minutes: float
    alert_threshold: int
    dry_run: bool
    probe: bool


@dataclass
class Stats:
    cities: int = 0
    cards: int = 0
    jobs: Counter[str] = field(default_factory=Counter)
    alerts: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class City:
    url: str
    label: str
    count: int


def plan_cities(
    cities: list[City], categories: tuple[str, ...], core_limit: int,
    rotation: int, position: int,
) -> tuple[list[City], int]:
    priority = {category: rank for rank, category in enumerate(categories)}
    ordered = sorted(
        cities,
        key=lambda city: (-city.count, priority[browse_parts(city.url)[0]], city.url),
    )
    core = ordered[:core_limit]
    rotating = [
        city for city in ordered[core_limit:]
        if int.from_bytes(hashlib.sha256(city.url.encode()).digest()[:8], "big")
        % rotation == position
    ]
    return core + rotating, len(core)


class Crawl:
    def __init__(self, settings: Settings, browser: Browser, storage: Storage, stats: Stats):
        self.settings = settings
        self.browser = browser
        self.storage = storage
        self.stats = stats
        self.tree_path = settings.data_dir / "tree.json"
        self.checkpoint_path = settings.data_dir / "checkpoint.json"
        self.tree = read_json(self.tree_path) or {}
        self.cache = self.tree.setdefault("categories", {})
        self.day = datetime.now(CHICAGO).date().isoformat()
        self.layout_misses = 0
        primary = tuple(category for category in settings.categories if category != SOFTWARE)
        self.groups = [group for group in (primary, (SOFTWARE,) if SOFTWARE in settings.categories else ()) if group]
        signature = {
            "categories": list(settings.categories),
            "states": list(settings.states),
            "core_cities": settings.core_cities,
            "rotation": settings.rotation,
        }
        checkpoint = read_json(self.checkpoint_path)
        if checkpoint and checkpoint.get("version") == 1 and checkpoint.get("scope") == signature:
            self.checkpoint = checkpoint
        else:
            if checkpoint:
                LOG.info("Crawl scope changed. Starting a new city plan with the existing job history.")
            self.checkpoint = {
                "version": 1, "scope": signature, "position": 0, "group": 0,
                "core_remaining": settings.core_cities, "pending": None,
            }

    def persist(self, path: Path, value: dict) -> None:
        if not self.settings.dry_run:
            write_json(path, value)

    def save_checkpoint(self) -> None:
        self.persist(self.checkpoint_path, self.checkpoint)

    async def discover(self, categories: tuple[str, ...], day: str) -> tuple[list[City], bool]:
        cities: dict[str, City] = {}
        retry = False
        for category in categories:
            cached = self.cache.get(category)
            if not cached or cached.get("day") != day:
                category_url = f"{BROWSE}/{category}"
                try:
                    page = await self.browser.read_tree_links(category_url)
                except URLDenied:
                    state_urls = []
                except NavigationFailure as exc:
                    LOG.warning("Discovery deferred for %s (%s)", category_url, exc)
                    retry = True
                    continue
                else:
                    state_urls = [
                        link.url for link in page.links
                        if (parts := browse_parts(link.url)) and len(parts) == 2
                        and parts[0] == category
                    ]
                cached = {"day": day, "states": sorted(set(state_urls)), "cities_by_state": {}}
                self.cache[category] = cached
                self.persist(self.tree_path, self.tree)
            by_state = cached["cities_by_state"]
            for state_url in cached["states"]:
                parts = browse_parts(state_url)
                if not parts or len(parts) != 2 or parts[0] != category:
                    continue
                if self.settings.states and parts[1] not in self.settings.states:
                    continue
                if state_url not in by_state:
                    try:
                        page = await self.browser.read_tree_links(state_url)
                    except URLDenied:
                        records = []
                    except NavigationFailure as exc:
                        LOG.warning("Discovery deferred for %s (%s)", state_url, exc)
                        retry = True
                        continue
                    else:
                        records = [
                            asdict(City(link.url, link.label, link.count or 0))
                            for link in page.links
                            if (child := browse_parts(link.url)) and child[:-1] == parts
                        ]
                    by_state[state_url] = records
                    self.persist(self.tree_path, self.tree)
                for record in by_state[state_url]:
                    city = City(**record)
                    child = browse_parts(city.url)
                    if child and child[:-1] == parts:
                        cities[city.url] = city
        return list(cities.values()), retry

    async def run(self) -> None:
        self.save_checkpoint()
        partial_discovery = False
        while self.checkpoint["group"] < len(self.groups):
            categories = self.groups[self.checkpoint["group"]]
            if self.checkpoint["pending"] is None:
                if not self.checkpoint.get("discovery_day"):
                    self.checkpoint["discovery_day"] = self.day
                    self.save_checkpoint()
                cities, retry = await self.discover(categories, self.checkpoint["discovery_day"])
                partial_discovery |= retry
                plan, core_used = plan_cities(
                    cities, categories, self.checkpoint["core_remaining"],
                    self.settings.rotation, self.checkpoint["position"],
                )
                self.checkpoint["pending"] = [asdict(city) for city in plan]
                self.checkpoint["core_remaining"] -= core_used
                self.save_checkpoint()
                LOG.info("Scheduled %s cities including %s core cities", len(plan), core_used)
            await self.run_pending()
            self.checkpoint["group"] += 1
            self.checkpoint["pending"] = None
            self.checkpoint["discovery_day"] = None
            self.save_checkpoint()

        self.checkpoint.update(
            position=(self.checkpoint["position"] + 1) % self.settings.rotation,
            group=0, core_remaining=self.settings.core_cities, pending=None,
        )
        self.save_checkpoint()
        if partial_discovery:
            raise StopRun("error", "Crawled the available cities. The next sweep will retry deferred discovery pages.")

    async def run_pending(self) -> None:
        pending = self.checkpoint["pending"]
        for _ in range(len(pending)):
            city = pending[0]
            try:
                complete = await self.run_city(city)
            except URLDenied:
                complete = True
            except NavigationFailure as exc:
                self.layout_misses = 0
                LOG.warning("City deferred for %s (%s)", city["url"], exc)
                complete = False
            pending.pop(0)
            if not complete and not city.get("deferred_once"):
                city["deferred_once"] = True
                pending.append(city)
            elif not complete:
                LOG.warning("Deferring %s until its next scheduled sweep", city["url"])
            self.save_checkpoint()
        if pending:
            raise StopRun("error", "Some city or job pages were deferred. The next run will resume them.")

    async def run_city(self, city: dict) -> bool:
        parts = browse_parts(city["url"])
        if not parts or len(parts) != 3:
            LOG.warning("Discarding invalid city URL from checkpoint")
            return True
        category, state, city_slug = parts
        candidates = city.get("candidates")
        if candidates is None:
            observed = datetime.now(UTC)
            cutoff = observed - timedelta(hours=self.settings.since_hours)
            page = await self.browser.read_cards(
                city["url"], cutoff=cutoff,
                parse_date=lambda value: parse_posted(value, observed),
            )
            self.stats.cards += len(page.cards)
            if not page.cards and city["count"] > 0:
                self.layout_misses += 1
                name = f"{category}-{state}-{city_slug}.html"
                self.save_html(Path("probe") / "layout" / name, page.html)
                LOG.warning("Expected jobs but found no links at %s", city["url"])
                if self.layout_misses >= 3:
                    raise StopRun("error", LAYOUT_MESSAGE)
                return False
            self.layout_misses = 0
            candidates = []
            for card in page.cards:
                ident = job_id(card.url)
                if not ident or self.storage.has_seen(ident):
                    continue
                if not self.title_match(card.title).family:
                    continue
                posted = parse_posted(card.posted_text, observed)
                if posted and posted < cutoff:
                    continue
                candidates.append({
                    "url": card.url, "title": card.title, "posted_text": card.posted_text,
                    "posted": posted.isoformat() if posted else None, "first_seen": observed.isoformat(),
                })
            city["candidates"] = candidates
            self.save_checkpoint()
        deferred = []
        while candidates:
            candidate = candidates[0]
            try:
                await self.run_job(candidate, category, state, city["label"] or city_slug)
            except URLDenied:
                pass
            except NavigationFailure as exc:
                LOG.warning("Job deferred for %s (%s)", candidate["url"], exc)
                deferred.append(candidate)
            candidates.pop(0)
            city["candidates"] = deferred + candidates
            self.save_checkpoint()
        if deferred:
            return False
        self.stats.cities += 1
        return True

    def title_match(self, title: str) -> TitleMatch:
        return classify_title(title, self.settings.browser_config["keywords"])

    async def run_job(self, candidate: dict, category: str, state: str, city: str) -> None:
        ident = job_id(candidate["url"])
        if not ident or self.storage.has_seen(ident):
            return
        now = datetime.now(UTC)
        cutoff = now - timedelta(hours=self.settings.since_hours)
        card_date = datetime.fromisoformat(candidate["posted"]) if candidate["posted"] else None
        if card_date and card_date < cutoff:
            return
        job = await self.browser.read_job(candidate["url"])
        title = job.title or candidate["title"]
        match = self.title_match(title)
        location = classify_location(job.location, city, state)
        first_seen = datetime.fromisoformat(candidate["first_seen"]) if candidate.get("first_seen") else now
        posted = parse_posted(job.posted_text, now)
        posted_text = job.posted_text or candidate["posted_text"]
        if posted is None and card_date is not None:
            posted, posted_text = card_date, candidate["posted_text"]
        if (
            not match.family or not location.is_us or not cutoff <= (posted or first_seen) <= now
            or requires_current_enrollment(title, job.description)
        ):
            self.storage.remember(ident, first_seen, title, job.company, city)
            return
        authorization = classify_authorization(title, job.description, job.company)
        years = extract_years(job.description)
        result = score_job(match, years, posted, now, location, authorization, self.settings.alert_threshold)
        if result.status == "too_senior":
            self.storage.remember(ident, first_seen, title, job.company, city)
            self.stats.jobs["too_senior"] += 1
            LOG.info("Too senior (%d years) %s", years, candidate["url"])
            return
        apply_url, apply_domain = offsite_details(job.apply_url)
        row = {
            "first_seen_utc": first_seen, "posted_utc": posted,
            "posted_text": posted_text,
            "score": result.score, "status": result.status,
            "score_reasons": result.reasons, "flags": result.flags,
            "title": title, "company": job.company, "location": job.location,
            "work_type": location.work_type, "employment_type": job.employment_type,
            "pay": job.pay, "years_required": years,
            "job_url": candidate["url"], "apply_url": apply_url, "apply_domain": apply_domain,
            "category": category, "state": state, "city": city, "job_id": ident,
        }
        status = self.storage.save(row)
        if status != "duplicate":
            self.stats.jobs[status] += 1
        if status == "alert":
            self.stats.alerts.append(row)

    def save_html(self, relative_path: Path, html: str) -> None:
        if not self.settings.dry_run:
            write_text(self.settings.data_dir / relative_path, html)


def offsite_details(url: str) -> tuple[str, str]:
    try:
        parts = urlsplit(url)
        if parts.scheme in {"http", "https"} and parts.hostname and not parts.username and not parts.password:
            return url, parts.hostname
    except ValueError:
        pass
    return "", ""


async def probe(settings: Settings, browser: Browser, stats: Stats) -> None:
    cfg = settings.browser_config["probe"]
    category = cfg["category"] if cfg["category"] in settings.categories else settings.categories[0]
    category_url = f"{BROWSE}/{category}"

    def capture(name: str, html: str) -> None:
        if not settings.dry_run:
            write_text(settings.data_dir / "probe" / f"{name}.html", html)

    async def choose_child(url: str, name: str, preferred: str) -> str:
        result = await browser.read_tree_links(url, strict=False)
        capture(name, result.html)
        parent = browse_parts(url)
        children = [
            link for link in result.links
            if (parts := browse_parts(link.url)) and parts[:-1] == parent
        ]
        if name == "category" and settings.states:
            children = [link for link in children if browse_parts(link.url)[1] in settings.states]
        LOG.info("Probe %s %s found %s child links", name, url, len(children))
        if not children:
            raise StopRun("error", f"No child links found on the probe {name} page. Check config.yaml.")
        return max(
            children, key=lambda link: (link.url.endswith(f"/{preferred}"), link.count or 0, link.url),
        ).url

    state = await choose_child(category_url, "category", cfg["state"])
    city = await choose_child(state, "state", cfg["city"])
    now = datetime.now(UTC)
    result = await browser.read_cards(
        city, cutoff=now - timedelta(hours=settings.since_hours),
        parse_date=lambda value: parse_posted(value, now), max_clicks=0,
    )
    capture("city", result.html)
    stats.cities += 1
    stats.cards += len(result.cards)
    LOG.info("Probe city %s | View More selector %r", city, result.view_more)
    for card in result.cards:
        LOG.info("Card %s | posted %r | %s", card.url, card.posted_text, " ".join(card.text.split()))
    if not result.cards:
        raise StopRun("error", "Probe found no job links. Review the saved city HTML and config.yaml.")
    selected = next((card for card in result.cards if card.posted_text), result.cards[0])
    job = await browser.read_job(selected.url, expand=False)
    capture("job", job.html)
    work_type = classify_location(job.location).work_type
    LOG.info(
        "Probe job %s | title=%r | company=%r | location=%r | work type=%r | posted=%r | parsed=%s | "
        "last updated=%r | employment=%r | pay=%r | apply=%r | years=%s",
        selected.url, job.title, job.company, job.location, work_type, job.posted_text,
        parse_posted(job.posted_text, now), job.last_updated_text, job.employment_type, job.pay, job.apply_url,
        extract_years(job.description),
    )
    LOG.info("Probe description (%d characters) | %.1200s", len(job.description), " ".join(job.description.split()))
    missing = [name for name in ("title", "company", "location", "description") if not getattr(job, name)]
    if missing:
        raise StopRun("error", f"Probe could not verify {', '.join(missing)}. Review the job HTML and selectors.")
