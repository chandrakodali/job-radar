#!/usr/bin/env python3
"""Command-line entry point for the Tallo job crawler."""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import re
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path

# A dry run should not create bytecode caches in the project.
sys.dont_write_bytecode = True

import requests
import yaml

from tallo.browser import Browser, StopRun
from tallo.crawler import Crawl, Settings, Stats, probe
from tallo.storage import Storage

LOG = logging.getLogger("tallo")
PROJECT = Path(__file__).resolve().parent
DEFAULT_CONTACT = "https://github.com/chandrakodali/job-radar"
CONTACT_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def arguments() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PROJECT / "config.yaml")
    parser.add_argument("--probe", action="store_true", help="inspect one category, state, city and job")
    parser.add_argument("--dry-run", action="store_true", help="inspect live pages without writing data or sending alerts")
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--since-hours", type=float)
    parser.add_argument("--categories", nargs="+")
    parser.add_argument("--states", nargs="+")
    parser.add_argument("--core-cities", type=int)
    parser.add_argument("--rotation", type=int)
    parser.add_argument("--verbose", action="store_true")
    return parser


def number(name: str, value: object, minimum: float, maximum: float, *, integer: bool = False) -> None:
    types = (int,) if integer else (int, float)
    if type(value) not in types or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")


def names(name: str, values: object, pattern: re.Pattern[str]) -> tuple[str, ...]:
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not pattern.fullmatch(value.lower()) for value in values
    ):
        raise ValueError(f"{name} must be a list of lowercase URL segments")
    return tuple(dict.fromkeys(value.lower() for value in values))


def read_settings(args: argparse.Namespace) -> Settings:
    with args.config.open(encoding="utf-8") as stream:
        cfg = yaml.safe_load(stream)
    if not isinstance(cfg, dict):
        raise ValueError("config.yaml must contain a mapping")
    # The bot identifies itself with the project URL (no personal email needed).
    contact = str(cfg.get("contact_url") or DEFAULT_CONTACT).strip()
    cfg["contact_email"] = contact
    budgets, thresholds = cfg["budgets"], cfg["thresholds"]
    categories = names("categories", args.categories or cfg["categories"], SLUG)
    if not categories:
        raise ValueError("Select at least one category")
    states = names("states", args.states or cfg["states"], re.compile("[a-z]{2}"))

    def override(name: str, config: dict) -> object:
        value = getattr(args, name)
        return config[name] if value is None else value

    max_pages = override("max_pages", budgets)
    core_cities = override("core_cities", budgets)
    rotation = override("rotation", budgets)
    since_hours = override("since_hours", thresholds)
    run_minutes = budgets["run_minutes"]
    alert_threshold = thresholds["alert_score"]
    number("max_pages", max_pages, 4 if args.probe else 1, math.inf, integer=True)
    number("core_cities", core_cities, 0, math.inf, integer=True)
    number("rotation", rotation, 1, math.inf, integer=True)
    number("since_hours", since_hours, 0.01, 87600)
    number("run_minutes", run_minutes, 0.01, 55)
    number("alert_score", alert_threshold, 0, 100, integer=True)
    number("delay_min_seconds", budgets["delay_min_seconds"], 3, 6)
    number("delay_max_seconds", budgets["delay_max_seconds"], budgets["delay_min_seconds"], 6)
    number("retry_5xx_seconds", budgets["retry_5xx_seconds"], 30, 30)
    number("navigation_timeout_seconds", budgets["navigation_timeout_seconds"], 1, 45)
    number("interaction_timeout_seconds", budgets["interaction_timeout_seconds"], 1, 45)
    number("max_view_more_clicks", budgets["max_view_more_clicks"], 0, 15, integer=True)
    for group in ("core", "bridge", "excluded"):
        patterns = cfg["keywords"][group]
        if not isinstance(patterns, list) or any(not isinstance(pattern, str) for pattern in patterns):
            raise ValueError(f"keywords.{group} must be a list of regular expressions")
        for pattern in patterns:
            re.compile(pattern, re.I)
    for key, value in cfg["selectors"].items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"selectors.{key} must be a nonempty string")
        if key.endswith("_text"):
            re.compile(value, re.I)
    for key in ("category", "state", "city"):
        if not isinstance(cfg["probe"][key], str) or not SLUG.fullmatch(cfg["probe"][key]):
            raise ValueError(f"probe.{key} must be a lowercase URL segment")

    return Settings(
        data_dir=args.config.resolve().parent / "data", browser_config=cfg,
        categories=categories, states=states, core_cities=core_cities, rotation=rotation,
        max_pages=max_pages, since_hours=since_hours, run_minutes=run_minutes,
        alert_threshold=alert_threshold, dry_run=args.dry_run, probe=args.probe,
    )


