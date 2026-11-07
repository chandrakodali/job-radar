"""Builds REPORT.md: the latest matches as tables that read well on GitHub (desktop or phone)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .store import ROOT, read_matches

REPORT_PATH = os.path.join(ROOT, "REPORT.md")


def _cell(s: str, limit: int = 60) -> str:
    s = (s or "").replace("|", "/").replace("\n", " ").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _when(iso: str, tz: ZoneInfo) -> str:
    if not iso:
        return "?"
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return "?"
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(tz).strftime("%b %d")


def _table(rows: list[dict], tz: ZoneInfo) -> list[str]:
    out = ["| Score | Role | Company | Location | Posted | Notes |", "|---:|---|---|---|---|---|"]
    for r in rows:
        title = _cell(r["title"], 70).replace("[", "(").replace("]", ")")
        loc = _cell(r["location"], 40) + (f" ({r['workplace']})" if r.get("workplace") else "")
        out.append(
            f"| {r['score']} | [{title}]({r['url']}) | {_cell(r['company'], 30)} | {loc} | "
            f"{_when(r.get('posted') or r['first_seen'], tz)} | {_cell(r.get('flags', ''), 70)} |"
        )
    return out


def build_report(tz_name: str, boards_total: int, now: datetime | None = None) -> str:
    tz = ZoneInfo(tz_name)
    now = now or datetime.now(timezone.utc)
    rows = read_matches(since=now - timedelta(days=7))
    visible = [r for r in rows if r["status"] in ("alert", "quiet")]
    blocked = [r for r in rows if r["status"] == "blocked"]
    day = [r for r in visible if r["_first_seen"] >= now - timedelta(hours=24)]
    week = [r for r in visible if r["_first_seen"] < now - timedelta(hours=24)]
    key = lambda r: (-r["score"], r.get("posted") or "")
    day.sort(key=key)
    week.sort(key=key)
    last_new = max((r["_first_seen"] for r in rows), default=None)

    lines = [
        "# Job Radar: latest matches",
        "",
        f"Watching **{boards_total:,} company job boards** every 30 minutes for entry-friendly "
        "DevOps, SRE, cloud, platform and infrastructure roles in the US.",
        "",
        f"Last new match: **{last_new.astimezone(tz).strftime('%a %b %d, %I:%M %p %Z') if last_new else 'none yet'}**. "
        "Score is 0–100 (higher = better fit for an early-career candidate on OPT). "
        "Full history: [data/matches.csv](data/matches.csv).",
        "",
        f"## New in the last 24 hours ({len(day)})",
        "",
    ]
    lines += _table(day[:80], tz) if day else ["Nothing new in the last 24 hours."]
    lines += ["", f"## Earlier this week ({len(week)})", ""]
    lines += _table(week[:150], tz) if week else ["No other matches this week."]
    lines += [
        "",
        f"## Hidden from alerts: needs clearance or US citizenship ({len(blocked)} this week)",
        "",
        "These mention a security clearance, US citizenship, ITAR or similar, which F-1/OPT holders can't meet. "
        "Listed so you can double-check.",
        "",
    ]
    if blocked:
        blocked.sort(key=key)
        lines += _table(blocked[:40], tz)
    else:
        lines.append("None this week.")
    lines.append("")
    return "\n".join(lines)


def write_report(text: str) -> None:
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(text)
