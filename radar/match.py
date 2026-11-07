"""Title matching, US-location detection, description checks and scoring."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import Board, Job

# ---------------------------------------------------------------- location

US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
    "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico",
    "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
    "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming", "district of columbia",
}
US_ABBR = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
    "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
US_CITIES = {
    "new york city", "nyc", "san francisco", "bay area", "seattle", "austin", "boston", "chicago",
    "denver", "atlanta", "dallas", "houston", "los angeles", "san jose", "mountain view", "palo alto",
    "sunnyvale", "santa clara", "menlo park", "redmond", "bellevue", "arlington", "reston", "herndon",
    "mclean", "columbus", "pittsburgh", "philadelphia", "minneapolis", "salt lake city", "raleigh",
    "durham", "charlotte", "nashville", "phoenix", "tempe", "scottsdale", "portland", "san diego",
    "irvine", "miami", "detroit", "ann arbor", "kansas city", "st. louis", "saint louis", "madison",
    "boulder", "cambridge, ma", "brooklyn", "jersey city", "hoboken", "newark", "baltimore",
    "richmond", "tampa", "orlando", "jacksonville", "indianapolis", "cincinnati", "cleveland",
    "milwaukee", "omaha", "oklahoma city", "albuquerque", "las vegas", "reno", "sacramento",
    "oakland", "berkeley", "san mateo", "redwood city", "foster city", "plano", "irving", "frisco",
    "fort worth", "san antonio", "el paso", "boise", "provo", "lehi", "huntsville", "birmingham, al",
    "chattanooga", "knoxville", "louisville", "lexington", "des moines", "st. paul", "rochester, ny",
    "buffalo", "albany", "hartford", "stamford", "providence", "burlington", "manchester, nh",
    "washington, d.c", "washington dc", "washington, dc", "silicon valley",
}
US_STRONG = re.compile(
    r"(?<![a-z])(united states(?: of america)?|u\.s\.a\.?|u\.s\.|usa|remote[\s,-]*\(?us\)?|"
    r"us[\s,-]*remote|\(us\)|anywhere in the us|nationwide)(?![a-z])",
    re.I,
)
US_UPPER = re.compile(r"(?<![A-Za-z])(US|USA|U\.S\.)(?![A-Za-z])")
NON_US = re.compile(
    r"\b(canada|toronto|vancouver|montreal|ottawa|calgary|waterloo|ontario|quebec|british columbia|"
    r"united kingdom|\buk\b|england|london|manchester|edinburgh|scotland|ireland|dublin|"
    r"germany|berlin|munich|hamburg|frankfurt|france|paris|spain|madrid|barcelona|portugal|lisbon|"
    r"italy|milan|rome|netherlands|amsterdam|belgium|brussels|switzerland|zurich|geneva|austria|vienna|"
    r"sweden|stockholm|norway|oslo|denmark|copenhagen|finland|helsinki|poland|warsaw|krakow|"
    r"czech|prague|romania|bucharest|hungary|budapest|ukraine|kyiv|kiev|serbia|belgrade|croatia|greece|"
    r"turkey|istanbul|israel|tel aviv|herzliya|haifa|raanana|india|bangalore|bengaluru|hyderabad|pune|"
    r"chennai|mumbai|delhi|gurgaon|gurugram|noida|kolkata|ahmedabad|china|beijing|shanghai|shenzhen|"
    r"hong kong|taiwan|taipei|japan|tokyo|osaka|korea|seoul|singapore|malaysia|kuala lumpur|"
    r"philippines|manila|indonesia|jakarta|vietnam|thailand|bangkok|australia|sydney|melbourne|"
    r"brisbane|perth|new zealand|auckland|mexico|guadalajara|monterrey|brazil|são paulo|sao paulo|"
    r"argentina|buenos aires|colombia|bogota|bogotá|medellin|chile|santiago|peru|lima|costa rica|"
    r"uruguay|montevideo|south africa|cape town|johannesburg|nigeria|lagos|kenya|nairobi|egypt|cairo|"
    r"uae|dubai|abu dhabi|saudi|riyadh|qatar|doha|emea|apac|latam|europe|european union|asia)\b",
    re.I,
)
_ABBR_RE = re.compile(r"(?:,|-|–)\s*(" + "|".join(US_ABBR) + r")\b(?!\s*[a-z])")
_US_COUNTRY_CODES = {"us", "usa", "united states", "united states of america", "u.s.", "u.s.a."}


def classify_location(text: str, countries: list[str] | None = None) -> str:
    """Return 'us', 'non_us' or 'unknown'."""
    countries = [c.strip().lower() for c in (countries or []) if c and c.strip()]
    if countries:
        if any(c in _US_COUNTRY_CODES or c.startswith("united states") for c in countries):
            return "us"
        # Greenhouse "offices" can be cities/regions rather than countries; only trust clear signals.
        joined = "; ".join(countries)
        if NON_US.search(joined) or all(len(c) == 2 for c in countries):
            if not _text_says_us(text):
                return "non_us"
    t = text or ""
    if _text_says_us(t):
        return "us"
    if NON_US.search(t):
        return "non_us"
    if _ABBR_RE.search(t):
        return "us"
    return "unknown"


def _text_says_us(t: str) -> bool:
    if not t:
        return False
    if US_STRONG.search(t) or US_UPPER.search(t):
        return True
    low = t.lower()
    for name in US_STATES:
        if re.search(r"\b" + re.escape(name) + r"\b", low):
            # "Georgia" alone could be the country; accept only with a US-looking context or no non-US terms
            if name == "georgia" and re.search(r"tbilisi", low):
                continue
            if name == "washington" and NON_US.search(low):
                continue
            return True
    for city in US_CITIES:
        if city in low:
            return True
    return False


# ---------------------------------------------------------------- titles and descriptions

@dataclass
class Rules:
    core: list[re.Pattern]
    bridge: list[re.Pattern]
    exclude: list[re.Pattern]
    entry: list[re.Pattern]
    blockers: list[re.Pattern]
    blocker_exceptions: list[re.Pattern]
    no_sponsorship: list[re.Pattern]
    sponsors: list[re.Pattern]
    e_verify: list[re.Pattern]
    big_companies: list[re.Pattern]
    exclude_companies: list[re.Pattern]
    alert_threshold: int = 45
    max_post_age_days: int = 14
    block_alerts_if_years_at_least: int = 7

    @classmethod
    def from_config(cls, cfg: dict) -> "Rules":
        def comp(xs):
            return [re.compile(x, re.I) for x in xs or []]
        t, d, s = cfg["titles"], cfg["description"], cfg.get("scoring", {})
        return cls(
            core=comp(t["core"]), bridge=comp(t["bridge"]), exclude=comp(t["exclude"]),
            entry=comp(t["entry"]), blockers=comp(d["blockers"]),
            blocker_exceptions=comp(d.get("blocker_exceptions")),
            no_sponsorship=comp(d["no_sponsorship"]), sponsors=comp(d["sponsors"]),
            e_verify=comp(d["e_verify"]), big_companies=comp(cfg.get("big_companies")),
            exclude_companies=comp(cfg.get("exclude_companies")),
            alert_threshold=int(s.get("alert_threshold", 45)),
            max_post_age_days=int(s.get("max_post_age_days", 14)),
            block_alerts_if_years_at_least=int(s.get("block_alerts_if_years_at_least", 7)),
        )


def _any(patterns, text) -> bool:
    return any(p.search(text) for p in patterns)


def title_category(title: str, rules: Rules) -> str | None:
    """'core', 'bridge' or None (not a match or excluded)."""
    t = " " + (title or "").replace("–", "-").replace("—", "-") + " "
    if _any(rules.exclude, t):
        return None
    if _any(rules.core, t):
        return "core"
    if _any(rules.bridge, t):
        return "bridge"
    return None


_YEARS = re.compile(
    r"(?:at least|minimum(?: of)?|min\.?)?\s*\(?(\d{1,2})\)?\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*(\d{1,2})\s*)?\+?\s*"
    r"(?:years?|yrs?)\b(?:'s)?",
    re.I,
)


def required_years(text: str) -> int | None:
    """Highest minimum years-of-experience requirement mentioned next to 'experience'."""
    best = None
    for m in _YEARS.finditer(text or ""):
        window = text[m.end(): m.end() + 70].lower()
        before = text[max(0, m.start() - 40): m.start()].lower()
        if "experience" not in window and "experience" not in before:
            continue
        n = int(m.group(1))
        if n > 15:
            continue
        best = n if best is None else max(best, n)
    return best


def blocker_hit(text: str, rules: Rules) -> str | None:
    for p in rules.blockers:
        for m in p.finditer(text or ""):
            window = text[max(0, m.start() - 60): m.end() + 60]
            if _any(rules.blocker_exceptions, window):
                continue
            return m.group(0).strip()
    return None


# ---------------------------------------------------------------- scoring

@dataclass
class Assessment:
    category: str
    location_class: str
    score: int
    status: str                 # alert | quiet | blocked | old
    flags: list[str] = field(default_factory=list)
    years: int | None = None
    blocker: str | None = None


def is_excluded_company(board: Board, rules: Rules) -> bool:
    return _any(rules.exclude_companies, board.company or "")


def assess(job: Job, board: Board, rules: Rules, category: str, now: datetime | None = None) -> Assessment:
    now = now or datetime.now(timezone.utc)
    flags: list[str] = []
    title = job.title or ""
    desc = job.description or ""
    loc_class = classify_location(job.location, job.countries)

    score = 45 if category == "core" else 30
    if _any(rules.entry, " " + title + " "):
        score += 15
        flags.append("entry-level title")
    elif re.search(r"\b(ii|2)\b", title, re.I):
        score += 0
    else:
        score += 5

    years = required_years(desc) if desc else None
    if years is not None:
        if years <= 2:
            score += 10
        elif years <= 4:
            score -= 5
        else:
            score -= 20
        flags.append("0-2 yrs exp" if years == 0 else f"{years}+ yrs exp")

    age_days = None
    if job.posted_at:
        age_days = max(0.0, (now - job.posted_at).total_seconds() / 86400)
        if age_days <= 1:
            score += 15
        elif age_days <= 3:
            score += 8
        elif age_days <= 7:
            score += 3

    if job.workplace in ("onsite", "hybrid"):
        score += 5
    elif job.workplace == "remote":
        score -= 3
    if loc_class == "unknown":
        score -= 5
        flags.append("location unclear")

    if desc and _any(rules.e_verify, desc):
        score += 5
        flags.append("E-Verify")
    if desc and _any(rules.sponsors, desc):
        score += 5
        flags.append("sponsors visas")
    elif desc and _any(rules.no_sponsorship, desc):
        score -= 5
        flags.append("no future H-1B sponsorship")
    if board.cap_exempt:
        score += 5
        flags.append("likely H-1B cap-exempt (verify)")
    if board.priority:
        score += 10
        flags.append("priority company")
    if _any(rules.big_companies, board.company or ""):
        score -= 5

    score = max(0, min(100, score))
    blocker = blocker_hit(" ".join([title, desc]), rules)

    if age_days is not None and age_days > rules.max_post_age_days:
        status = "old"
    elif blocker:
        status = "blocked"
        flags.append(f"needs: {blocker}")
    elif loc_class == "non_us":
        status = "old"  # never reported; caller normally filters these out earlier
    elif years is not None and years >= rules.block_alerts_if_years_at_least:
        status = "quiet"
    elif score >= rules.alert_threshold:
        status = "alert"
    else:
        status = "quiet"
    return Assessment(category=category, location_class=loc_class, score=score, status=status,
                      flags=flags, years=years, blocker=blocker)
