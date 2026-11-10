"""Pure page analysis: what a careers page reveals about how a company takes applications.

Nothing here touches the network, so every rule is unit-tested in tests/test_detect.py.
"""
from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from .urls import absolute, host_of, registrable_domain

try:  # lxml is faster and more forgiving; html.parser is the stdlib fallback
    import lxml  # noqa: F401
    _PARSER = "lxml"
except ImportError:  # pragma: no cover
    _PARSER = "html.parser"

MAX_PARSE_CHARS = 1_500_000

# --------------------------------------------------------------------------- vendors

# Labels that mark a vendor's own marketing/help site rather than a customer's job board.
VENDOR_SITE_LABELS = {
    "www", "help", "support", "blog", "docs", "developers", "developer", "dev", "status", "community",
    "go", "learn", "partners", "partner", "marketplace", "get", "info", "pages", "academy",
    "university", "events", "press", "investors", "ir", "news", "resources", "trust", "security",
    "legal", "privacy", "shop", "store", "email", "mail", "cdn-cgi",
}


@dataclass(frozen=True)
class Vendor:
    name: str
    kind: str                 # "ats" (applicant tracking system), "board" (public job site), "form", "doc"
    apexes: tuple[str, ...]   # host suffixes
    path: str | None = None   # regex the URL path must match (search, case-insensitive)
    tenant: bool = True       # True: needs a customer subdomain (x.vendor.com), not the bare/www site


