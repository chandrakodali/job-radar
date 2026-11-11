"""Turn the roles read off no-job-board careers pages into one quick-apply list.

Called by watch.py after it has fetched every careers page:
  1. keep titles the radar's rules call cloud / DevOps / SRE / platform / infrastructure (config.yaml)
  2. open each role's own page (when it has one) for the description, location and apply address
  3. score with the radar's assess(): years of experience, visa sponsorship, clearance, location
  4. write careers_scan/ROLES.md and data/roles.csv, and report which roles are new
"""
from __future__ import annotations

import csv
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .fetch import Fetcher
from .detect import match_vendor
from .roles import assess_role, infra_category, load_rules, mailto, read_job_page, role_key
from .urls import registrable_domain

log = logging.getLogger("careers_scan.apply_list")
MAX_DETAIL_PAGES = 400
FIELDS = ["score", "status", "title", "company", "location", "apply", "apply_kind", "job_url", "careers_url",
          "flags", "category", "domain", "first_seen", "last_seen", "yc_batch", "stage", "key"]


def _company_info(root: str) -> dict[str, dict]:
    path = os.path.join(root, "data", "classified.csv")
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8") as fh:
        return {r["domain"]: r for r in csv.DictReader(fh)}


def _load_state(path: str) -> tuple[dict, bool]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return (data if isinstance(data, dict) else {}), True
    except (OSError, ValueError):
        return {}, False


def build(results: list[dict], fetcher: Fetcher, root: str, now: datetime | None = None,
          config_path: str = "config.yaml") -> tuple[list[dict], list[dict], bool]:
    """Returns (current role rows, newly seen rows, whether earlier state existed)."""
    now = now or datetime.now(timezone.utc)
    rules, _cfg = load_rules(config_path)
    companies = _company_info(root)

    # 1. title filter
    picked = []
    for res in results:
        for r in res.get("roles") or []:
            cat = infra_category(r.title, rules)
            if cat:
                picked.append((res, r, cat))

    # 2. each role's own page
    detail_targets = {r.url for res, r, _ in picked if r.url and r.url != r.page_url}
    details: dict[str, dict] = {}
    targets = sorted(detail_targets)[:MAX_DETAIL_PAGES]

    def one(u):
        pg = fetcher.page(u)
        if pg.error or pg.status >= 400:
            return u, None
        try:
            return u, read_job_page(pg.text, pg.url, {registrable_domain(u)})
        except Exception:  # noqa: BLE001
            return u, None
    if targets:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for u, d in pool.map(one, targets):
                if d and not d["blocked"]:
                    details[u] = d

    # 3. score and route
    state_path = os.path.join(root, "data", "roles_state.json")
    state, had_state = _load_state(state_path)
    rows, seen_keys = [], set()
    for res, r, cat in picked:
        domain = res["domain"]
        key = role_key(domain, r.title)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        info = companies.get(domain, {})
        company = info.get("name") or res.get("name") or domain
        d = details.get(r.url) or {}
        location = r.location or d.get("location", "")
        a = assess_role(rules, company, domain, r.title, cat, location, d.get("description", ""), r.url, now=now)
        emails = list(dict.fromkeys(r.emails + d.get("emails", []) +
                                    [e.strip() for e in (info.get("apply_emails") or "").split(";") if e.strip()]))
        form = r.apply_url or d.get("apply_url", "") or info.get("form_url", "") or info.get("doc_url", "")
        if emails:
            apply, kind = emails[0], "email"
        elif form:
            v = match_vendor(form)
            apply, kind = form, (v.kind if v and v.kind in ("form", "doc") else "link")
        else:
            apply, kind = r.url or res.get("final_url") or res["url"], "page"
        if a.location_class == "non_us":
            status = "outside_us"
        else:
            status = a.status  # alert | quiet | blocked | old
        first = state.get(key, {}).get("first_seen") or now.isoformat(timespec="minutes")
        rows.append({
            "score": a.score, "status": status, "title": r.title, "company": company, "location": location,
            "apply": apply, "apply_kind": kind, "job_url": r.url, "careers_url": r.page_url,
            "flags": "; ".join(a.flags), "category": cat, "domain": domain, "first_seen": first,
            "last_seen": now.isoformat(timespec="minutes"), "yc_batch": info.get("yc_batch", ""),
            "stage": info.get("stage", ""), "key": key,
        })
    new = [x for x in rows if x["key"] not in state]
    rows.sort(key=lambda x: (x["status"] not in ("alert", "quiet"), -int(x["score"]), x["company"].lower()))
    return rows, new, had_state


