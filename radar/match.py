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
    r"uae|dubai|abu dhabi|saudi|riyadh|qatar|doha|emea|apac|latam|europe|european union|asia|"
    r"luxembourg|estonia|tallinn|latvia|riga|lithuania|vilnius|slovakia|bratislava|slovenia|ljubljana|"
    r"bulgaria|sofia|cyprus|limassol|malta|iceland|reykjavik|morocco|casablanca|tunisia|ghana|accra|"
    r"pakistan|karachi|lahore|islamabad|bangladesh|dhaka|sri lanka|colombo|nepal|kathmandu|armenia|yerevan|"
    r"tbilisi|kazakhstan|almaty|ecuador|quito|guatemala|panama|dominican republic|jamaica|venezuela|"
    r"bolivia|paraguay|el salvador|honduras|nicaragua|ethiopia|addis ababa|rwanda|kigali|uganda|kampala|"
    r"tanzania|zambia|zimbabwe|jordan|amman|lebanon|beirut|kuwait|bahrain|oman|muscat|"
    r"belfast|cardiff|bristol|cork|galway|lyon|toulouse|rotterdam|eindhoven|antwerp|gothenburg|"
    r"cluj|iasi|wroclaw|gdansk|lodz|poznan|tallin|noida|coimbatore|kochi|trivandrum|mysore|"
    r"shenzhen|hangzhou|chengdu|suzhou|nanjing|wuhan|xian|xi.an|guangzhou|kowloon|yokohama|"
    r"busan|penang|cebu|ho chi minh|hanoi|da nang|auckland|wellington|christchurch|"
    r"curitiba|porto alegre|belo horizonte|rio de janeiro|campinas|florianopolis|cordoba|rosario|"
    r"mexico city|ciudad de mexico|cdmx|tijuana|queretaro|puebla|leon, gto|zapopan|"
    r"king abdullah|jeddah|veneto|padua|turin|torino|piedmont|lviv|vantaa|espoo|hsinchu|brno|roznov|surry hills|"
    r"quezon|indaiatuba|bohemia|stuttgart|ingolstadt|wolfsburg|regensburg|ulm|friedrichshafen|"
    r"timisoara|sibiu|chihuahua|juarez|saltillo|san luis potosi|windsor, on|oshawa|markham)\b",
    re.I,
)
_ABBR_RE = re.compile(r"(?:,|-|–)\s*(" + "|".join(US_ABBR) + r")\b(?!\s*[a-z])")
# State abbreviations that are also common country codes (CA=Canada, IN=India, DE=Germany, ...)
_AMBIG_ABBR = {"CA", "IN", "DE", "IL", "MA", "CO", "AR", "ID", "PA", "MT", "AL", "GA", "MD", "ME", "MN", "MO", "SC", "SD", "TN"}
# "Padua, Veneto, IT" / "Acuña, COA, MX": a trailing country code that is not also a US state.
_FOREIGN_TAIL = re.compile(
    r",\s*(IT|FI|UA|BR|CZ|MX|TW|RO|PH|JP|KR|CN|FR|ES|PL|SE|NL|BE|CH|AT|HU|PT|GR|TR|IE|GB|UK|AU|AUS|NZ|SG|MY|TH|VN|ZA|"
    r"AE|SA|EG|RS|BG|HR|SK|SI|LT|LV|EE|NO|DK|CL|PE|CR|UY|EC|DEU|IND|CHN|JPN|KOR|MEX|BRA|GBR|FRA|ITA|ESP|POL)\s*(;|$)")
