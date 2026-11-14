"""Builds REPORT.md: the latest matches as tables that read well on GitHub (desktop or phone)."""
from __future__ import annotations

import os
import re
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
    if any(r.get("ats") == "adzuna" for r in rows):
        lines += ["", "Some listings: [Jobs by Adzuna](https://www.adzuna.com)."]
    lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------ newest-first weekly view

_LEVEL = re.compile(r"[^a-z0-9+#]+")


def dup_key(r: dict) -> str:
    """Same company and same title: one row, however many places or sources list it."""
    c = re.sub(r"\b(inc|llc|ltd|corp(oration)?|co|company|group)\b\.?", " ", (r.get("company") or "").lower())
    t = re.sub(r"\(.*?\)|\[.*?\]|\s[-–|]\s.*$", " ", (r.get("title") or "").lower())
    return _LEVEL.sub(" ", c).strip() + "|" + _LEVEL.sub(" ", t).strip()


AGGREGATORS = ("adzuna", "alert", "workable_search", "hn", "remoteok", "wwr", "usajobs", "yc")


def posted_time(r: dict) -> datetime:
    """When the job was posted; when the board doesn't say, when the radar first saw it."""
    d = None
    if r.get("posted"):
        try:
            d = datetime.fromisoformat(r["posted"])
        except ValueError:
            d = None
    d = d or r["_first_seen"]
    d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    u = d.astimezone(timezone.utc)
    if r.get("posted") and (u.hour, u.minute, u.second, u.microsecond) == (0, 0, 0, 0):
        # the board gives the date only (Workday and others): keep it on that date in US time
        d = u.replace(hour=17)
        r["_date_only"] = True
    return d


