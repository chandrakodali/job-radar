"""URL helpers: clean up company websites and compare sites by their registrable domain."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit, urlunsplit

import tldextract

# Offline: use the Public Suffix List snapshot bundled with tldextract (no network at import time).
# Private suffixes on, so "acme.github.io" and "acme.vercel.app" stay distinct sites.
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)

# A "website" that is a profile or page *on* one of these platforms (non-root path) is not the
# company's own site. The bare platform root is kept: it may be the platform company itself.
PROFILE_PLATFORMS = {
    "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com", "youtube.com",
    "github.com", "gitlab.com", "medium.com", "substack.com", "linktr.ee", "angel.co",
    "wellfound.com", "crunchbase.com", "ycombinator.com", "producthunt.com", "tiktok.com",
    "discord.gg", "discord.com", "t.me", "notion.so", "bit.ly", "tinyurl.com", "calendly.com",
    "apps.apple.com", "play.google.com", "chrome.google.com", "docs.google.com", "sites.google.com",
}


def registrable_domain(host_or_url: str) -> str:
    """'https://WWW.Jobs.Acme.co.uk:443/x' -> 'acme.co.uk'. Returns '' when there is no usable host."""
    s = (host_or_url or "").strip()
    if not s:
        return ""
    host = urlsplit(s if "://" in s else "//" + s).hostname or ""
    host = host.strip(".").lower()
    if not host:
        return ""
    ext = _EXTRACT(host)
    if not ext.suffix or not ext.domain:
        return ""  # localhost, bare IPs, unknown TLDs
    return f"{ext.domain}.{ext.suffix}"


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").strip(".").lower()
    except ValueError:
        return ""


def normalize_website(raw: str | None) -> str | None:
    """Turn a messy website value into 'https://host/' or None if it can't be a company site.

    Handles: missing scheme, whitespace, trailing dots, ports, paths/queries (dropped), uppercase,
    'www' prefixes (kept: some sites only answer on www), IDN hosts, and non-web schemes.
    """
    s = (raw or "").strip().strip("\"'<>")
    # Several sites in one field ("https://a.io, https://b.com"): use the first.
    parts_ = [x for x in re.split(r"[\s,;|]+", s) if x]
    s = parts_[0] if parts_ else ""
    if not s:
        return None
    # Scheme typos seen in real data: "htps://", "htttps://", "http:/x", "https//x".
    s = re.sub(r"^h+t+p+(s?)\s*:?/*", lambda m: f"http{m.group(1)}://", s, flags=re.I) \
        if re.match(r"^h+t+p+s?\s*:?/", s, re.I) else s
    if s.startswith("//"):
        s = "https:" + s
    if re.match(r"^(mailto|tel|javascript|data|sms|file):", s, re.I):
        return None
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", s):
        s = "https://" + s
    try:
        parts = urlsplit(s)
        host = (parts.hostname or "").strip(".").lower()
        _ = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host or "@" in parts.netloc:
        return None
    if not registrable_domain(host):
        return None
    try:
        host.encode("idna")
    except UnicodeError:
        return None
    if is_profile_page(host, parts.path):
        return None
    return urlunsplit(("https", host, "/", "", ""))


def is_profile_page(host: str, path: str) -> bool:
    """True for e.g. linkedin.com/company/acme or apps.apple.com/app/x (not a company's own site)."""
    host = (host or "").lower()
    bare = host[4:] if host.startswith("www.") else host
    on_platform = bare in PROFILE_PLATFORMS or registrable_domain(host) in PROFILE_PLATFORMS
    first = (path or "").strip("/").split("/")[0].lower()
    if first in ("careers", "career", "jobs", "about", "about-us", "company-info"):
        return False  # the platform's own page, e.g. calendly.com/careers is Calendly itself
    has_path = bool(first)
    return on_platform and (has_path or bare in {"apps.apple.com", "play.google.com", "docs.google.com",
                                                 "sites.google.com", "chrome.google.com"})


def canon(url: str) -> str:
    """Key for de-duplicating URLs: no fragment, lower-case host, no trailing slash, no default port."""
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        path = re.sub(r"/+$", "", p.path or "") or "/"
        return urlunsplit((p.scheme.lower(), host, path, p.query, ""))
    except ValueError:
        return url


def absolute(base: str, href: str) -> str | None:
    """Resolve a link; drop non-web links (mailto, tel, javascript, data, fragments)."""
    h = (href or "").strip()
    if not h or h.startswith("#"):
        return None
    low = h.lower()
    if low.startswith(("mailto:", "tel:", "javascript:", "data:", "sms:", "whatsapp:", "skype:")):
        return None
    try:
        u = urljoin(base, h)
        p = urlsplit(u)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    return urlunsplit((p.scheme, p.netloc, p.path or "/", p.query, ""))


def same_site(a: str, b: str) -> bool:
    ra, rb = registrable_domain(a), registrable_domain(b)
    return bool(ra) and ra == rb
