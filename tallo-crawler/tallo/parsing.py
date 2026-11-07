"""Pure parsing, eligibility signals, and scoring for a Tallo posting."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("’", "'")
    return "".join(
        "-" if unicodedata.category(char) == "Pd" or char == "−" else char
        for char in text
    )


def parse_posted(text: str, now: datetime) -> datetime | None:
    """Parse a Posted value. Calendar dates start at midnight in Chicago."""
    now = now.astimezone(UTC)
    punctuation = " \t:;,.*-!?()[]\"'"
    value = " ".join(_clean(text).strip(punctuation).split()).lower()
    value = re.sub(r"^posted\b[\s:;,.\-]*", "", value)
    value = value.strip(punctuation)
    if value == "just now":
        return now
    if value in {"today", "yesterday"}:
        local_date = now.astimezone(CHICAGO).date()
        if value == "yesterday":
            local_date -= timedelta(days=1)
        return datetime.combine(local_date, time.min, CHICAGO).astimezone(UTC)
    if match := re.fullmatch(r"(\d+)\s*\+?\s+(minute|hour|day|week)s?\s+ago", value):
        units = {"minute": "minutes", "hour": "hours", "day": "days", "week": "weeks"}
        try:
            return now - timedelta(**{units[match[2]]: int(match[1])})
        except (OverflowError, ValueError):
            return None
    value = re.sub(r"^([a-z]{3})\.(?=\s+\d)", r"\1", value)
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=CHICAGO).astimezone(UTC)
        except ValueError:
            continue
    return None


DEFAULT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "core": (
        r"\b(?:dev\s*-?\s*ops|dev\s*-?\s*sec\s*-?\s*ops|site reliability|sre|"
        r"kubernetes|observability|systems reliability)\b",
        r"\b(?:platform|infrastructure)\s+engineer\b",
        r"\bcloud\s+(?:engineer|operations|infrastructure|support engineer|administrator)\b",
    ),
    "bridge": (
        r"\b(?:systems? administrator|linux|unix|network operations|noc|network engineer|network administrator)\b",
        r"\b(?:cloud|technical|it|production) support engineer\b",
        r"\b(?:data[ -]?center technician|it operations|(?:build|release|build\s*[/&-]\s*release) engineer)\b",
        r"\bsystems? engineer\b.{0,40}\b(?:cloud|linux|it|infrastructure|network(?:ing)?|servers?)\b",
        r"\b(?:cloud|linux|it|infrastructure|network(?:ing)?|servers?)\b.{0,40}\bsystems? engineer\b",
    ),
    "excluded": (
        r"\b(?:senior|staff|principal|lead|manager|director|head of|vp|vice president|"
        r"architect|chief|distinguished|fellow)\b",
        r"\b(?:intern(?:ship)?|co[ -]?op|summer|sales|recruiter|marketing)\b",
        r"\b(?:mechanical|electrical|civil|manufacturing|maintenance|construction|hvac|facilities)\b",
    ),
}

_ROMAN_LEVELS = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}
_LEVEL = re.compile(
    r"\blevel\s+(?P<explicit>\d{1,2})\b"
    r"|(?<![/.])\b(?P<roman>viii|vii|iii|vi|iv|ix|ii|i|v|x)\b(?![/.]\w)"
    r"|(?<![\d/.])\b(?P<number>[1-9]\d?)\b(?=\+?\s*(?:$|[-,():]|remote\b|hybrid\b|on[ -]?site\b))",
    re.I,
)
_JUNIOR = re.compile(r"\b(?:junior|associate|entry(?:[ -]level)?|new grad(?:uate)?|graduate|early[ -]career)\b")
_TITLE_CLEARANCE = re.compile(r"\b(?:t\s*s\s*/\s*s\s*c\s*i|top[ -]secret|secret)\b", re.I)
_ENROLLMENT = re.compile(
    r"\b(?:must be |requires? (?:that you are |applicants? (?:to be )?)?)?"
    r"(?:currently enrolled|current enrollment|current (?:college |university )?students?)\b"
    r"|\benrollment (?:is )?required\b",
    re.I,
)
_ENROLLMENT_EXCEPTION = re.compile(
    r"\b(?:need not be|not required to be|do not (?:have|need) to be|no requirement to be)"
    r"\s+currently enrolled\b|\b(?:current )?enrollment (?:is )?not required\b",
    re.I,
)


@dataclass(frozen=True, slots=True)
class TitleMatch:
    family: Literal["core", "bridge"] | None
    level: int | None
    clearance_excluded: bool = False


def normalize_title(title: str) -> str:
    title = " ".join(_clean(title).lower().split())
    abbreviations = {
        "sr": "senior",
        "jr": "junior",
        "eng": "engineer",
        "admin": "administrator",
        "sysadmin": "systems administrator",
    }
    return re.sub(
        r"\b(sr|jr|eng|admin|sysadmin)\b\.?",
        lambda match: abbreviations[match[1]],
        title,
    )


def requires_current_enrollment(title: str, description: str) -> bool:
    if not re.search(r"\bapprentice(?:ship)?\b", normalize_title(title)):
        return False
    text = _ENROLLMENT_EXCEPTION.sub(" ", f"{title}\n{description}")
    return bool(_ENROLLMENT.search(text))


def classify_title(
    title: str, keywords: Mapping[str, Sequence[str]] | None = None
) -> TitleMatch:
    normalized = normalize_title(title)
    levels = []
    for match in _LEVEL.finditer(normalized):
        if match["roman"]:
            levels.append(_ROMAN_LEVELS[match["roman"]])
        else:
            levels.append(int(match["explicit"] or match["number"]))
    level = max(levels, default=1 if _JUNIOR.search(normalized) else None)
    patterns = DEFAULT_KEYWORDS if keywords is None else keywords
    if (level is not None and level >= 3) or requires_current_enrollment(title, ""):
        return TitleMatch(None, level)
    if any(re.search(pattern, normalized, re.I) for pattern in patterns.get("excluded", DEFAULT_KEYWORDS["excluded"])):
        return TitleMatch(None, level)

    clearance = _title_has_clearance(normalized)
    if any(re.search(pattern, normalized, re.I) for pattern in patterns.get("core", DEFAULT_KEYWORDS["core"])):
        return TitleMatch("core", level, clearance)
    if any(re.search(pattern, normalized, re.I) for pattern in patterns.get("bridge", DEFAULT_KEYWORDS["bridge"])):
        return TitleMatch("bridge", level, clearance)
    return TitleMatch(None, level)


_WORD_NUMBERS = {
    word: index
    for index, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen".split()
    )
}
_NUMBER = rf"(?:\d{{1,3}}|(?:{'|'.join(_WORD_NUMBERS)})(?:\s*\(\s*\d{{1,3}}\s*\))?)"
_YEARS = re.compile(
    rf"(?<![\w.])(?P<low>{_NUMBER})(?:\s*(?:\+|\bplus\b))?"
    rf"(?:\s*(?:-|\bto\b)\s*(?P<high>{_NUMBER})(?:\s*\+)?)?\s*(?:years?|yrs?)\b",
    re.I,
)
_EXPERIENCE = re.compile(r"\b(?:experience|exp)\b", re.I)
_PREFERRED_HEADING = (
    r"preferred(?:\s+(?:qualifications|requirements|experience|skills))?|"
    r"nice[ -]to[ -]have(?:\s+skills)?|bonus(?:\s+points)?|desired qualifications"
)
_REQUIRED_HEADING = (
    r"(?:required|minimum|basic|essential)(?:\s+(?:qualifications|requirements|experience|skills))?|"
    r"requirements|experience|what you(?:'ll| will) need|must[ -]haves?"
)
_NEUTRAL_HEADING = (
    r"qualifications|(?:about|why) (?:the role|us)|job description|"
    r"responsibilities|benefits|what we offer|equal opportunity"
)
_HEADING = re.compile(
    rf"^\s*(?:[#*•-]+\s*)?(?:(?P<preferred>{_PREFERRED_HEADING})|"
    rf"(?P<required>{_REQUIRED_HEADING})|(?P<neutral>{_NEUTRAL_HEADING}))\s*(?::\s*|$)",
    re.I,
)
_INLINE_HEADING = re.compile(
    rf"(?<!\S)(?:{_PREFERRED_HEADING}|{_REQUIRED_HEADING}|{_NEUTRAL_HEADING})\s*:",
    re.I,
)
_DEGREES = re.compile(
    r"\b(?:(?P<bachelor>bachelor(?:'s|s)?|b\.?s\.?c?|b\.?a\.?)"
    r"|(?P<graduate>master(?:'s|s)?|m\.?s\.?c?|m\.?a\.?|mba|ph\.?d\.?|doctor(?:al|ate)))\b",
    re.I,
)
_COMPANY_AGE = re.compile(
    r"^\s*(?:of\s+)?(?:in business|of history|of service|of operation|in operation|"
    r"since (?:we|our)|as (?:a company|an employer))\b",
    re.I,
)
_COMPANY_EXPERIENCE = re.compile(
    r"\b(?:we (?:bring|have|offer)|(?:our (?:company|team|founders?)|the company) "
    r"(?:brings?|has|have|offers?))\b.{0,45}$",
    re.I,
)
_PREF_BEFORE = re.compile(r"\b(?:preferred|desirable|nice[ -]to[ -]have|ideally)\b[\s:,-]*$", re.I)
_PREF_AFTER = re.compile(r"\b(?:preferred|desirable|a plus|nice[ -]to[ -]have)\b", re.I)
_DEGREE_EQUATION = re.compile(r"(?:\+|\bplus)\s*$", re.I)
_OR = re.compile(r"\bor\b", re.I)
_OR_LINE = re.compile(r"^\s*(?:[•*-]\s*)?or\b", re.I)
_BULLET = re.compile(r"^\s*(?:[-*•]+|\d+[.)])\s+")
_SENTENCE = re.compile(r"(?<!\b[A-Z])(?<=[!?])\s+|(?<=[.])\s+(?=[A-Z])")
_DOTTED_DEGREE = re.compile(r"\b(?:[bm]\.[as]\.|m\.b\.a\.|ph\.d\.)(?!\w)", re.I)
_ENTRY_LEVEL = re.compile(r"\bentry[ -]level\b", re.I)


def _qualification_lines(text: str) -> list[str]:
    lines: list[str] = []
    preferred = False
    experience_heading = False
    text = _DOTTED_DEGREE.sub(lambda match: match[0].replace(".", ""), _clean(text))
    for line in _INLINE_HEADING.sub(lambda match: f"\n{match[0]}", text).splitlines():
        if heading := _HEADING.match(line):
            preferred = bool(heading["preferred"])
            experience_heading = bool(re.search(r"\bexperience\b", heading[0], re.I))
            line = line[heading.end():]
        if preferred or not line.strip():
            continue
        line = _BULLET.sub("", line).strip()
        if experience_heading and not _EXPERIENCE.search(line):
            line = f"Experience: {line}"
        if lines and (_OR_LINE.match(line) or re.search(r"\bor\s*$", lines[-1], re.I)):
            lines[-1] += " " + line
        else:
            lines.append(line)
    return lines


def _number(text: str) -> int:
    return int(text) if text.isdecimal() else _WORD_NUMBERS[text.lower().partition("(")[0].strip()]


def _experience_candidates(clause: str) -> list[re.Match[str]]:
    markers = list(_EXPERIENCE.finditer(clause))
    candidates = list(_YEARS.finditer(clause))
    accepted = []
    for index, candidate in enumerate(candidates):
        start, end = candidate.span()
        before = clause[candidates[index - 1].end() if index else 0:start]
        after = clause[end:candidates[index + 1].start() if index + 1 < len(candidates) else len(clause)]
        if _COMPANY_AGE.search(after) or _COMPANY_EXPERIENCE.search(before):
            continue
        after_qualification = re.split(r";|\b(?:and|or|but)\b", after, maxsplit=1, flags=re.I)[0]
        if _PREF_BEFORE.search(before) or _PREF_AFTER.search(after_qualification):
            continue
        near_experience = any(
            max(start - marker.end(), marker.start() - end, 0) <= 80
            for marker in markers
        )
        degree_equation = _DEGREE_EQUATION.search(before) and _DEGREES.search(before)
        if near_experience or degree_equation:
            accepted.append(candidate)
    return accepted


def _required_for_clause(clause: str) -> list[int]:
    candidates = _experience_candidates(clause)
    if not candidates:
        return []
    requirements = []
    group_start = 0
    path_start = 0
    alternatives: list[tuple[int, bool]] = []
    for index, candidate in enumerate(candidates):
        path_end = len(clause)
        next_alternative = None
        if index + 1 < len(candidates):
            next_start = candidates[index + 1].start()
            next_alternative = _OR.search(clause, candidate.end(), next_start)
            path_end = next_alternative.start() if next_alternative else next_start
        path = clause[path_start:path_end]
        degrees = list(_DEGREES.finditer(path))
        graduate_only = bool(degrees) and all(degree["graduate"] for degree in degrees)
        alternatives.append((_number(candidate["low"]), graduate_only))
        if next_alternative:
            path_start = next_alternative.end()
            continue

        if index > group_start:
            bachelor_paths = [years for years, graduate in alternatives if not graduate]
            requirements.append(min(bachelor_paths or [years for years, _ in alternatives]))
        else:
            requirements.append(alternatives[0][0])
        group_start = index + 1
        path_start = candidate.end()
        alternatives.clear()
    return requirements


def extract_years(description: str) -> int | None:
    """Return the strongest required minimum across eligible alternatives."""
    lines = _qualification_lines(description)
    years = [
        value
        for line in lines
        for clause in _SENTENCE.split(line)
        for value in _required_for_clause(clause)
    ]
    if years:
        return max(years)
    return 0 if any(_ENTRY_LEVEL.search(line) for line in lines) else None


_US = r"(?:u\s*\.?\s*s\s*\.?|united states)"
_CITIZENS = (
    rf"{_US}\s+citizens?(?:\s+(?:and|or|&)\s+(?:(?:legal|lawful)\s+)?"
    r"(?:permanent residents?|green[ -]card holders?))?"
)
_GREEN_CARD = r"green[ -]card holders?"
_H1B = r"h\s*-?\s*1\s*-?\s*b"
_F1 = r"f\s*-?\s*1"
_STATUS = rf"(?:{_F1}\s+)?(?:stem\s+)?(?:opt|cpt)(?:\s*[/&]\s*(?:opt|cpt))?"
_RESIDENCY = re.compile(
    rf"\b(?:must be (?:an? )?{_US}\s+citizens?"
    rf"|{_US}\s+citizenship\s+(?:is\s+)?(?:required|mandatory)"
    rf"|requires?\s+{_US}\s+citizenship"
    rf"|(?:only\s+(?:{_CITIZENS}|{_US}\s+persons?|{_GREEN_CARD})"
    rf"|(?:{_CITIZENS}|{_US}\s+persons?|{_GREEN_CARD})\s+only))\b",
    re.I,
)
_EXPORT = re.compile(
    r"\bitar\b|^\W*ear\W*$"
    r"|\bear\b(?=\s+(?:export|restrictions?|compliance|requirements?|controls?|eligibility))"
    r"|\b(?:export[ -]control|subject to|governed by|comply with)\b.{0,35}\bear\b",
    re.I,
)
_CLEARANCE = re.compile(
    r"\b(?:security clearance|active clearance|top[ -]secret|ts\s*/\s*sci|public trust|dod)\b"
    r"|\b(?:ability|eligible|eligibility)\b[^.;\n]{0,25}\bobtain\s+(?:a\s+)?clearance\b"
    r"|\bclearance\s+(?:is\s+)?(?:required|mandatory)\b"
    r"|\bclearance\s*[:-]\s*secret\b|^\W*secret\W*$"
    r"|\bsecret\s+(?:is\s+)?(?:required|mandatory|clearance|eligibility)\b"
    r"|\b(?:obtain|maintain|hold|requires?|active)\s+(?:an?\s+)?secret\b",
    re.I,
)
_CLEARANCE_NAME = (
    r"(?:an?\s+)?(?:(?:(?:security|active|dod)\s+)?clearance"
    r"|(?:top[ -]secret|secret|public trust|ts\s*/\s*sci)(?:\s+(?:security\s+)?clearance)?)"
)
_CLEARANCE_EXCEPTION = re.compile(
    rf"\bno\s+{_CLEARANCE_NAME}\s+(?:is\s+)?required\b"
    rf"|\b{_CLEARANCE_NAME}\s+(?:is\s+)?not\s+(?:required|necessary)\b"
    rf"|\b(?:do(?:es)?\s+not|doesn't|don't)\s+require\s+{_CLEARANCE_NAME}\b",
    re.I,
)
_REQUIRED_CLEARANCE = re.compile(
    r"\b(?:security |active |secret |top[ -]secret )?clearance\s+(?:is\s+)?(?:required|mandatory)\b"
    r"|\b(?:must|ability to|able to)\s+(?:obtain|hold|maintain)\s+(?:an?\s+)?"
    r"(?:(?:active|security)\s+)?clearance\b",
    re.I,
)
_C2C = r"(?:c2c|corp[ -]to[ -]corp)"
_C2C_SCOPE = re.compile(
    rf"\b(?:for|via|on|under)\s+(?:an?\s+)?{_C2C}\b|^\W*{_C2C}\s*(?::|only\b)",
    re.I,
)
_NO_SPONSORSHIP = re.compile(
    rf"\b(?:unable to|not able to|cannot|can not|can't|will not|won't|do not|does not)\s+"
    r"(?:sponsor\b|(?:provide|offer|support)\s+"
    rf"(?:(?:visa|employment|work|employer|immigration|{_H1B})\s+){{0,3}}sponsorship\b)"
    rf"|\bno\s+(?:(?:work|employment|employer|visa|{_H1B})\s+){{0,3}}sponsorship\b"
    rf"|\bsponsorship\s+(?:is\s+|will\s+)?not\s+(?:available|offered|provided|supported)\b"
    r"|\bwithout\s+(?:(?:requiring|the need for|need for|any|current|future|visa|employment|employer|work)"
    r"\s+){0,6}sponsorship\b"
    rf"|\bno\s+(?:{_STATUS}|{_H1B}(?:\s+(?:transfers?|sponsorship))?)\b"
    rf"|\b{_STATUS}\s+(?:is\s+|are\s+)?not\s+(?:accepted|supported|eligible|permitted)\b",
    re.I,
)
_OPT = re.compile(rf"\b(?:{_STATUS}\s+(?:are\s+)?welcome|{_F1}\s+opt|stem\s+opt)\b", re.I)
_E_VERIFY = re.compile(r"\be\s*-?\s*verify\b", re.I)
_SPONSOR = re.compile(rf"\b(?:visa sponsorship\s+(?:is\s+)?available|will sponsor|{_H1B}\s+sponsorship)\b", re.I)
_W2 = re.compile(r"\bw\s*-?\s*2\s+only\b|\bonly\s+w\s*-?\s*2\b", re.I)
_STAFFING = re.compile(rf"\b(?:consulting|staffing|our client|{_C2C})\b", re.I)
_CLAUSE = re.compile(r"[;\n]+|(?<=[.!?])\s+|\s+\b(?:but|however|whereas)\b\s+", re.I)


@dataclass(frozen=True, slots=True)
class Authorization:
    blocked: bool
    no_sponsorship: bool
    opt_friendly: bool
    sponsorship: bool
    flags: tuple[str, ...]


def _title_has_clearance(title: str) -> bool:
    return bool(_TITLE_CLEARANCE.search(title) and not _CLEARANCE_EXCEPTION.search(title))


def _clearance_in_clause(clause: str) -> bool:
    if not _CLEARANCE_EXCEPTION.search(clause):
        return bool(_CLEARANCE.search(clause))
    return bool(_REQUIRED_CLEARANCE.search(_CLEARANCE_EXCEPTION.sub(" ", clause)))


def classify_authorization(title: str, description: str, company: str = "") -> Authorization:
    combined = re.sub(r"\bu\.\s*s\.(?!\w)", "US", _clean(f"{title}\n{description}"), flags=re.I)
    clauses = _CLAUSE.split(combined)
    clearance = _title_has_clearance(_clean(title)) or any(map(_clearance_in_clause, clauses))
    restrictions = [clause for clause in clauses if _RESIDENCY.search(clause)]
    c2c = any(_C2C_SCOPE.search(clause) for clause in restrictions)
    residency = any(not _C2C_SCOPE.search(clause) for clause in restrictions)
    export = bool(any(map(_EXPORT.search, clauses)) or re.search(r"\bEAR\b", combined))
    no_sponsorship = False
    opt = False
    sponsor = False
    for clause in clauses:
        # A positive term inside a negative match (for example, "no STEM OPT")
        # does not count as employer support.
        support_text, negatives = _NO_SPONSORSHIP.subn(" ", clause)
        no_sponsorship |= bool(negatives)
        opt |= bool(_OPT.search(support_text))
        sponsor |= bool(_SPONSOR.search(support_text))

    everify = bool(_E_VERIFY.search(combined))
    flags = tuple(
        flag for present, flag in (
            (clearance, "clearance"),
            (residency, "citizenship/residency restriction"),
            (export, "export control"),
            (no_sponsorship, "no sponsorship"),
            (everify, "E-Verify"),
            (opt, "OPT-friendly"),
            (sponsor, "sponsorship available"),
            (bool(_W2.search(combined)), "W2 only"),
            (c2c, "C2C restriction"),
            (bool(_STAFFING.search(f"{company}\n{combined}")), "staffing firm: verify"),
        ) if present
    )
    return Authorization(clearance or residency or export, no_sponsorship, everify or opt, sponsor, flags)


_STATE_PAIRS = (
    "AL:Alabama|AK:Alaska|AZ:Arizona|AR:Arkansas|CA:California|CO:Colorado|CT:Connecticut|"
    "DE:Delaware|DC:District of Columbia|FL:Florida|GA:Georgia|HI:Hawaii|ID:Idaho|IL:Illinois|"
    "IN:Indiana|IA:Iowa|KS:Kansas|KY:Kentucky|LA:Louisiana|ME:Maine|MD:Maryland|MA:Massachusetts|"
    "MI:Michigan|MN:Minnesota|MS:Mississippi|MO:Missouri|MT:Montana|NE:Nebraska|NV:Nevada|"
    "NH:New Hampshire|NJ:New Jersey|NM:New Mexico|NY:New York|NC:North Carolina|ND:North Dakota|"
    "OH:Ohio|OK:Oklahoma|OR:Oregon|PA:Pennsylvania|RI:Rhode Island|SC:South Carolina|SD:South Dakota|"
    "TN:Tennessee|TX:Texas|UT:Utah|VT:Vermont|VA:Virginia|WA:Washington|WV:West Virginia|"
    "WI:Wisconsin|WY:Wyoming|PR:Puerto Rico|GU:Guam|VI:US Virgin Islands|AS:American Samoa|"
    "MP:Northern Mariana Islands"
)
_STATES = dict(pair.lower().split(":") for pair in _STATE_PAIRS.split("|"))
_AMBIGUOUS_COUNTRIES = {"ca": "canada", "in": "india", "de": "germany", "il": "israel"}
_STANDALONE_STATES = _STATES.keys() - _AMBIGUOUS_COUNTRIES.keys()
_FOREIGN = re.compile(
    r"\b(?:canada|mexico|united kingdom|uk|england|europe|germany|india|australia|"
    r"philippines|ireland|pakistan|singapore|japan|china|brazil|poland|france|spain|israel|"
    r"netherlands|south africa|new zealand|nigeria|argentina|kenya|emea|apac|latin america|"
    r"british columbia|quebec|alberta|manitoba|saskatchewan|nova scotia|new brunswick)\b"
    r"|,\s*(?:on|bc|qc|ab|mb|sk|ns|nb|pe|nl|yt|nt|nu)\s*(?:,|$)",
    re.I,
)
_COMMON_US_CITIES = set(
    "atlanta|austin|baltimore|boston|buffalo|charlotte|chicago|cincinnati|cleveland|columbus|"
    "dallas|denver|detroit|houston|indianapolis|jacksonville|kansas city|las vegas|los angeles|"
    "miami|milwaukee|minneapolis|nashville|new orleans|new york city|oklahoma city|omaha|orlando|"
    "philadelphia|phoenix|pittsburgh|portland|raleigh|sacramento|salt lake city|san antonio|"
    "san diego|san francisco|san jose|seattle|st. louis|tampa|tucson".split("|")
)
_COUNTRY_US = re.compile(r"(?i:\b(?:united states(?: of america)?|usa)\b|\bu\.s\.(?:a\.)?(?!\w))|\bUS\b")
_LOCATION_SPLIT = re.compile(r"\s*[/;|\n]+\s*|\s+(?:and|or)\s+(?=\S)", re.I)
_STATE_NAMES_PATTERN = "|".join(map(re.escape, _STATES.values()))
_CITY_STATE = re.compile(
    rf"(?P<city>[^,()]+),\s*(?P<state>[a-z]{{2}}|{_STATE_NAMES_PATTERN})"
    r"(?=\s*(?:,|$|\d{5}\b))",
    re.I,
)
_STATE_NAME = re.compile(
    rf"(?:^|,)\s*(?:{_STATE_NAMES_PATTERN})(?=\s*(?:,|$))",
    re.I,
)
_WORK_TYPE = re.compile(r"\b(?:hybrid|on[ -]?site|remote)\b", re.I)
_NEGATIVE_US = re.compile(r"\b(?:non[- ]|(?:outside|except)\s+(?:of\s+)?(?:the\s+)?)(?:u\.?s\.?|united states)\b", re.I)


@dataclass(frozen=True, slots=True)
class Location:
    is_us: bool
    unclear: bool
    work_type: str


def _has_us_city_state(part: str) -> bool:
    for match in _CITY_STATE.finditer(part):
        state = match["state"].lower()
        if (state not in _STATES and state not in _STATES.values()) or _FOREIGN.search(match["city"]):
            continue
        # A foreign country immediately after a city/state-like pair qualifies
        # that pair ("Munich, DE, Germany"), not another city later in the list.
        following = part[match.end():].lstrip(", ").split(",", 1)[0].lower().strip()
        if following != _AMBIGUOUS_COUNTRIES.get(state):
            return True
    return False


def classify_location(location: str, city: str = "", state: str = "") -> Location:
    text = _clean(location)
    work_types = {match[0].lower().replace("-", "").replace(" ", "") for match in _WORK_TYPE.finditer(text)}
    work_type = next((kind.capitalize() for kind in ("hybrid", "remote", "onsite") if kind in work_types), "")
    display = _WORK_TYPE.sub("", text)
    display = re.sub(r"\bd\.\s*c\.(?!\w)", "DC", display, flags=re.I)
    # Make a US country label independent of a companion location in parentheses.
    display = display.replace("(", ",").replace(")", " ")
    hint = " ".join(_clean(city).lower().replace("-", " ").split())
    hint_us = state.lower() in _STATES or state.lower() in _STATES.values()
    has_foreign = False
    for part in _LOCATION_SPLIT.split(display):
        part = " ".join(part.strip(" ,-").split())
        if not part:
            continue
        foreign = bool(_FOREIGN.search(part))
        has_foreign |= foreign
        country_us = bool(_COUNTRY_US.search(part) or part.lower() in {"us", "u.s.", "usa", "u.s.a."})
        if (country_us and not _NEGATIVE_US.search(part)) or _has_us_city_state(part):
            return Location(True, False, work_type)
        # Explicitly foreign cities followed by a country are handled before
        # resolving a US state abbreviation.
        if foreign:
            continue
        lowered = part.lower()
        if (
            lowered in _STANDALONE_STATES
            or lowered in _COMMON_US_CITIES
            or _STATE_NAME.search(part)
            or (hint_us and lowered == hint and lowered not in _AMBIGUOUS_COUNTRIES)
        ):
            return Location(True, False, work_type)
    if work_type == "Remote" and not has_foreign and not _NEGATIVE_US.search(display):
        return Location(True, True, work_type)
    return Location(False, False, work_type)


@dataclass(frozen=True, slots=True)
class ScoreResult:
    score: int
    reasons: tuple[str, ...]
    flags: tuple[str, ...]
    status: Literal["blocked", "too_senior", "alert", "quiet"]


def score_job(
    title: TitleMatch,
    years: int | None,
    posted: datetime | None,
    now: datetime,
    location: Location,
    authorization: Authorization,
    alert_threshold: int = 45,
) -> ScoreResult:
    """Apply score weights to an in-window US posting and explain each term."""
    points: list[tuple[str, int]] = []
    flags = list(authorization.flags)
    if title.family:
        points.append((f"{title.family} title", 45 if title.family == "core" else 30))
    levels = {None: ("level unspecified", 5), 1: ("junior/level I", 15), 2: ("level II", 0)}
    points.append(levels.get(title.level, ("level III or higher", 0)))

    if years is None:
        flags.append("years unknown")
        points.append(("years unknown", 0))
    else:
        points.append((f"{years} required years", 10 if years <= 2 else -5 if years <= 4 else 0))
    if posted is None:
        flags.append("date unknown")
        points.append(("date unknown", -3))
    else:
        age = now - posted
        if timedelta() <= age <= timedelta(hours=24):
            points.append(("posted within 24h", 15))
        elif timedelta(hours=24) < age <= timedelta(hours=48):
            points.append(("posted 24-48h ago", 8))

    if location.work_type.lower() in {"onsite", "hybrid"}:
        points.append((location.work_type, 5))
    elif location.work_type.lower() == "remote":
        points.append((location.work_type, -3))
    if authorization.opt_friendly:
        points.append(("E-Verify/OPT-friendly", 5))
    if authorization.sponsorship:
        points.append(("sponsorship available", 5))
    if authorization.no_sponsorship:
        points.append(("no sponsorship", -15))
    if location.unclear:
        flags.append("location unclear")
        points.append(("location unclear", -5))
    if "staffing firm: verify" in flags:
        points.append(("staffing firm", -5))

    raw = sum(amount for _, amount in points)
    score = max(0, min(100, raw))
    if score != raw:
        points.append(("score clamped", score - raw))
    if authorization.blocked or title.clearance_excluded:
        status = "blocked"
    elif years is not None and years >= 5:
        status = "too_senior"
    else:
        status = "alert" if score >= alert_threshold else "quiet"
    return ScoreResult(score, tuple(f"{reason} {amount:+d}" for reason, amount in points), tuple(flags), status)