VENDORS: tuple[Vendor, ...] = (
    # ---- applicant tracking systems
    Vendor("greenhouse", "ats", ("greenhouse.io",)),
    Vendor("greenhouse", "ats", ("grnh.se",), tenant=False),
    Vendor("lever", "ats", ("lever.co",)),
    Vendor("ashby", "ats", ("ashbyhq.com",)),
    Vendor("workable", "ats", ("workable.com",)),
    Vendor("bamboohr", "ats", ("bamboohr.com",), path=r"/(careers|jobs|hiring)"),
    Vendor("smartrecruiters", "ats", ("smartrecruiters.com",)),
    Vendor("workday", "ats", ("myworkdayjobs.com", "myworkdaysite.com", "workdayjobs.com")),
    Vendor("icims", "ats", ("icims.com",)),
    Vendor("jobvite", "ats", ("jobvite.com",)),
    Vendor("breezy", "ats", ("breezy.hr",)),
    Vendor("jazzhr", "ats", ("applytojob.com", "jazzhr.com")),
    Vendor("recruitee", "ats", ("recruitee.com",)),
    Vendor("teamtailor", "ats", ("teamtailor.com",)),
    Vendor("teamtailor", "ats", ("teamtailor-cdn.com",), tenant=False),
    Vendor("personio", "ats", ("personio.de", "personio.com")),
    Vendor("rippling", "ats", ("ats.rippling.com",), tenant=False),
    Vendor("pinpoint", "ats", ("pinpointhq.com",)),
    Vendor("homerun", "ats", ("homerun.co",)),
    Vendor("gem", "ats", ("jobs.gem.com",), path=r"/.", tenant=False),
    Vendor("dover", "ats", ("app.dover.com", "app.dover.io"), path=r"^/(apply|jobs)/", tenant=False),
    Vendor("dover", "ats", ("jobs.dover.com", "jobs.dover.io"), path=r"/.", tenant=False),
    Vendor("polymer", "ats", ("jobs.polymer.co",), path=r"/.", tenant=False),
    Vendor("freshteam", "ats", ("freshteam.com",)),
    Vendor("zoho_recruit", "ats", ("zohorecruit.com", "zohorecruit.eu", "zohorecruit.in")),
    Vendor("comeet", "ats", ("comeet.com", "comeet.co"), path=r"/jobs/", tenant=False),
    Vendor("join", "ats", ("join.com",), path=r"^/companies/", tenant=False),
    Vendor("factorial", "ats", ("factorialhr.com",)),
    Vendor("paylocity", "ats", ("recruiting.paylocity.com",), tenant=False),
    Vendor("ukg", "ats", ("ultipro.com", "rec.pro.ukg.net")),
    Vendor("adp", "ats", ("workforcenow.adp.com", "recruiting.adp.com", "myjobs.adp.com"), tenant=False),
    Vendor("successfactors", "ats", ("successfactors.com", "successfactors.eu", "sapsf.com", "sapsf.eu")),
    Vendor("taleo", "ats", ("taleo.net",)),
    Vendor("oracle", "ats", ("oraclecloud.com",), path=r"candidateexperience|hcmui"),
    Vendor("avature", "ats", ("avature.net",)),
    Vendor("phenom", "ats", ("phenompeople.com",)),
    Vendor("eightfold", "ats", ("eightfold.ai",), path=r"/careers"),
    Vendor("jobscore", "ats", ("jobscore.com",), path=r"/(careers|jobs)/", tenant=False),
    Vendor("trakstar", "ats", ("hire.trakstar.com", "recruiterbox.com")),
    Vendor("hiringthing", "ats", ("hiringthing.com",)),
    Vendor("applicantpro", "ats", ("applicantpro.com",)),
    Vendor("careerplug", "ats", ("careerplug.com",)),
    Vendor("paycom", "ats", ("paycomonline.net",)),
    Vendor("isolved", "ats", ("isolvedhire.com",)),
    Vendor("manatal", "ats", ("careers-page.com",)),
    Vendor("recruitcrm", "ats", ("recruitcrm.io",)),
    Vendor("softgarden", "ats", ("softgarden.io", "softgarden.de")),
    Vendor("dvinci", "ats", ("dvinci-hr.com",)),
    Vendor("talentlyft", "ats", ("talentlyft.com",)),
    Vendor("keka", "ats", ("keka.com",), path=r"/careers"),
    Vendor("darwinbox", "ats", ("darwinbox.in", "darwinbox.com"), path=r"/(ms/candidate|careers|jobs)"),
    Vendor("bullhorn", "ats", ("bullhornstaffing.com",)),
    Vendor("ceipal", "ats", ("ceipal.com",)),
    Vendor("jobdiva", "ats", ("jobdiva.com",)),
    Vendor("crelate", "ats", ("crelate.com",)),
    Vendor("loxo", "ats", ("loxo.co",), path=r"/(jobs|careers)"),
    Vendor("workstream", "ats", ("workstream.us", "workstream.is")),
    Vendor("fountain", "ats", ("fountain.com",), path=r"/(apply|jobs)"),
    Vendor("hirehive", "ats", ("hirehive.com",)),
    Vendor("hireology", "ats", ("hireology.com",), path=r"/(careers|jobs)"),
    Vendor("clearcompany", "ats", ("clearcompany.com",), path=r"/careers"),
    Vendor("hrmdirect", "ats", ("hrmdirect.com",)),
    Vendor("gusto", "ats", ("jobs.gusto.com",), path=r"/.", tenant=False),
    Vendor("paradox", "ats", ("paradox.ai",), path=r"/(jobs|careers|apply)"),
    # ---- public job sites (postings live on someone else's board)
    Vendor("linkedin_jobs", "board", ("linkedin.com",), path=r"^/(company/[^/]+/jobs|jobs/)", tenant=False),
    Vendor("wellfound", "board", ("wellfound.com", "angel.co"), path=r"/(jobs|l/)", tenant=False),
    Vendor("yc_jobs", "board", ("ycombinator.com",), path=r"^/companies/[^/]+/jobs", tenant=False),
    Vendor("yc_jobs", "board", ("workatastartup.com",), path=r"^/(companies|jobs)/", tenant=False),
    Vendor("indeed", "board", ("indeed.com",), path=r"^/(cmp/[^/]+/jobs|viewjob|job/|jobs)", tenant=False),
    Vendor("glassdoor", "board", ("glassdoor.com",), path=r"/(jobs|job-listing)/", tenant=False),
    Vendor("builtin", "board", ("builtin.com", "builtinnyc.com", "builtinsf.com", "builtinla.com",
                                "builtinboston.com", "builtinchicago.org", "builtinaustin.com",
                                "builtinseattle.com", "builtincolorado.com"), path=r"^/(job|jobs|company/[^/]+/jobs)",
           tenant=False),
    Vendor("welcometothejungle", "board", ("welcometothejungle.com", "otta.com"), path=r"/(jobs|companies/[^/]+/jobs)",
           tenant=False),
    Vendor("weworkremotely", "board", ("weworkremotely.com",), path=r"^/(company|remote-jobs)/", tenant=False),
    Vendor("remoteok", "board", ("remoteok.com", "remoteok.io"), path=r"^/(remote-jobs|l/)", tenant=False),
    # ---- application forms (no tracking system behind them)
    Vendor("google_forms", "form", ("docs.google.com",), path=r"^/forms/", tenant=False),
    Vendor("google_forms", "form", ("forms.gle",), path=r"/.", tenant=False),
    Vendor("typeform", "form", ("typeform.com",), path=r"^/to/"),
    Vendor("typeform", "form", ("form.typeform.com",), path=r"^/to/", tenant=False),
    Vendor("tally", "form", ("tally.so",), path=r"^/(r|forms)/", tenant=False),
    Vendor("jotform", "form", ("jotform.com", "jotform.co", "jotform.eu"), path=r"/(form/)?\d{6,}", tenant=False),
    Vendor("airtable_form", "form", ("airtable.com",), path=r"/(app[a-z0-9]+/)?(shr|pag)[a-z0-9]+", tenant=False),
    Vendor("hubspot_form", "form", ("share.hsforms.com", "share-eu1.hsforms.com"), path=r"/.", tenant=False),
    Vendor("fillout", "form", ("fillout.com",), path=r"/t/|/forms?/", tenant=True),
    Vendor("microsoft_forms", "form", ("forms.office.com", "forms.microsoft.com"), path=r"/.", tenant=False),
    Vendor("formstack", "form", ("formstack.com",), path=r"/forms/"),
    Vendor("paperform", "form", ("paperform.co",)),
    # ---- job descriptions hosted as plain documents
    Vendor("notion", "doc", ("notion.site",), tenant=True),
    Vendor("notion", "doc", ("notion.so",), path=r"/.", tenant=False),
    Vendor("google_docs", "doc", ("docs.google.com",), path=r"^/document/", tenant=False),
    Vendor("coda", "doc", ("coda.io",), path=r"^/(@|d/)", tenant=False),
    Vendor("craft", "doc", ("craft.do",), path=r"^/s/", tenant=False),
)