def collapse(rows: list[dict]) -> tuple[list[dict], int]:
    """Merge rows that are the same job (one company, one title) listed in several places or on several
    sources. The company's own board wins over an aggregator; the newest posting time is kept; the other
    places are added to the location. Returns the rows and how many were merged away."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(dup_key(r), []).append(r)
    out = []
    for g in groups.values():
        g.sort(key=lambda r: (str(r.get("ats", "")).startswith(AGGREGATORS), -posted_time(r).timestamp()))
        keep = dict(g[0])
        keep["_t"] = max(posted_time(r) for r in g)
        places = []
        for r in g:
            loc = (r.get("location") or "").strip()
            if loc and loc not in places:
                places.append(loc)
        if len(places) > 1:
            keep["location"] = places[0] + f" +{len(places) - 1} more"
        keep["_dups"] = len(g) - 1
        out.append(keep)
    return out, sum(r["_dups"] for r in out)


def _day_table(rows: list[dict], tz: ZoneInfo) -> list[str]:
    out = ["| Posted | Role | Company | Location | Score | Notes |", "|---|---|---|---|---:|---|"]
    for r in rows:
        title = _cell(r["title"], 70).replace("[", "(").replace("]", ")")
        loc = _cell(r["location"], 40) + (f" ({r['workplace']})" if r.get("workplace") else "")
        t = "date only" if r.get("_date_only") else \
            r["_t"].astimezone(tz).strftime("%-I:%M %p") + ("" if r.get("posted") else " (seen)")
        out.append(f"| {t} | [{title}]({r['url']}) | {_cell(r['company'], 30)} | {loc} | {r['score']} | "
                   f"{_cell(r.get('flags', ''), 70)} |")
    return out


def build_newest_report(tz_name: str, boards_total: int, now: datetime | None = None, view: dict | None = None,
                        closed: set[str] = frozenset(), extra_lines: list[str] = ()) -> str:
    """Every match posted in the last `days` days, newest first, one section per day (local time)."""
    tz = ZoneInfo(tz_name)
    now = now or datetime.now(timezone.utc)
    v = view or {}
    days = int(v.get("days", 7))
    start = now - timedelta(days=days)
    from .match import classify_location
    rows = [r for r in read_matches(since=start - timedelta(days=1)) if r["uid"] not in closed
            and classify_location(r.get("location") or "", []) != "non_us"]   # saved before a location fix
    hide = int(v.get("hide_years", 0))
    for r in rows:   # rows saved before the years rule existed
        m = re.search(r"(\d+)\+ yrs exp", r.get("flags") or "")
        if hide and m and int(m.group(1)) >= hide and r["status"] in ("alert", "quiet"):
            r["status"] = "blocked"
            r["flags"] = f"asks for {m.group(1)}+ years of experience; " + r["flags"]
    visible = [r for r in rows if r["status"] in ("alert", "quiet") and posted_time(r) >= start]
    visible, merged = collapse(visible)
    visible.sort(key=lambda r: r["_t"], reverse=True)
    blocked = [r for r in rows if r["status"] == "blocked" and posted_time(r) >= start]
    for r in read_rejected(since=start):
        r["flags"] = r.get("reject_reason") or r.get("flags", "")
        blocked.append(r)
    blocked, _ = collapse(blocked)
    blocked.sort(key=lambda r: r["_t"], reverse=True)

    lines = [
        f"# {v.get('label', 'DevOps / SRE / Cloud / Platform jobs in the US')}: posted in the last {days} days",
        "",
        f"**{len(visible)} jobs**, newest first. Updated "
        f"{now.astimezone(tz).strftime('%a %b %-d, %-I:%M %p %Z')} from {boards_total:,} company job boards"
        + (" and other sources" if any(str(r.get("ats", "")).startswith(AGGREGATORS) for r in rows) else "") + ".",
        "",
        v.get("note", "US jobs only. Jobs asking for 5+ years, needing a clearance or US citizenship, or with an "
                      "adjacent title (sysadmin, network, systems) but little DevOps/cloud work are left out (listed at the end). Senior titles that state no years are kept and marked. "
                      "\"(seen)\" means the board gives no posting time, so the time the radar first saw it is shown; "
                      "\"date only\" means the board gives the day but not the time."),
        "",
    ]
    if merged:
        lines += [f"{merged} repeat listings (same company and title in another place or on another source) "
                  f"are merged into one row.", ""]
    lines += list(extra_lines)
    by_day: dict[str, list[dict]] = {}
    for r in visible:
        by_day.setdefault(r["_t"].astimezone(tz).strftime("%A, %b %-d"), []).append(r)
    if by_day:
        lines += ["Jump to: " + " · ".join(
            f"[{d.split(',')[0][:3]} {d.split(' ')[-1]} ({len(rs)})](#{_anchor(d, len(rs))})"
            for d, rs in by_day.items()), ""]
    for d, rs in by_day.items():
        lines += [f"## {d} ({len(rs)})", ""] + _day_table(rs, tz) + [""]
    if not by_day:
        lines += [f"No matches posted in the last {days} days.", ""]
    lines += [f"## Left out: 5+ years, clearance/citizenship, or adjacent titles with little DevOps work ({len(blocked)})", ""]
    if blocked:
        lines += ["<details><summary>Show</summary>", ""] + _day_table(blocked[:int(v.get("hidden_rows", 400))], tz) \
                 + ["", "</details>"]
    else:
        lines.append("None.")
    srcs = {str(r.get("ats", "")) for r in rows}
    credits = []
    if "adzuna" in srcs:
        credits.append("[Jobs by Adzuna](https://www.adzuna.com)")
    if "remoteok" in srcs:
        credits.append("Remote jobs from [Remote OK](https://remoteok.com)")
    if "wwr" in srcs:
        credits.append("Remote jobs from [We Work Remotely](https://weworkremotely.com)")
    if "usajobs" in srcs:
        credits.append("Federal jobs from [USAJOBS](https://www.usajobs.gov)")
    if "hn" in srcs:
        credits.append("Posts from Hacker News \"Who is hiring?\"")
    if credits:
        lines += ["", "Sources: " + " · ".join(credits) + "."]
    lines.append("")
    return "\n".join(lines)


def _anchor(day: str, n: int) -> str:
    """GitHub's heading anchor for "## Thursday, Nov 12 (45)"."""
    return re.sub(r"[^a-z0-9 -]", "", f"{day} ({n})".lower()).replace(" ", "-")


def write_report(text: str) -> None:
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(text)
