"""robots.txt rules for the page and feed readers (RFC 9309).

The JSON APIs the radar uses are public developer endpoints. Pages and feeds on career
sites are checked against the site's robots.txt before they are fetched, and rules are
cached for a week per host so each site's robots.txt is read at most once a week.
"""
from __future__ import annotations

import re
import threading
from datetime import date, timedelta
from urllib.parse import urlsplit

from .http import FetchError, get_text

AGENT = "job-radar"
TTL_DAYS = 7
MAX_RULES = 400


def parse(text: str, agent: str = AGENT) -> list[list]:
    """Return [[allow: bool, pattern: str], ...] for the group that applies to `agent`."""
    groups: list[tuple[list[str], list[list]]] = []
    agents: list[str] = []
    rules: list[list] = []
    in_rules = False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key, val = key.strip().lower(), val.strip()
        if key == "user-agent":
            if in_rules:
                groups.append((agents, rules))
                agents, rules, in_rules = [], [], False
            agents.append(val.lower())
        elif key in ("allow", "disallow"):
            in_rules = True
            if agents and val:
                rules.append([key == "allow", val])
    if agents:
        groups.append((agents, rules))
    mine = [r for a, r in groups if any(x != "*" and x in agent.lower() for x in a)]
    if not mine:
        mine = [r for a, r in groups if "*" in a]
    out: list[list] = []
    for r in mine:
        out.extend(r)
    return out[:MAX_RULES]


def _pattern_re(pat: str) -> re.Pattern:
    anchored = pat.endswith("$")
    body = pat[:-1] if anchored else pat
    rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile(rx + ("$" if anchored else ""))


def is_allowed(rules: list[list], path: str) -> bool:
    best_len, best_allow = -1, True
    for allow, pat in rules:
        if _pattern_re(pat).match(path):
            n = len(pat)
            if n > best_len or (n == best_len and allow):
                best_len, best_allow = n, allow
    return best_allow


class Robots:
    """Per-run robots.txt checker backed by a persistent cache dict (stored in state)."""

    def __init__(self, cache: dict, today: str):
        self.cache = cache
        self.today = today
        self._lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._unreachable: set[str] = set()

    def _host_lock(self, host: str) -> threading.Lock:
        with self._lock:
            return self._host_locks.setdefault(host, threading.Lock())

    def _rules(self, scheme: str, host: str):
        rec = self.cache.get(host)
        fresh_after = (date.fromisoformat(self.today) - timedelta(days=TTL_DAYS)).isoformat()
        if rec and rec.get("t", "") > fresh_after:
            return rec.get("r")
        if host in self._unreachable:
            return "deny"
        with self._host_lock(host):
            rec = self.cache.get(host)
            if rec and rec.get("t", "") > fresh_after:
                return rec.get("r")
            try:
                text = get_text(f"{scheme or 'https'}://{host}/robots.txt", accept="text/plain,*/*;q=0.5")
                rules = parse(text)
            except FetchError as e:
                if e.status and 400 <= e.status < 500 and e.status != 429:
                    rules = []  # "unavailable": RFC 9309 lets crawlers proceed
                else:
                    self._unreachable.add(host)  # "unreachable": assume full disallow, retry next run
                    return "deny"
            with self._lock:
                self.cache[host] = {"t": self.today, "r": rules}
            return rules

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        host = parts.netloc.lower()
        if not host:
            return False
        rules = self._rules(parts.scheme, host)
        if rules == "deny":
            return False
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        return is_allowed(rules or [], path)

    def prune(self, keep_days: int = 30) -> None:
        cutoff = (date.fromisoformat(self.today) - timedelta(days=keep_days)).isoformat()
        for h in [h for h, r in self.cache.items() if r.get("t", "") < cutoff]:
            del self.cache[h]


# The run installs one checker here; fetchers call allowed(). Tests may replace it.
_current: Robots | None = None


def install(r: Robots | None) -> None:
    global _current
    _current = r


def allowed(url: str) -> bool:
    return True if _current is None else _current.allowed(url)


class Disallowed(FetchError):
    def __init__(self, url: str):
        super().__init__(f"robots.txt disallows {urlsplit(url).path[:40]}", 999)


def check(url: str) -> None:
    if not allowed(url):
        raise Disallowed(url)
