"""Reading and writing the radar's files: company lists, state and match history."""
from __future__ import annotations

import csv
import json
import os
import re
from datetime import date, datetime, timedelta, timezone

from .models import Board

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
STATE_PATH = os.path.join(DATA, "state.json")
MATCHES_PATH = os.path.join(DATA, "matches.csv")
COMPANIES_PATH = os.path.join(ROOT, "companies.csv")
CUSTOM_PATH = os.path.join(ROOT, "companies_custom.csv")

MATCH_FIELDS = ["first_seen", "posted", "score", "status", "category", "company", "title",
                "location", "workplace", "flags", "url", "ats", "uid"]

CAP_EXEMPT_RE = re.compile(r"universit|\bcollege\b|institute of technology|school of medicine|national lab", re.I)
NOT_CAP_EXEMPT_RE = re.compile(
    r"investment|foundation|obstetricians|physicians|surgeons|association|health network|"
    r"college board|university health network",
    re.I,
)
VALID_ATS = {"greenhouse", "lever", "ashby", "smartrecruiters", "workday", "oracle", "workable", "rippling",
             "bamboohr", "recruitee", "auto"}


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "y")


def load_boards() -> list[Board]:
    """companies.csv (auto-built) + companies_custom.csv (yours; wins on duplicates)."""
    boards: dict[str, Board] = {}
    for path, is_custom in ((COMPANIES_PATH, False), (CUSTOM_PATH, True)):
        if not os.path.exists(path):
            continue
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ats = (row.get("ats") or "").strip().lower()
                board = (row.get("board") or "").strip()
                if not board or board.startswith("#") or ats not in VALID_ATS:
                    continue
                company = (row.get("company") or "").strip() or board
                b = Board(
                    ats=ats, board=board, company=company,
                    priority=is_custom and _truthy(row.get("priority", "yes")),
                    cap_exempt=_truthy(row.get("cap_exempt")) or (bool(CAP_EXEMPT_RE.search(company))
                                                                and not NOT_CAP_EXEMPT_RE.search(company)),
                )
                boards[b.key.lower()] = b
    return list(boards.values())


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
    else:
        st = {}
    st.setdefault("version", 1)
    st.setdefault("bootstrapped", False)
    st.setdefault("seen", {})
    st.setdefault("fails", {})
    st.setdefault("resolved", {})
    st.setdefault("last_digest", "")
    st.setdefault("baselined", {})
    return st


def save_state(st: dict) -> None:
    os.makedirs(DATA, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, STATE_PATH)


def prune_seen(st: dict, keep_days: int = 120) -> int:
    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
    old = [k for k, v in st["seen"].items() if str(v) < cutoff]
    for k in old:
        del st["seen"][k]
    return len(old)


def append_matches(rows: list[dict]) -> None:
    if not rows:
        return
    os.makedirs(DATA, exist_ok=True)
    new_file = not os.path.exists(MATCHES_PATH)
    with open(MATCHES_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MATCH_FIELDS, extrasaction="ignore")
        if new_file:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def read_matches(since: datetime | None = None) -> list[dict]:
    if not os.path.exists(MATCHES_PATH):
        return []
    out = []
    with open(MATCHES_PATH, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                fs = datetime.fromisoformat(r["first_seen"])
            except (KeyError, ValueError):
                continue
            if since and fs < since:
                continue
            r["_first_seen"] = fs
            try:
                r["score"] = int(r.get("score") or 0)
            except ValueError:
                r["score"] = 0
            out.append(r)
    return out


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