_CJK = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]")
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
    if any(a not in _AMBIG_ABBR for a in _ABBR_RE.findall(t)):
        return "us"  # e.g. "Lebanon, NH", "Paris, TX", "Panama City, FL"
    if NON_US.search(t):
        return "non_us"
    if _ABBR_RE.search(t):
        return "us"
    if _FOREIGN_TAIL.search(t) or _CJK.search(t):
        return "non_us"
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
    staffing: list[re.Pattern]
    alert_threshold: int = 45
    max_post_age_days: int = 14
    block_alerts_if_years_at_least: int = 7
    hide_if_years_at_least: int = 0   # experienced mode: postings asking this many years go to the hidden list
    # ---- optional, used by profiles that match on job-text evidence (see profiles/*/config.yaml)
    wide: list[re.Pattern] = field(default_factory=list)        # generic titles: kept only with skills evidence
    wide_focus: list[re.Pattern] = field(default_factory=list)  # same, but only on the profile's own employer list
    stretch: list[re.Pattern] = field(default_factory=list)     # titles a level above the candidate
    boost: list[re.Pattern] = field(default_factory=list)       # titles naming the candidate's specialty
    skills: list[tuple[str, re.Pattern, int]] = field(default_factory=list)  # (name, pattern, weight)
    wide_min_weight: int = 6
    untitled_min_weight: int = 9
    core_min_weight: int = 0        # target titles whose text shows less than this are set aside
    mode: str = "entry"             # entry (early-career scoring) | experienced
    years_experience: int = 0
    home: list[re.Pattern] = field(default_factory=list)        # locations that get a small bonus
    stale_after_days: int = 60
    ranker: object = None           # radar.rank.Ranker: rule-based fit score from the full job text
    senior: list[re.Pattern] = field(default_factory=list)      # senior-level titles: kept when years allow
    bridge_evidence: list[re.Pattern] = field(default_factory=list)  # adjacent titles must name these tools
    strong: list[re.Pattern] = field(default_factory=list)       # unmistakable target titles (e.g. SRE) ...
    hard_exclude: list[re.Pattern] = field(default_factory=list) # ... that only these exclusions can drop
    bridge_min: int = 2
    years_by_section: bool = False  # read years from required/duties text only, degree-aware ("BS+5 or MS+3")
    degree: str = "master"          # the degree path that applies when a posting gives several

    @classmethod
    def from_config(cls, cfg: dict) -> "Rules":
        def comp(xs):
            return [re.compile(x, re.I) for x in xs or []]
        t, d, s = cfg["titles"], cfg["description"], cfg.get("scoring", {})
        sk = cfg.get("skills") or {}
        skills = [(name, re.compile(pat, re.I), weight)
                  for group, weight in (("strong", 3), ("medium", 2), ("weak", 1))
                  for name, pat in (sk.get(group) or {}).items()]
        return cls(
            wide=comp(t.get("wide")), wide_focus=comp(t.get("wide_focus")), stretch=comp(t.get("stretch")),
            boost=comp(t.get("boost")), skills=skills,
            wide_min_weight=int(sk.get("wide_min_weight", 6)),
            untitled_min_weight=int(sk.get("untitled_min_weight", 9)),
            core_min_weight=int(sk.get("core_min_weight", 0)),
            mode=str(s.get("mode", "entry")), years_experience=int(s.get("years_experience", 0)),
            home=comp(s.get("home_locations")), stale_after_days=int(s.get("stale_after_days", 60)),
            core=comp(t["core"]), bridge=comp(t["bridge"]), exclude=comp(t["exclude"]),
            entry=comp(t["entry"]), blockers=comp(d["blockers"]),
            blocker_exceptions=comp(d.get("blocker_exceptions")),
            no_sponsorship=comp(d["no_sponsorship"]), sponsors=comp(d["sponsors"]),
            e_verify=comp(d["e_verify"]), big_companies=comp(cfg.get("big_companies")),
            exclude_companies=comp(cfg.get("exclude_companies")),
            staffing=comp(cfg.get("staffing_company_patterns")),
            alert_threshold=int(s.get("alert_threshold", 45)),
            max_post_age_days=int(s.get("max_post_age_days", 14)),
            block_alerts_if_years_at_least=int(s.get("block_alerts_if_years_at_least", 7)),
            hide_if_years_at_least=int(s.get("hide_if_years_at_least", 0)),
            senior=comp(t.get("senior")), years_by_section=bool(s.get("years_by_section")),
            bridge_evidence=comp(s.get("bridge_evidence")), bridge_min=int(s.get("bridge_min", 2)),
            strong=comp(t.get("strong")), hard_exclude=comp(t.get("hard_exclude")),
            degree=str(s.get("degree", "master")),
        )


def _any(patterns, text) -> bool:
    return any(p.search(text) for p in patterns)


def _norm_title(title: str) -> str:
    return " " + (title or "").replace("–", "-").replace("—", "-") + " "