def match_vendor(url: str) -> Vendor | None:
    """Which job-board vendor (if any) a URL belongs to. Vendor marketing sites don't count."""
    host = host_of(url)
    if not host:
        return None
    try:
        path = unquote(urlsplit(url).path or "/")
    except ValueError:
        return None
    for v in VENDORS:
        for apex in v.apexes:
            if host != apex and not host.endswith("." + apex):
                continue
            sub = host[: -len(apex)].rstrip(".")
            if v.tenant:
                labels = [x for x in sub.split(".") if x]
                if not labels or labels[0] in VENDOR_SITE_LABELS:
                    break  # bare vendor domain or its marketing/help site
            if v.path and not re.search(v.path, path, re.I):
                break
            return v
    return None


# --------------------------------------------------------------------------- text patterns

CAREER_WORD = (
    r"careers?|jobs?|join(ing)?[ -]?(us|the[ -]team|our[ -]team)|we'?re[ -]hiring|now[ -]hiring|hiring|"
    r"work[ -](with|for|at)[ -]us|open[ -](positions|roles|jobs)|vacanc(y|ies)|job[ -]openings|"
    r"opportunities|karriere|stellen(angebote|markt)?|jobb?|emplois?|carri[eè]res?|empleos?|trabaja[ -]con[ -]nosotros|"
    r"[uú]nete|lavora[ -]con[ -]noi|carriere|vacatures|werken[ -]bij|vagas|trabalhe[ -]conosco|rekrytointi|"
    r"praca|kariera|вакансии|карьера|採用|求人|招聘"
)
CAREER_TEXT_RE = re.compile(rf"(^|\b)({CAREER_WORD})(\b|$)", re.I)
CAREER_PATH_SEGMENTS = {
    "careers", "career", "jobs", "job", "join", "join-us", "joinus", "join-the-team", "join-our-team",
    "work-with-us", "work-for-us", "workwithus", "hiring", "we-are-hiring", "were-hiring", "open-positions",
    "open-roles", "openings", "vacancies", "opportunities", "karriere", "stellenangebote", "jobb",
    "emplois", "emploi", "carrieres", "carreras", "empleos", "empleo", "lavora-con-noi", "carriere",
    "vacatures", "werken-bij", "vagas", "trabalhe-conosco", "kariera", "praca", "team#jobs",
}
# Paths that mention jobs but are about something else (docs for "cron jobs", blog posts...).
NON_CAREER_PATH_RE = re.compile(
    r"/(docs?|documentation|api|reference|help|support|kb|knowledge-?base|learn|guides?|tutorials?|blog|news|"
    r"articles?|posts?|press|stories|story|case-stud(y|ies)|customers|changelog|releases?|integrations?|"
    r"templates?|glossary|podcast|webinars?|events?|resources|features?|product|pricing|legal|privacy|terms)(/|$)",
    re.I,
)