def save(rows: list[dict], root: str, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    data = os.path.join(root, "data")
    os.makedirs(data, exist_ok=True)
    state_path = os.path.join(data, "roles_state.json")
    state, _ = _load_state(state_path)
    for x in rows:
        st = state.setdefault(x["key"], {})
        st.setdefault("first_seen", x["first_seen"])
        st["last_seen"] = x["last_seen"]
    # forget roles not seen for 60 days (a role that comes back after that counts as new)
    cutoff = now.timestamp() - 60 * 86400
    for k in list(state):
        try:
            if datetime.fromisoformat(state[k]["last_seen"]).timestamp() < cutoff:
                del state[k]
        except (KeyError, ValueError, TypeError):
            del state[k]
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    tmp = os.path.join(data, "roles.csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, os.path.join(data, "roles.csv"))
    with open(os.path.join(root, "ROLES.md"), "w", encoding="utf-8") as fh:
        fh.write(markdown(rows, now))


def _md(s, n=60) -> str:
    s = " ".join(str(s or "").split()).replace("|", "/")
    return s if len(s) <= n else s[: n - 1] + "…"


def _apply_cell(x: dict) -> str:
    if x["apply_kind"] == "email":
        return f"[✉ {_md(x['apply'], 40)}]({mailto(x['apply'], x['title'], x['company'])})"
    if x["apply_kind"] == "form":
        return f"[Apply form]({x['apply']})"
    if x["apply_kind"] == "doc":
        return f"[Job post]({x['apply']})"
    if x["apply_kind"] == "link":
        return f"[Apply]({x['apply']})"
    return f"[Careers page]({x['apply']})"


def _table(rows: list[dict], now: datetime) -> list[str]:
    out = ["| Score | Role | Company | Location | Apply | Since | Notes |", "|---:|---|---|---|---|---|---|"]
    for x in rows:
        try:
            since = datetime.fromisoformat(x["first_seen"]).strftime("%b %d")
        except ValueError:
            since = ""
        fresh = (now - datetime.fromisoformat(x["first_seen"])).days < 2 if since else False
        mark = " 🆕" if fresh else ""
        out.append(f"| {x['score']} | [{_md(x['title'], 55)}]({x['job_url']}){mark} | {_md(x['company'], 30)} | "
                   f"{_md(x['location'] or '?', 30)} | {_apply_cell(x)} | {since} | {_md(x['flags'], 60)} |")
    return out


def markdown(rows: list[dict], now: datetime) -> str:
    main = [x for x in rows if x["status"] in ("alert", "quiet")]
    abroad = [x for x in rows if x["status"] == "outside_us"]
    blocked = [x for x in rows if x["status"] == "blocked"]
    lines = [
        "# Cloud, DevOps, SRE, platform and infrastructure roles (no job board)",
        "",
        f"Updated {now.strftime('%Y-%m-%d %H:%M')} UTC. **{len(main)} open roles** at "
        f"{len({x['domain'] for x in main})} companies that post jobs only on their own website, so job sites "
        "and the radar can't see them. Roles at companies on Greenhouse, Lever, Ashby and other tracking systems "
        "from the same company lists appear in the radar's [REPORT.md](../REPORT.md).",
        "",
        "**Apply:** the ✉ link opens an email to the company's hiring address with the subject filled in; attach "
        "your résumé. Scores and notes use the radar's rules (config.yaml): higher is a better early-career fit; "
        "\"location unclear\" means the page didn't say, so check it. 🆕 = first seen in the last 2 days.",
        "",
        f"## US or location not stated ({len(main)})",
        "",
    ]
    lines += _table(main, now) if main else ["None right now."]
    lines += ["", f"## Outside the US ({len(abroad)})", ""]
    lines += _table(abroad, now) if abroad else ["None right now."]
    if blocked:
        lines += ["", f"## Need a clearance or US citizenship ({len(blocked)})", ""] + _table(blocked, now)
    lines += ["", "Full data: [data/roles.csv](data/roles.csv).", ""]
    return "\n".join(lines)