def title_category(title: str, rules: Rules, focus: bool = False, searched: bool = False) -> str | None:
    """'core', 'bridge', 'wide' or None (not a match or excluded).

    'wide' is a generic title (or a keyword-search hit) that is kept only if the job text shows enough of
    the candidate's skills; profiles without a `skills` section never produce it."""
    t = _norm_title(title)
    # Unmistakable target titles ("Site Reliability Engineer - Voice Platform", "SRE, SAP Cloud") are not
    # dropped by the broad word exclusions; only seniority, internships, clearance and the like apply.
    if rules.strong and _any(rules.strong, t):
        return None if _any(rules.hard_exclude, t) else "core"
    if _any(rules.exclude, t):
        return None
    if _any(rules.core, t):
        return "core"
    if _any(rules.bridge, t):
        return "bridge"
    if not rules.skills:
        return None
    if _any(rules.wide, t) or (focus and _any(rules.wide_focus, t)) or searched:
        return "wide"
    return None


def skill_hits(text: str, rules: Rules) -> tuple[int, int, list[str]]:
    """(total weight, number of strong skills, skill names) found in a job's title + description."""
    weight, strong, names = 0, 0, []
    for name, pat, w in rules.skills:
        if pat.search(text or ""):
            weight += w
            strong += w >= 3
            names.append(name)
    return weight, strong, names


def has_evidence(text: str, rules: Rules, minimum: int | None = None) -> bool:
    """Enough of the candidate's tools in the text to keep a generically titled job."""
    weight, strong, _ = skill_hits(text, rules)
    return strong >= 1 and weight >= (rules.wide_min_weight if minimum is None else minimum)


def job_category(job: Job, board: Board, rules: Rules) -> str | None:
    """Category of a job from its title, how it was found and (on focus boards) its text."""
    cat = title_category(job.title, rules, focus=board.focus, searched=bool(job.found_by))
    if cat is None and board.focus and rules.skills and job.description and not _any(rules.exclude, _norm_title(job.title)):
        weight, strong, _ = skill_hits(f"{job.title}\n{job.description}", rules)
        if strong >= 2 and weight >= rules.untitled_min_weight:
            cat = "wide"
    return cat


def row_still_fits(row: dict, rules: Rules) -> bool:
    """Does a saved match still fit after the title rules changed?"""
    if row.get("category") == "wide" and rules.skills:
        return not _any(rules.exclude, _norm_title(row.get("title", "")))  # kept on job-text evidence
    return title_category(row.get("title", ""), rules, focus=True) is not None


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
    has: list[str] = field(default_factory=list)       # resume skills the job asks for (ranker)
    missing: list[str] = field(default_factory=list)   # required skills the resume does not show (ranker)
    detail: str = ""                                    # how the score was made up (ranker)


def is_excluded_company(board: Board, rules: Rules) -> bool:
    return _any(rules.exclude_companies, board.company or "")


def assess(job: Job, board: Board, rules: Rules, category: str, now: datetime | None = None) -> Assessment:
    now = now or datetime.now(timezone.utc)
    flags: list[str] = []
    title = job.title or ""
    desc = job.description or ""
    loc_class = classify_location(job.location, job.countries)
    if loc_class == "unknown" and desc:          # "Remote", blank, etc.: the job text often says where
        loc_class = location_from_text(desc)
    if rules.mode == "experienced" and rules.ranker is not None:
        return _assess_ranked(job, board, rules, category, now, loc_class)
    if rules.mode == "experienced":
        return _assess_experienced(job, board, rules, category, now, loc_class)

    score = 45 if category == "core" else 30
    if _any(rules.entry, " " + title + " "):
        score += 15
        flags.append("entry-level title")
    elif re.search(r"\b(ii|2)\b", title, re.I):
        score += 0
    else:
        score += 5

    if rules.years_by_section and desc:
        years = years_from_text(desc, rules.degree)
    else:
        years = required_years(desc) if desc else None
    if years is not None:
        if years <= 2:
            score += 10
        elif years <= 4:
            score -= 5
        else:
            score -= 20
        flags.append("0-2 yrs exp" if years == 0 else f"{years}+ yrs exp")
    if _any(rules.senior, " " + title + " "):
        if years is None:
            score -= 10
            flags.append("senior title, years not stated")
        else:
            flags.append("senior title")

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
    if _any(rules.staffing, job.company or board.company or ""):
        score -= 5
        flags.append("staffing/consulting firm: verify it's real")

    thin_bridge = False
    if category == "bridge" and rules.bridge_evidence and len(desc) > 300:
        named = sum(1 for p in rules.bridge_evidence if p.search(desc))
        thin_bridge = named < rules.bridge_min
    score = max(0, min(100, score))
    blocker = blocker_hit(" ".join([title, desc]), rules)

    if age_days is not None and age_days > rules.max_post_age_days:
        status = "old"
    elif blocker:
        status = "blocked"
        flags.append(f"needs: {blocker[:40]}")
    elif loc_class == "non_us":
        status = "old"  # never reported; caller normally filters these out earlier
    elif years is not None and rules.hide_if_years_at_least and years >= rules.hide_if_years_at_least:
        status = "blocked"
        flags.append(f"asks for {years}+ years of experience")
    elif thin_bridge:
        status = "blocked"
        flags.append("adjacent title, little DevOps/cloud work in the text")
    elif years is not None and years >= rules.block_alerts_if_years_at_least:
        status = "quiet"
    elif score >= rules.alert_threshold:
        status = "alert"
    else:
        status = "quiet"
    return Assessment(category=category, location_class=loc_class, score=score, status=status,
                      flags=flags, years=years, blocker=blocker)