@contextmanager
def run_lock(data_dir: Path) -> Iterator[None]:
    """Serialize local writers with a lock the OS releases on process exit."""
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / ".run.lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError("Another crawler is writing this data directory") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("Another crawler is writing this data directory") from exc
        yield


def send_alerts(topic: str, alerts: list[dict], email: str, deadline: float) -> None:
    """Publish optional phone notifications after the durable report is ready."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
        LOG.warning("Ignoring invalid NTFY_TOPIC")
        return
    ranked = sorted(alerts, key=lambda row: row["score"], reverse=True)
    messages = [
        {
            "title": row["title"][:160],
            "message": f"{row['company'][:150]} · {row['location'][:150]} · Score {row['score']}",
            "click": row["job_url"],
        }
        for row in ranked[:15]
    ]
    if len(ranked) > 15:
        messages.append({"title": "Tallo jobs", "message": f"{len(ranked) - 15} more in the report"})
    with requests.Session() as session:
        session.trust_env = False
        session.headers["User-Agent"] = f"personal-job-search-bot/1.0 (+contact: {email})"
        for message in messages:
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                LOG.warning("Stopped sending phone alerts at the run time limit")
                break
            try:
                with session.post(
                    "https://ntfy.sh/", json={"topic": topic, **message},
                    timeout=min(4, remaining / 3), allow_redirects=False, stream=True,
                ) as response:
                    if not 200 <= response.status_code < 300:
                        LOG.warning("ntfy returned HTTP %s. Stopping phone alerts.", response.status_code)
                        break
            except requests.RequestException as exc:
                LOG.warning("Phone alert failed (%s)", type(exc).__name__)
                break


async def execute(settings: Settings) -> int:
    start = time.monotonic()
    deadline = start + settings.run_minutes * 60
    stats = Stats()
    browser = None
    reason = "error"
    try:
        browser = Browser(settings.browser_config, max_pages=4 if settings.probe else settings.max_pages)
        lock = nullcontext() if settings.dry_run else run_lock(settings.data_dir)
        with lock:
            storage_context = nullcontext() if settings.probe else Storage(settings.data_dir, dry_run=settings.dry_run)
            with storage_context as storage:
                timed_out = False
                try:
                    if storage is not None:
                        storage.prune(datetime.now(UTC))
                    async with asyncio.timeout(max(0, deadline - time.monotonic())):
                        async with browser:
                            if settings.probe:
                                await probe(settings, browser, stats)
                            else:
                                await Crawl(settings, browser, storage, stats).run()
                    reason = "finished"
                except StopRun as exc:
                    reason = exc.reason
                    LOG.warning("%s", exc)
                except TimeoutError:
                    reason, timed_out = "budget", True
                    LOG.warning("Run time limit reached")
                finally:
                    if storage is not None:
                        storage.flush_csv()
                        storage.write_report(datetime.now(UTC), settings.since_hours)
                topic = os.getenv("NTFY_TOPIC")
                if (
                    topic and stats.alerts and not settings.dry_run and not timed_out
                    and reason in {"finished", "budget"}
                ):
                    await asyncio.to_thread(
                        send_alerts, topic, stats.alerts, settings.browser_config["contact_email"], deadline,
                    )
    except Exception as exc:
        reason = "error"
        LOG.error("%s", exc, exc_info=LOG.isEnabledFor(logging.DEBUG))
    finally:
        statuses = ("alert", "quiet", "blocked", "repost", "too_senior")
        LOG.info(
            "Summary | pages loaded %d | cities covered %d | cards seen %d | new jobs %d (%s) | "
            "stop reason %s | elapsed %.1fs",
            browser.pages_loaded if browser else 0, stats.cities, stats.cards, sum(stats.jobs.values()),
            ", ".join(f"{status}={stats.jobs[status]}" for status in statuses),
            reason, time.monotonic() - start,
        )
    return int(reason in {"blocked", "error"} or (settings.probe and reason != "finished"))


def main() -> int:
    parser = arguments()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if args.verbose:
        logging.getLogger("tallo").setLevel(logging.DEBUG)
    try:
        settings = read_settings(args)
    except (KeyError, OSError, TypeError, ValueError, re.error, yaml.YAMLError) as exc:
        parser.error(str(exc))
    try:
        return asyncio.run(execute(settings))
    except KeyboardInterrupt:
        LOG.info("Interrupted. Saved crawl progress is available for the next run.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