APPLY_BY_EMAIL_RE = re.compile(
    r"(send|e-?mail|submit|forward|mail)\b[^.\n]{0,60}\b(cv|c\.v\.|resume|résumé|curriculum|application|portfolio|"
    r"cover letter|linkedin profile|github profile)"
    r"|(to apply|apply|applications?)\b[^.\n]{0,60}\b(e-?mail|write to|reach out|contact us at|send)"
    r"|application documents"
    r"|(cv|resume|résumé)\b[^.\n]{0,40}\bto\b[^.\n]{0,40}@"
    r"|envoy(ez|er) (votre|ton) cv|bewerbung|envía (tu|su) cv|invia (il tuo|un) cv|stuur (je|uw) cv|"
    r"envie (seu|o seu) curr[ií]culo",
    re.I,
)
APPLY_CONTEXT_RE = re.compile(
    r"\b(apply|applying|application|applicants?|resumes?|résumés?|cvs?|c\.v\.|curriculum|cover letter|"
    r"portfolio|candidates?|positions?|roles?|hiring|join (us|our team|the team)|opening|openings|vacanc|"
    r"job|jobs|career|careers|bewerb|candidat|postul)\b",
    re.I,
)
NO_OPENINGS_RE = re.compile(
    r"(no|not any|zero) (current |open |available )?(job )?(openings|positions|vacancies|roles|jobs|opportunities)"
    r"( available| open| at (this|the) (time|moment))?"
    r"|(are|is) not (currently |actively )?hiring|not hiring (right now|at the moment|currently)"
    r"|(don'?t|do not) have any (open|current|available) (positions|roles|openings|vacancies|jobs)"
    r"|currently (no|not any) (open )?(positions|openings|vacancies|roles)"
    r"|no (open )?(positions|roles|openings) (right now|at the moment|currently)",
    re.I,
)
ROLE_RE = re.compile(
    r"\b(engineer|engineering manager|developer|devops|sre|site reliability|architect|scientist|analyst|designer|"
    r"product manager|program manager|project manager|manager|director|head of|lead|intern(ship)?|specialist|"
    r"associate|consultant|administrator|admin|technician|coordinator|representative|recruiter|writer|"
    r"researcher|officer|executive|strategist|marketer|account manager|account executive|operator|"
    r"programmer|tester|qa)\b",
    re.I,
)
ENGINEERING_RE = re.compile(
    r"\b(software|engineer(ing)?|developer|devops|sre|site reliability|platform|infrastructure|cloud|"
    r"back-?end|front-?end|full[- ]?stack|data engineer|machine learning|ml engineer|security engineer|"
    r"systems? (administrator|engineer)|kubernetes|linux|python|golang|rust|typescript|java)\b",
    re.I,
)
SOFT_404_RE = re.compile(
    r"\b(404|page not found|not found|page (doesn'?t|does not|could not|cannot) (exist|be found)|"
    r"no longer (exists|available)|oops)\b",
    re.I,
)
PARKED_RE = re.compile(
    r"(this )?domain (name )?(is|may be|might be) for sale|buy this domain|this domain is parked|"
    r"parked (free|domain|by)|domain (has )?expired|hugedomains|afternic|sedo\.com|dan\.com|"
    r"this (web ?site|site) is (for sale|parked)|purchase this domain|make an offer on this domain|"
    r"domain is available|related searches",
    re.I,
)
PARKING_HOSTS = {
    "hugedomains.com", "dan.com", "sedo.com", "afternic.com", "godaddy.com", "bodis.com",
    "parkingcrew.net", "above.com", "undeveloped.com", "squadhelp.com", "atom.com", "brandbucket.com",
    "domainmarket.com", "sav.com", "porkbun.com", "namecheap.com", "domainlore.com", "efty.com",
}
BLOCK_RE = re.compile(
    r"just a moment\.\.\.|attention required!? \| cloudflare|cf-chl|cf_chl|checking your browser|"
    r"enable javascript and cookies to continue|ddos protection by|incapsula|_incapsula_resource|"
    r"pardon our interruption|px-captcha|perimeterx|captcha-delivery|datadome|access denied|"
    r"request unsuccessful|are you a robot|verify you are (a )?human|bot detection",
    re.I,
)
JS_SHELL_RE = re.compile(
    r"<div[^>]+id=[\"'](root|app|__next|__nuxt|svelte|main-app|react-root)[\"']|__NEXT_DATA__|"
    r"window\.__INITIAL_STATE__|ng-version=|data-reactroot|<noscript>[^<]{0,200}(enable|need) javascript",
    re.I,
)

# --------------------------------------------------------------------------- emails