def _assess_experienced(job: Job, board: Board, rules: Rules, category: str, now: datetime,
                        loc_class: str) -> Assessment:
    """Scoring for a candidate with a few years of experience: fit is mostly how many of their tools
    the job text names; senior titles are fine, staff/principal ones are a flagged stretch."""
    flags: list[str] = []
    title = job.title or ""
    desc = job.description or ""
    t = _norm_title(title)
    score = {"core": 40, "bridge": 34}.get(category, 28)

    weight, _strong, names = skill_hits(f"{title}\n{desc}", rules)
    if names:
        score += min(28, round(weight * 1.4))
        flags.append("tools: " + ", ".join(names[:8]) + (f" +{len(names) - 8}" if len(names) > 8 else ""))
    if not desc:
        flags.append("description not read")
    if _any(rules.boost, t):
        score += 8
    if _any(rules.stretch, t):
        score -= 10
        flags.append("stretch: senior-level title")
    elif _any(rules.entry, t):
        score -= 4
        flags.append("entry-level title")

    exp = rules.years_experience
    years = required_years(desc) if desc else None
    if years is not None:
        if years <= exp + 1:
            score += 8
        elif years <= exp + 3:
            score -= 6
        else:
            score -= 20
        flags.append(f"{years}+ yrs exp" if years else "no experience minimum")

    age_days = None
    if job.posted_at:
        age_days = max(0.0, (now - job.posted_at).total_seconds() / 86400)
        if age_days <= 1:
            score += 10
        elif age_days <= 3:
            score += 6
        elif age_days <= 7:
            score += 3
        elif age_days > rules.stale_after_days:
            score -= 5
            flags.append(f"open {int(age_days)} days")

    if _any(rules.home, job.location or ""):
        score += 5
    if loc_class == "unknown":
        score -= 5
        flags.append("location unclear")
    if desc and _any(rules.e_verify, desc):
        flags.append("E-Verify")
    if desc and _any(rules.sponsors, desc):
        score += 4
        flags.append("sponsors visas")
    elif desc and _any(rules.no_sponsorship, desc):
        score -= 2           # only a small nudge: sponsorship is noted, not held against the job
        flags.append("no visa sponsorship")
    if board.priority:
        score += 5
        flags.append("priority company")
    if _any(rules.staffing, job.company or board.company or ""):
        score -= 5
        flags.append("staffing/consulting firm")

    score = max(0, min(100, score))
    blocker = blocker_hit(" ".join([title, desc]), rules)
    if age_days is not None and age_days > rules.max_post_age_days:
        status = "old"
    elif blocker:
        status = "blocked"
        flags.append(f"needs: {blocker[:40]}")
    elif loc_class == "non_us":
        status = "old"
    elif desc and rules.core_min_weight and weight < rules.core_min_weight:
        status = "blocked"
        flags.append("none of your tools in the job text")
    elif years is not None and rules.hide_if_years_at_least and years >= rules.hide_if_years_at_least:
        status = "blocked"
        flags.append(f"asks for {years}+ years of experience")
    elif years is not None and years >= rules.block_alerts_if_years_at_least:
        status = "quiet"
    elif score >= rules.alert_threshold:
        status = "alert"
    else:
        status = "quiet"
    return Assessment(category=category, location_class=loc_class, score=score, status=status,
                      flags=flags, years=years, blocker=blocker)


