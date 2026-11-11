"""Profiles: more than one job search run from the same repository.

The default profile is the DevOps radar (config.yaml, data/, REPORT.md, docs/index.html). A named
profile keeps everything of its own next to it:

    profiles/<name>/config.yaml      rules, scoring, search keywords, wording
    profiles/<name>/companies.csv    its own employer boards, scanned in depth every run
    data/<name>/                     state, match history, rejected jobs
    REPORT_<NAME>.md                 its report
    docs/<name>/index.html           its page

Run one with:  python -m radar.main --profile <name>
"""
from __future__ import annotations

import os
import re

from . import dashboard, report, store

PROFILES_DIR = os.path.join(store.ROOT, "profiles")


def paths(name: str) -> dict:
    base = os.path.join(PROFILES_DIR, name)
    data = os.path.join(store.ROOT, "data", name)
    docs = os.path.join(store.ROOT, "docs", name)
    return {
        "config": os.path.join(base, "config.yaml"),
        "focus": os.path.join(base, "companies.csv"),
        "data": data,
        "state": os.path.join(data, "state.json"),
        "matches": os.path.join(data, "matches.csv"),
        "rejected": os.path.join(data, "rejected.csv"),
        "report": os.path.join(store.ROOT, f"REPORT_{name.upper()}.md"),
        "docs": docs,
        "page": os.path.join(docs, "index.html"),
    }


def activate(name: str) -> dict:
    """Point the radar's files at a profile. Returns its paths."""
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", name or ""):
        raise SystemExit(f"Bad profile name: {name!r}")
    p = paths(name)
    if not os.path.exists(p["config"]):
        raise SystemExit(f"No such profile: {p['config']} is missing")
    store.DATA = p["data"]
    store.STATE_PATH = p["state"]
    store.MATCHES_PATH = p["matches"]
    store.REJECTED_PATH = p["rejected"]
    report.REPORT_PATH = p["report"]
    dashboard.DOCS = p["docs"]
    dashboard.PAGE_PATH = p["page"]
    return p