EMAIL_RE = re.compile(
    r"(?<![\w.+%-])([a-z0-9](?:[a-z0-9._%+-]{0,62}[a-z0-9_-])?)@"
    r"((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(?![\w-])",
    re.I,
)
# Obfuscations people use against scrapers: "jobs [at] acme [dot] com", "jobs(at)acme.com", "jobs @ acme . com"
_OBF_AT = re.compile(r"\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|<\s*at\s*>|\s@\s|\s+\[@\]\s+)\s*", re.I)
_OBF_DOT = re.compile(r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|<\s*dot\s*>)\s*", re.I)
FILE_TLDS = {
    "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp", "tif", "tiff", "avif", "heic", "css", "js",
    "mjs", "map", "json", "xml", "mp4", "webm", "mov", "mp3", "wav", "woff", "woff2", "ttf", "otf", "eot",
    "pdf", "zip", "gz", "txt", "html", "htm", "php", "asp", "aspx", "jsp", "ts", "tsx", "jsx", "vue", "md",
}
JUNK_EMAIL_DOMAINS = {
    "example.com", "example.org", "example.net", "domain.com", "email.com", "yourdomain.com",
    "yourcompany.com", "mysite.com", "test.com", "sentry.io", "wixpress.com",
    "sentry-next.wixpress.com", "ingest.sentry.io", "mailinator.com", "address.com", "website.com",
    "users.noreply.github.com", "noreply.github.com", "u003e", "2x.png",
}
HIRING_LOCAL_RE = re.compile(
    r"(career|job|hiring|hire|recruit|talent|^hr$|^hr[._-]|[._-]hr$|people|^join|apply|application|"
    r"^cv$|resume|vacanc|staffing|employ|opportunit|work(with|for)us|karriere|bewerbung|empleo|emploi|"
    r"lavora|carriere|vagas|rh$)",
    re.I,
)
NEVER_HIRING_LOCAL_RE = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|privacy|gdpr|dpo|abuse|security|legal|press|media|pr|billing|"
    r"invoices?|accounts?|accounting|payments?|finance|accessibility|dmca|unsubscribe|webmaster|postmaster|"
    r"hostmaster|sales|partners?|partnerships|investors?|ir|marketing|newsletter|orders?|returns|"
    r"compliance|copyright|trademark|bugs?|vulnerability|disclosure|feedback|events|sponsor(ship)?s?)$",
    re.I,
)


def decode_cfemail(hexstr: str) -> str | None:
    """Cloudflare 'email protection' encodes addresses as hex XOR'd with the first byte."""
    try:
        data = bytes.fromhex(hexstr.strip())
    except ValueError:
        return None
    if len(data) < 2:
        return None
    key = data[0]
    try:
        return bytes(b ^ key for b in data[1:]).decode("utf-8")
    except UnicodeDecodeError:
        return None


def deobfuscate(text: str) -> str:
    t = _OBF_AT.sub("@", text)
    return _OBF_DOT.sub(".", t)


def clean_email(local: str, domain: str) -> str | None:
    local, domain = local.strip().lower(), domain.strip().lower().rstrip(".")
    tld = domain.rsplit(".", 1)[-1]
    if tld in FILE_TLDS:
        return None
    if domain in JUNK_EMAIL_DOMAINS or any(domain.endswith("." + j) for j in ("sentry.io", "wixpress.com")):
        return None
    if re.fullmatch(r"[0-9a-f]{16,}", local):  # hashes / ids (e.g. Sentry DSN keys)
        return None
    if re.search(r"\d+x$", local) and tld in FILE_TLDS:
        return None
    if local.startswith(("u002", "x22", "%")):
        return None
    return f"{local}@{domain}"


def find_emails(text: str) -> list[tuple[str, int]]:
    """All plausible addresses in text with their positions (after undoing common obfuscation)."""
    out: list[tuple[str, int]] = []
    for src in (text, deobfuscate(text)):
        for m in EMAIL_RE.finditer(src):
            e = clean_email(m.group(1), m.group(2))
            if e:
                out.append((e, m.start()))
    seen, uniq = set(), []
    for e, pos in out:
        if e not in seen:
            seen.add(e)
            uniq.append((e, pos))
    return uniq


# Words that tie a generic address (hello@, info@, a person) to applying. "jobs"/"careers" alone are
# not enough: a footer "info@" often sits next to a "Careers" menu link.
STRICT_APPLY_RE = re.compile(
    r"\b(apply|applying|application|applications|resumes?|résumés?|cvs?|c\.v\.|curriculum|cover letter|"
    r"portfolio|bewerb\w*|candidat\w*|postul\w*)\b",
    re.I,
)


