"""Builds REPORT.md: the latest matches as tables that read well on GitHub (desktop or phone)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .store import ROOT, read_matches, read_rejected

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
    fit = any(r.get("has") or r.get("missing") for r in rows)
    ai = not fit and any(str(r.get("ai_score", "")).strip() for r in rows)
    if fit:   # rule-based fit score: what the job asks that the resume has, and what it lacks
        out = ["| Score | Role | Company | Location | Posted | Has | Missing | Notes |",
               "|---:|---|---|---|---|---|---|---|"]
        for r in rows:
            title = _cell(r["title"], 70).replace("[", "(").replace("]", ")")
            loc = _cell(r["location"], 40) + (f" ({r['workplace']})" if r.get("workplace") else "")
            out.append(
                f"| {r['score']} | [{title}]({r['url']}) | {_cell(r['company'], 30)} | {loc} | "
                f"{_when(r.get('posted') or r['first_seen'], tz)} | {_cell(r.get('has', ''), 90)} | "
                f"{_cell(r.get('missing', ''), 60) or '–'} | {_cell(r.get('flags', ''), 70)} |"
            )
        return out
    if ai:   # a resume check ran: show its score and reason next to the radar's notes
        out = ["| Score | AI | Role | Company | Location | Posted | AI check | Notes |",
               "|---:|---:|---|---|---|---|---|---|"]
    else:
        out = ["| Score | Role | Company | Location | Posted | Notes |", "|---:|---|---|---|---|---|"]
    for r in rows:
        title = _cell(r["title"], 70).replace("[", "(").replace("]", ")")
        loc = _cell(r["location"], 40) + (f" ({r['workplace']})" if r.get("workplace") else "")
        when = _when(r.get("posted") or r["first_seen"], tz)
        if ai:
            ai_score = str(r.get("ai_score", "")).strip() or "–"
            out.append(
                f"| {r['score']} | {ai_score} | [{title}]({r['url']}) | {_cell(r['company'], 30)} | {loc} | {when} | "
                f"{_cell(r.get('ai_why', ''), 160)} | {_cell(r.get('flags', ''), 70)} |"
            )
        else:
            out.append(
                f"| {r['score']} | [{title}]({r['url']}) | {_cell(r['company'], 30)} | {loc} | "
                f"{when} | {_cell(r.get('flags', ''), 70)} |"
            )
    return out


def build_report(tz_name: str, boards_total: int, now: datetime | None = None, profile: dict | None = None,
                 closed: set[str] = frozenset(), handled: set[str] = frozenset()) -> str:
    """`profile` (the `profile:` section of a profile's config) changes the wording and how far back the
    report looks; `closed` holds matches whose postings have been taken down."""
    tz = ZoneInfo(tz_name)
    now = now or datetime.now(timezone.utc)
    p = profile or {}
    days = int(p.get("report_days", 7))
    span = "this week" if days == 7 else f"in the last {days} days"
    rows = [r for r in read_matches(since=now - timedelta(days=days)) if r["uid"] not in closed]
    done = [r for r in rows if r["uid"] in handled]
    rows = [r for r in rows if r["uid"] not in handled]
    visible = [r for r in rows if r["status"] in ("alert", "quiet")]
    blocked = [r for r in rows if r["status"] == "blocked"] + read_rejected(since=now - timedelta(days=days))
    def when(r):  # posting time when the job board gives it, otherwise when the radar first saw it
        try:
            d = datetime.fromisoformat(r["posted"]) if r.get("posted") else r["_first_seen"]
        except ValueError:
            d = r["_first_seen"]
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

    cutoff = now - timedelta(hours=24)
    day = [r for r in visible if when(r) >= cutoff]
    week = [r for r in visible if when(r) < cutoff]
    key = lambda r: (-r["score"], r.get("posted") or "")
    day.sort(key=key)
    week.sort(key=key)
    last_new = max((r["_first_seen"] for r in rows), default=None)

    lines = [
        f"# {p.get('label', 'Job Radar')}: latest matches",
        "",
        f"Watching **{boards_total:,} company job boards** " + p.get(
            "watching", "every 30 minutes for entry-friendly DevOps, SRE, cloud, platform and infrastructure "
                        "roles in the US."),
        "",
        f"Last new match: **{last_new.astimezone(tz).strftime('%a %b %d, %I:%M %p %Z') if last_new else 'none yet'}**. "
        + p.get("score_note", "Score is 0–100 (higher = better fit for an early-career candidate on OPT).")
        + f" Full history: [{p.get('matches_file', 'data/matches.csv')}]({p.get('matches_file', 'data/matches.csv')}).",
        "",
        *( [f"{len(done)} jobs you marked as applied or not interested are left out.", ""] if done else []),
        f"## Posted in the last 24 hours ({len(day)})",
        "",
    ]
    max_rows = int(p.get("report_rows", 150))
    lines += _table(day[:max(80, max_rows)], tz) if day else ["Nothing posted in the last 24 hours."]
    lines += ["", f"## Found {span}, posted earlier ({len(week)})", ""]
    if len(week) > max_rows:
        lines += [f"Showing the {max_rows} best of {len(week)}; the rest are in the full history file.", ""]
    lines += _table(week[:max_rows], tz) if week else [f"No other matches {span}."]
    lines += [
        "",
        f"## Hidden from alerts: clearance/citizenship or rejected by the AI check ({len(blocked)} {span})",
        "",
        p.get("hidden_note",
              "Clearance, US citizenship or ITAR requirements (which F-1/OPT holders can't meet), or jobs the AI "
              "reviewer judged off-target or too senior. The reason is in the flags column. Listed so you can "
              "double-check."),
        "",
    ]
    if blocked:
        for r in blocked:
            if r.get("reject_reason"):
                r["flags"] = r["reject_reason"]
        blocked.sort(key=key)
        lines += _table(blocked[:int(p.get("hidden_rows", 40))], tz)
    else:
        lines.append(f"None {span}.")
    lines.append("")
    return "\n".join(lines)


def write_report(text: str) -> None:
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(text)