def _assess_ranked(job: Job, board: Board, rules: Rules, category: str, now: datetime, loc_class: str) -> Assessment:
    """Experienced-candidate scoring from radar.rank: skills required vs. on the resume, title, years
    (degree-aware), industry, word overlap, freshness and place. Sponsorship moves the score by 2 at most."""
    title, desc = job.title or "", job.description or ""
    fit = rules.ranker.score(title, desc, location=job.location or "", loc_class=loc_class, posted=job.posted_at,
                             now=now, focus=board.focus, company=job.company or board.company or "")
    score = fit.score
    flags: list[str] = []
    if not desc:
        flags.append("description not read")
    if fit.years_note:
        flags.append(fit.years_note)
    if fit.phd:
        flags.append("PhD required")
    if fit.stretch:
        flags.append("stretch: senior-level title")
    elif fit.junior:
        flags.append("entry-level title")
    age_days = (now - job.posted_at).total_seconds() / 86400 if job.posted_at else None
    if age_days is not None and age_days > rules.stale_after_days:
        flags.append(f"open {int(age_days)} days")
    if loc_class == "unknown":
        flags.append("location unclear")
    if fit.contract:
        score -= 3
        flags.append("contract")
    if desc and _any(rules.e_verify, desc):
        flags.append("E-Verify")
    if desc and _any(rules.sponsors, desc):
        score += 2
        flags.append("sponsors visas")
    elif desc and _any(rules.no_sponsorship, desc):
        score -= 2
        flags.append("no visa sponsorship")
    if board.priority:
        score += 3
        flags.append("priority company")
    if _any(rules.staffing, job.company or board.company or ""):
        score -= 3
        flags.append("staffing/consulting firm")
    score = max(0, min(100, round(score)))
    years = fit.years
    blocker = blocker_hit(" ".join([title, desc]), rules)
    if age_days is not None and age_days > rules.max_post_age_days:
        status = "old"
    elif blocker:
        status = "blocked"
        flags.append(f"needs: {blocker[:40]}")
    elif loc_class == "non_us":
        status = "old"
    elif desc and fit.got <= 0:
        status = "blocked"
        flags.append("none of your tools in the job text")
    elif years is not None and rules.hide_if_years_at_least and years >= rules.hide_if_years_at_least:
        status = "blocked"
        flags.append(f"asks for {years}+ years of experience")
    elif years is not None and years >= rules.block_alerts_if_years_at_least:
        status = "quiet"
    elif score >= rules.alert_threshold:
        status = "alert"
    else:
        status = "quiet"
    return Assessment(category=category, location_class=loc_class, score=score, status=status, flags=flags,
                      years=years, blocker=blocker, has=fit.has[:10], missing=fit.missing[:6], detail=fit.detail)


def years_from_text(desc: str, degree: str = "master") -> int | None:
    """Years of experience a posting requires, read from its required / duties / other text only (a
    "5+ years preferred" line does not count), with degree paths and ranges handled by radar.rank."""
    from . import rank
    sec = rank.sections(desc)
    body = "\n".join(sec[k] for k in ("required", "duties", "other"))
    years, _ = rank.years_required(body, degree)
    return years


def location_from_text(desc: str) -> str:
    """'us' / 'non_us' / 'unknown' from a job's text, for postings whose location field is blank or just
    "Remote". Only clear one-sided signals count."""
    head = (desc or "")[:4000]
    us = _text_says_us(head) or bool(re.search(
        r"\b(anywhere in the (us|u\.s\.|united states)|us[- ]based|based in the (us|u\.s\.|united states)|"
        r"remote[^.\n]{0,20}\b(us|usa|u\.s\.)\b|eligible to work in the (us|u\.s\.|united states))", head, re.I))
    foreign = bool(NON_US.search(head)) or bool(re.search(r"\b(emea|apac|latam|europe|european union)\b", head, re.I))
    if us and not foreign:
        return "us"
    if foreign and not us:
        return "non_us"
    return "unknown"