def email_kind(email: str, context: str) -> str | None:
    """'hiring' (careers@...), 'contextual' (generic address next to apply/CV wording) or None."""
    local = email.split("@", 1)[0]
    if NEVER_HIRING_LOCAL_RE.match(local):
        return None
    if HIRING_LOCAL_RE.search(local):
        return "hiring"
    if STRICT_APPLY_RE.search(context or ""):
        return "contextual"
    return None


# --------------------------------------------------------------------------- page analysis

@dataclass
class Link:
    url: str
    text: str
    vendor: str = ""
    kind: str = ""          # vendor kind, or "" for a plain link


@dataclass
class PageInfo:
    url: str
    title: str = ""
    text: str = ""
    text_len: int = 0
    is_careers_like: bool = False
    soft_404: bool = False
    parked: bool = False
    blocked: bool = False
    js_shell: bool = False
    apply_by_email_phrase: str = ""
    no_openings: bool = False
    mentions_engineering: bool = False
    vendor_links: list[Link] = field(default_factory=list)
    careers_links: list[tuple[int, Link]] = field(default_factory=list)  # (score, link)
    detail_links: list[Link] = field(default_factory=list)
    emails: list[tuple[str, str, str]] = field(default_factory=list)       # (email, kind, snippet)
    roles: list[str] = field(default_factory=list)


_BLOCK_TAG_RE = re.compile(
    r"<(/?)(p|div|li|h[1-6]|tr|br|section|article|dt|dd|td|th|blockquote|pre|label|ul|ol|table|header|"
    r"footer|nav|main|aside|summary|details|figure|figcaption|hr)\b",
    re.I,
)


def _soup(page: str) -> BeautifulSoup:
    """Parse a page. A newline is put before every block-level tag so the text keeps its line structure
    (much faster than inserting nodes into the tree afterwards)."""
    raw = (page or "")[:MAX_PARSE_CHARS]
    raw = _BLOCK_TAG_RE.sub(lambda m: "\n" + m.group(0), raw)
    return BeautifulSoup(raw, _PARSER)


def visible_text(soup: BeautifulSoup, drop_chrome: bool = False) -> str:
    """Readable text of a page. drop_chrome also removes nav/header/footer (used for change diffs)."""
    # Not "head": lxml's HTML4 parser can leave HTML5 elements (footer, section...) inside <head>.
    for t in soup(["script", "style", "noscript", "template", "svg", "title", "meta", "link", "base",
                   "iframe", "object"]):
        t.decompose()
    if drop_chrome:
        for t in soup(["nav", "header", "footer", "form", "button", "select"]):
            t.decompose()
        for t in soup.select('[role="navigation"], [aria-hidden="true"], .cookie, #cookie, [class*="cookie-banner"]'):
            t.decompose()
    blocks = []
    text = soup.get_text(" ")
    lines = [" ".join(x.split()) for x in text.splitlines()]
    for ln in lines:
        if ln:
            blocks.append(ln)
    return "\n".join(blocks)


def _urls_in_markup(soup: BeautifulSoup, raw: str, base: str) -> list[Link]:
    """Every URL a page references: links (with their text), embeds, scripts, forms and raw strings."""
    found: list[Link] = []
    for a in soup.find_all("a", href=True):
        u = absolute(base, a["href"])
        if u:
            found.append(Link(u, " ".join(a.get_text(" ").split())[:140]))
    for tag, attr in (("iframe", "src"), ("iframe", "data-src"), ("script", "src"), ("form", "action"),
                      ("link", "href"), ("embed", "src"), ("object", "data"), ("div", "data-url"),
                      ("button", "data-href"), ("a", "data-href")):
        for el in soup.find_all(tag):
            v = el.get(attr)
            if isinstance(v, str):
                u = absolute(base, v)
                if u:
                    found.append(Link(u, ""))
    for m in soup.find_all("meta", attrs={"http-equiv": re.compile("refresh", re.I)}):
        c = m.get("content") or ""
        mm = re.search(r"url\s*=\s*['\"]?([^'\";]+)", c, re.I)
        if mm:
            u = absolute(base, mm.group(1))
            if u:
                found.append(Link(u, ""))
    # URLs inside scripts / JSON blobs (e.g. __NEXT_DATA__, embed configs); JSON escapes "\/".
    flat = raw.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    for m in re.finditer(r"https?://[^\s\"'<>\\)\]}]+", flat):
        found.append(Link(m.group(0).rstrip(".,;"), ""))
    return found


