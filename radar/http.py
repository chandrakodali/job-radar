"""Shared HTTP session with retries, timeouts and a polite User-Agent."""
import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = "job-radar/1.0 (personal job search tool; +https://github.com/chandrakodali/job-radar)"
TIMEOUT = (10, 30)  # connect, read seconds

_local = threading.local()
MAX_RETRY_AFTER = 8  # seconds; some servers ask for an hour, which would stall the whole run


class _CappedRetry(Retry):
    def get_retry_after(self, response):
        v = super().get_retry_after(response)
        return None if v is None else min(v, MAX_RETRY_AFTER)


def session() -> requests.Session:
    """One session per thread (requests.Session is not guaranteed thread-safe)."""
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        retry = _CappedRetry(
            total=2,
            connect=2,
            read=1,
            backoff_factor=1.5,
            backoff_max=10,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        _local.session = s
    return s


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


MAX_TEXT_BYTES = 12 * 1024 * 1024
HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


def _send(method: str, url: str, **kw):
    try:
        return session().request(method, url, timeout=TIMEOUT, **kw)
    except requests.exceptions.RetryError as e:
        raise FetchError("rate limited (retries exhausted)", 429) from e
    except requests.RequestException as e:
        raise FetchError(f"network error: {type(e).__name__}") from e


def _json(r):
    if r.status_code != 200:
        raise FetchError(f"HTTP {r.status_code}", r.status_code)
    try:
        return r.json()
    except ValueError as e:
        raise FetchError("invalid JSON", r.status_code) from e


def get_json(url: str, params: dict | None = None, headers: dict | None = None):
    return _json(_send("GET", url, params=params, headers=headers))


def post_json(url: str, payload, headers: dict | None = None, params: dict | None = None):
    return _json(_send("POST", url, json=payload, headers=headers, params=params))


def get_text(url: str, params: dict | None = None, headers: dict | None = None,
             accept: str = HTML_ACCEPT, method: str = "GET", data=None) -> str:
    """GET (or POST) a page or feed as text (HTML, XML, RSS). Caps the size to stay memory-safe."""
    h = {"Accept": accept}
    if headers:
        h.update(headers)
    r = _send(method, url, params=params, headers=h, data=data, stream=True)
    try:
        if r.status_code != 200:
            raise FetchError(f"HTTP {r.status_code}", r.status_code)
        chunks, size = [], 0
        for chunk in r.iter_content(65536):
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_TEXT_BYTES:
                break
        raw = b"".join(chunks)
    except requests.RequestException as e:
        raise FetchError(f"network error: {type(e).__name__}") from e
    finally:
        r.close()
    enc = r.encoding or "utf-8"
    if enc.lower() in ("iso-8859-1", "latin-1") and b"charset" not in raw[:2048].lower():
        enc = "utf-8"  # requests' default when no charset is sent; real pages are almost always UTF-8
    return raw.decode(enc, errors="replace")