def _careers_score(link: Link, base: str) -> int:
    """How likely a link points at the careers page (0 = not at all)."""
    try:
        p = urlsplit(link.url)
    except ValueError:
        return 0
    path = (p.path or "/").lower()
    segs = [s for s in path.split("/") if s]
    text = (link.text or "").strip()
    score = 0
    if text and len(text) <= 60 and CAREER_TEXT_RE.search(text):
        score += 4
    if any(s.split(".")[0] in CAREER_PATH_SEGMENTS for s in segs):
        score += 3
    host = (p.hostname or "").lower()
    if host.split(".")[0] in ("careers", "jobs", "career", "karriere", "join", "work"):
        score += 3
    if score and NON_CAREER_PATH_RE.search(path) and not text:
        score = 0
    if score and NON_CAREER_PATH_RE.search(path) and text and not CAREER_TEXT_RE.fullmatch(text.strip(" ›→>.")):
        score = 0  # "Jobs API" -> /docs/jobs, "How we hire" -> /blog/...
    if score and registrable_domain(link.url) != registrable_domain(base):
        score -= 1  # external pages are less likely to be the company's own careers page
    if len(segs) > 3:
        score -= 1
    return max(score, 0)


def _role_lines(soup: BeautifulSoup) -> list[str]:
    roles: list[str] = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "li", "a", "strong", "b", "dt", "summary", "p"]):
        t = " ".join(el.get_text(" ").split())
        if not (6 <= len(t) <= 90) or len(t.split()) > 12:
            continue
        if CAREER_TEXT_RE.fullmatch(t) or t.lower() in ("open positions", "open roles", "our team"):
            continue
        if ROLE_RE.search(t) and not t.endswith((".", "?", "!")) and t not in roles:
            roles.append(t)
        if len(roles) >= 40:
            break
    return roles


def analyze(page: str, url: str, status: int = 200) -> PageInfo:
    """Inspect one HTML page. `url` is the final URL after redirects."""
    raw = page or ""
    soup = _soup(raw)
    info = PageInfo(url=url)
    title_el = soup.find("title")
    info.title = " ".join(title_el.get_text(" ").split())[:200] if title_el else ""
    h1 = soup.find("h1")
    h1_text = " ".join(h1.get_text(" ").split())[:200] if h1 else ""

    # Cloudflare-protected addresses must be decoded before the text is extracted.
    cf_emails: list[str] = []
    for el in soup.select("[data-cfemail]"):
        e = decode_cfemail(el.get("data-cfemail", ""))
        if e:
            cf_emails.append(e)
            el.replace_with(e)
    for a in soup.find_all("a", href=re.compile(r"/cdn-cgi/l/email-protection#", re.I)):
        e = decode_cfemail(a["href"].split("#", 1)[1])
        if e:
            cf_emails.append(e)
    mailtos: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=re.compile(r"^\s*mailto:", re.I)):
        target = unquote(htmllib.unescape(a["href"].strip()[7:].split("?", 1)[0]))
        for part in re.split(r"[,;\s]+", target):
            mailtos.append((part, " ".join(a.get_text(" ").split())))

    info.vendor_links = []
    for link in _urls_in_markup(soup, raw, url):
        v = match_vendor(link.url)
        if v:
            link.vendor, link.kind = v.name, v.kind
            info.vendor_links.append(link)
        sc = _careers_score(link, url) if link.text or not v else 0
        if sc >= 3:
            info.careers_links.append((sc, link))
    info.roles = _role_lines(soup)

    # Same-site links that look like individual job pages (followed when the listing has no apply route).
    base_rd = registrable_domain(url)
    seen = set()
    for a in soup.find_all("a", href=True):
        u = absolute(url, a["href"])
        if not u or registrable_domain(u) != base_rd or u in seen:
            continue
        t = " ".join(a.get_text(" ").split())
        lp = (urlsplit(u).path or "").lower()
        jobish_path = re.search(r"/(careers?|jobs?|positions?|openings?|roles?|vacanc(y|ies)|opportunities)/[^/]+", lp)
        if jobish_path and (ROLE_RE.search(t) or ROLE_RE.search(lp.replace("-", " "))) and u.rstrip("/") != url.rstrip("/"):
            seen.add(u)
            info.detail_links.append(Link(u, t[:120]))

    text = visible_text(soup)  # destructive: must come after every other use of `soup`
    info.text = text[:200_000]
    info.text_len = len(text)
    head = f"{info.title}\n{h1_text}"
    path = (urlsplit(url).path or "/").lower()

    info.parked = bool(PARKED_RE.search(head + "\n" + text[:3000])) or registrable_domain(url) in PARKING_HOSTS
    if status in (401, 403, 429, 503):
        # error statuses: a bot wall shows up anywhere in the markup
        info.blocked = bool(BLOCK_RE.search(head + "\n" + raw[:50000])) or info.text_len < 300
    else:
        # a normal 200 page may load a bot-protection script; only its visible words count
        info.blocked = info.text_len < 2500 and bool(BLOCK_RE.search(head + "\n" + text[:2500]))
    info.soft_404 = status in (404, 410) or (bool(SOFT_404_RE.search(head)) and info.text_len < 6000)
    info.js_shell = info.text_len < 400 and bool(JS_SHELL_RE.search(raw[:200_000]))
    seg_hit = any(s.split(".")[0] in CAREER_PATH_SEGMENTS for s in path.split("/") if s)
    info.is_careers_like = (seg_hit and not NON_CAREER_PATH_RE.search(path)) or bool(CAREER_TEXT_RE.search(head)) \
        or host_of(url).split(".")[0] in ("careers", "jobs", "career", "karriere")
    m = APPLY_BY_EMAIL_RE.search(text)
    info.apply_by_email_phrase = " ".join(text[max(0, m.start() - 60): m.end() + 80].split()) if m else ""
    info.no_openings = bool(NO_OPENINGS_RE.search(text))
    info.mentions_engineering = bool(ENGINEERING_RE.search(text))

    # Emails: mailto links + Cloudflare-decoded + anything in the visible text.
    found: dict[str, tuple[str, str]] = {}
    text_emails = find_emails(text)
    positions = {e: pos for e, pos in text_emails}
    candidates = [(e, t) for e, t in mailtos] + [(e, "") for e in cf_emails] + [(e, "") for e, _ in text_emails]
    for addr, link_text in candidates:
        mm = EMAIL_RE.fullmatch(addr.strip().lower()) if addr else None
        if not mm:
            continue
        e = clean_email(mm.group(1), mm.group(2))
        if not e or e in found:
            continue
        pos = positions.get(e)
        if pos is None:
            pos = text.lower().find(e)
        ctx = text[max(0, pos - 300): pos + 300] if pos is not None and pos >= 0 else ""
        kind = email_kind(e, f"{link_text} {ctx}")
        if kind:
            found[e] = (kind, " ".join(ctx[max(0, (len(ctx) // 2) - 120): (len(ctx) // 2) + 120].split()))
    info.emails = [(e, k, s) for e, (k, s) in found.items()]
    info.careers_links.sort(key=lambda x: -x[0])
    return info


# --------------------------------------------------------------------------- vendor tenant names

_GENERIC_LABELS = {
    "jobs", "boards", "job-boards", "boards-api", "apply", "api", "careers", "career", "app", "recruiting",
    "ats", "hire", "eu", "us", "www", "embed", "job", "go", "secure", "rec", "pro", "wd1", "wd2", "wd3", "wd5",
    "wd10", "wd12", "wd103", "wd501", "companies", "company", "company-jobs", "l", "o", "to", "en", "en-us",
}


def vendor_slug(url: str) -> str:
    """The customer name in a job-board URL: boards.greenhouse.io/acme, acme.bamboohr.com, ... -> 'acme'."""
    try:
        p = urlsplit(url)
    except ValueError:
        return ""
    v = match_vendor(url)
    if not v:
        return ""
    host = (p.hostname or "").lower()
    q = dict(x.split("=", 1) for x in (p.query or "").split("&") if "=" in x)
    if q.get("for"):
        return q["for"].lower()
    sub = ""
    for apex in v.apexes:
        if host == apex or host.endswith("." + apex):
            sub = host[: -len(apex)].rstrip(".")
            break
    for label in sub.split("."):
        label = re.sub(r"^(careers?|jobs?)-", "", label)
        if label and label not in _GENERIC_LABELS:
            return label
    for seg in (p.path or "").split("/"):
        seg = unquote(seg).lower()
        if seg and seg not in _GENERIC_LABELS and not seg.isdigit():
            return seg
    return ""


def _alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def slug_matches(slug: str, name: str, domain: str) -> bool:
    """Does a job-board tenant name plausibly belong to this company?"""
    s = _alnum(slug)
    if len(s) < 2:
        return False
    label = _alnum(domain.split(".")[0]) if domain else ""
    n = _alnum(name)
    for cand in (label, n):
        if not cand:
            continue
        if s == cand:
            return True
        if len(cand) >= 4 and len(s) >= 4 and (s.startswith(cand) or cand.startswith(s)):
            return True
        if len(cand) >= 5 and cand in s:
            return True
    return False
