"""Rule-based fit score for a job against one resume: no AI, no outside service.

It reads a job description the way a recruiter's screening software does:

1. Drops boilerplate (about the company, benefits, equal-opportunity text).
2. Splits the rest into sections: required, preferred, responsibilities, other. A skill named under
   "required" counts fully, under "preferred" half, in boilerplate not at all.
3. Looks up every skill of a taxonomy (profiles/<name>/skills.yaml), including skills the resume does
   NOT have, so a job that asks for Simulink *and* Linux kernel drivers *and* FPGA scores lower than one
   that asks for Simulink and AUTOSAR. Result: the resume skills the job asks for ("has") and the
   required ones the resume lacks ("missing").
4. Reads years of experience the way they are written ("BS with 5 years or MS with 3": the candidate's
   degree decides which applies).
5. Grades the title (target role, neighbour, generic), the industry, freshness and location.
6. Adds a corpus-weighted word overlap between the resume and the job (TF-IDF): words that are rare
   across all collected postings (TargetLink, Polyspace) count far more than common ones.

Score = weighted sum of those parts, 0-100. Every part is listed in the "detail" column.
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

import yaml

# ---------------------------------------------------------------- sections

_PREFERRED = re.compile(
    r"^(?:preferred|desired|desirable|additional|bonus|nice[- ]to[- ]haves?|optional|pluses|a plus|it would be "
    r"(?:great|nice)(?: if you had)?|ideally|you might also have|extra credit|good to have)"
    r"(?:\s+(?:qualifications?|skills?|experience|requirements?|attributes|knowledge|points|but not required|"
    r"\(?not required\)?|to have))*", re.I)
_REQUIRED = re.compile(
    r"^(?:(?:basic|minimum|required|must[- ]haves?|essential|mandatory|key|core|technical)\b.{0,30}"
    r"(?:qualification|requirement|skill|experience|competenc|knowledge)|requirements|qualifications|"
    r"what (?:you(?:'ll| will)?|we(?:'re| are)?) (?:need|bring|require|looking for|expect)|what you (?:have|bring)|"
    r"you (?:have|bring|will have|should have)|who you are|your (?:profile|background|experience|qualifications|skills)|"
    r"(?:skills|experience|education|knowledge)(?: (?:and|&) (?:experience|skills|education|abilities|qualifications))?\b|"
    r"job requirements|position requirements|candidate profile|about you)", re.I)
_DUTIES = re.compile(
    r"^(?:responsibilit|what you(?:'ll| will)? (?:do|work on|be doing|own)|your (?:role|responsibilities|impact|"
    r"mission|day)|the (?:role|job|position|opportunity)|job (?:duties|description|summary|overview|purpose)|"
    r"key (?:responsibilities|duties|accountabilities|tasks)|duties|essential (?:duties|functions)|in this role|"
    r"day[- ]to[- ]day|position (?:summary|overview|description|purpose)|about (?:the|this) (?:role|job|position|"
    r"team|opportunity)|role (?:summary|overview|description)|overview|summary|description|tasks|"
    r"what (?:the role|this role) (?:involves|offers))", re.I)
_BOILER = re.compile(
    r"^(?:about (?:us|the company|our company|[\w&.,'’ -]{2,40})|who we are|our (?:company|story|mission|culture|"
    r"values|commitment|benefits|team)|why (?:join|work|you'll love)|what we offer|we offer|benefits|perks|"
    r"compensation|pay (?:range|transparency)|salary|total rewards|equal (?:employment )?opportunit|eeo\b|"
    r"accommodation|disclaimer|privacy|e-?verify|diversity|notice to|life at|working at|additional information|"
    r"physical (?:demands|requirements)|work environment|travel|legal|recruit(?:ment|ing) (?:fraud|scam))", re.I)
_BOILER_LINE = re.compile(
    r"equal (?:employment )?opportunity|without regard to|race,? colou?r|veteran status|sexual orientation|"
    r"reasonable accommodation|401\(?k\)?|paid time off|medical,? dental|dental,? (?:and )?vision|tuition "
    r"reimbursement|background check|drug (?:test|screen)|pay transparency|salary range|compensation range|"
    r"benefits (?:include|package)|bonus eligib|employee assistance|parental leave|we are proud to be|"
    r"applicants? (?:will|must) receive|by applying|privacy (?:notice|policy)", re.I)
_INLINE_HEADER = re.compile(
    r"(?i)(?<=[.!?;:)\s])((?:basic|minimum|required|preferred|desired|additional)\s+(?:qualifications|requirements|"
    r"skills)|(?:key )?responsibilities|qualifications|requirements|what you(?:'ll| will)? (?:do|need|bring)|"
    r"nice[- ]to[- ]have|benefits|about us|who you are|what we offer)\s*:")

SECTION_WEIGHT = {"required": 1.0, "duties": 0.8, "other": 0.7, "preferred": 0.5, "boiler": 0.0}


def sections(text: str) -> dict[str, str]:
    """Split a plain-text job description into required / preferred / duties / other / boiler."""
    t = (text or "").replace("\r", "\n")
    t = _INLINE_HEADER.sub(lambda m: "\n" + m.group(1) + ":\n", t)
    out = {k: [] for k in SECTION_WEIGHT}
    cur = "other"
    for raw in t.split("\n"):
        line = raw.strip().strip(" \t•*-–—·▪◦●").strip()
        if not line:
            continue
        head, sep, rest = line.partition(":")
        if sep and len(head.split()) <= 8 and _header_kind(head):
            cur = _header_kind(head)
            line = rest.strip()
            if not line:
                continue
        elif not sep and len(line.split()) <= 8 and _header_kind(line):
            cur = _header_kind(line)
            continue
        if _BOILER_LINE.search(line):
            out["boiler"].append(line)
            continue
        out[cur].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def _header_kind(s: str) -> str | None:
    """The section a short line names, if the line is a header (the match must cover nearly all of it, so
    "Experience with Simulink" is content while "Experience" or "Basic Qualifications" is a header)."""
    s = s.strip().rstrip(":").strip()
    if not s or len(s) > 70:
        return None
    for kind, rx in (("preferred", _PREFERRED), ("duties", _ABOUT_ROLE), ("boiler", _BOILER),
                     ("required", _REQUIRED), ("duties", _DUTIES)):
        m = rx.search(s)
        if m and m.end() >= len(s) - 12:
            return kind
    return None


_ABOUT_ROLE = re.compile(r"^about (?:the|this) (?:role|job|position|team|opportunity)", re.I)


# ---------------------------------------------------------------- years of experience

_YEARS = re.compile(r"(?:at least|minimum(?: of)?|min\.?)?\s*\(?(\d{1,2})\)?\s*(?:\+|plus)?\s*"
                    r"(?:(?:-|–|to)\s*(\d{1,2})\s*)?\+?\s*(?:years?|yrs?)\b(?:'s)?", re.I)
_DEG = [
    ("phd", re.compile(r"\bph\.?\s?d\b|\bdoctora(?:te|l)\b", re.I)),
    ("master", re.compile(r"\bmaster'?s?\b|\bm\.?\s?s\.?(?=[\s,;)/])|\bmsc\b|\bm\.?\s?eng\b|\bmsee\b|\bmsme\b|"
                          r"\bgraduate degree\b|\badvanced degree\b", re.I)),
    ("bachelor", re.compile(r"\bbachelor'?s?\b|\bb\.?\s?s\.?(?=[\s,;)/])|\bbsc\b|\bb\.?\s?eng\b|\bbsee\b|\bbsme\b|"
                            r"\bbsce\b|\bundergraduate\b|\bb\.?\s?tech\b|\bfour[- ]year degree\b", re.I)),
]
_EXP_WORD = re.compile(r"experience|exp\b|working|work in|industry|professional|relevant|hands-on|background", re.I)


def years_required(text: str, degree: str = "master") -> tuple[int | None, str]:
    """(years the candidate must have, note). `degree` is the candidate's highest degree."""
    found: list[tuple[int, str]] = []
    for m in _YEARS.finditer(text or ""):
        n = int(m.group(1))
        after = text[m.end(): m.end() + 70]
        before = text[max(0, m.start() - 40): m.start()]
        if n > 15 or not (_EXP_WORD.search(after) or _EXP_WORD.search(before)):
            continue
        if re.match(r"\s*(?:old|of age|ago|warranty|in business|history)", after, re.I):
            continue
        found.append((n, _degree_of(text, m.start(), m.end())))
    if not found:
        return None, ""
    by = {k: [n for n, d in found if d == k] for k in ("phd", "master", "bachelor", "")}
    if degree == "master" and by["master"]:
        n = min(by["master"])
        note = f"{n}+ yrs (MS path)" if by["bachelor"] or by[""] else f"{n}+ yrs"
        return n, note
    base = by["bachelor"] + by[""]
    if base:
        n = max(base)
        return n, f"{n}+ yrs"
    if by["phd"]:   # only a PhD path is stated
        n = min(by["phd"])
        return n, f"{n}+ yrs (PhD path)"
    return None, ""


def _degree_of(text: str, start: int, end: int) -> str:
    before = text[max(0, start - 90): start]
    best, pos = "", -1
    for name, rx in _DEG:
        for m in rx.finditer(before):
            if m.start() > pos and not re.search(r"[.;]\s", before[m.end():]):   # same sentence/clause
                best, pos = name, m.start()
    if best:
        # "Bachelor's or Master's degree with 3 years": several degrees, one number -> it applies to all
        return best if not re.search(r"\bor\b", before[pos:]) else ""
    after = text[end: end + 50]
    for name, rx in _DEG:
        if rx.search(after) and not re.search(r"[.;]\s", after[:rx.search(after).start()]):
            return name
    return ""


def phd_required(sec: dict[str, str]) -> bool:
    req = sec.get("required", "")
    return bool(re.search(r"\bph\.?\s?d\b[^.\n]{0,40}\b(required|is required|in|degree)", req, re.I)) and not \
        re.search(r"\b(bachelor|master|b\.?s\.?|m\.?s\.?)\b", req, re.I)


# ---------------------------------------------------------------- word overlap (TF-IDF)

_TOK = re.compile(r"[a-z][a-z0-9+#/.-]*[a-z0-9+#]|[a-z]")
_STOP = set("""a an the and or of to in for on with by at as is are be been being this that these those it its our
we you your they their from will can may must should would could have has had not no do does did all any each
other such than then there here into over under about above below more most less very via per across within
including include includes etc using use used work working team teams role job position company new strong good
ability able knowledge understanding experience years year skills skill required preferred plus related
including e.g i.e""".split())


def tokens(text: str) -> list[str]:
    return [t.strip(".-/") for t in _TOK.findall((text or "").lower()) if t not in _STOP and len(t) > 1]


class Corpus:
    """Document frequencies over every stored job text, for TF-IDF weights."""

    def __init__(self, texts=()):
        self.df: Counter = Counter()
        self.n = 0
        for t in texts:
            self.add(t)

    def add(self, text: str) -> None:
        self.n += 1
        self.df.update(set(tokens(text)))

    def idf(self, tok: str) -> float:
        return math.log((self.n + 1) / (self.df.get(tok, 0) + 1)) + 1.0

    def vector(self, text: str) -> dict[str, float]:
        tf = Counter(tokens(text))
        v = {t: (1 + math.log(c)) * self.idf(t) for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    def similarity(self, a: dict[str, float], text: str) -> tuple[float, list[str]]:
        b = self.vector(text)
        common = sorted(((a[t] * b[t], t) for t in a.keys() & b.keys()), reverse=True)
        return sum(x for x, _ in common), [t for _, t in common[:6]]


# ---------------------------------------------------------------- taxonomy

@dataclass
class Skill:
    name: str
    rx: re.Pattern
    weight: float       # how central the skill is to this kind of job (1-3)
    have: float         # how well the resume shows it (0 = not at all, 1 = clearly)
    group: str = ""


@dataclass
class Ranker:
    skills: list[Skill]
    titles: list[tuple[float, re.Pattern]]
    domains: list[tuple[float, re.Pattern]]
    profile_vec: dict[str, float]
    profile_text: str
    corpus: Corpus
    degree: str = "master"
    years: int = 4
    weights: dict = field(default_factory=dict)
    home: list[re.Pattern] = field(default_factory=list)
    stretch: list[re.Pattern] = field(default_factory=list)
    entry: list[re.Pattern] = field(default_factory=list)
    contract: re.Pattern = re.compile(r"\b(contract|contractor|c2c|corp[- ]to[- ]corp|w2 contract|temporary|temp to hire|"
                                      r"contract[- ]to[- ]hire|\d+\s*(?:month|mo)s?\s+contract)\b", re.I)

    @classmethod
    def load(cls, cfg: dict, base_dir: str, texts=()) -> "Ranker | None":
        rc = cfg.get("ranking") or {}
        path = os.path.join(base_dir, rc.get("taxonomy", "skills.yaml"))
        if not os.path.exists(path):
            return None
        tx = yaml.safe_load(open(path, encoding="utf-8")) or {}
        skills = []
        for group, items in (tx.get("skills") or {}).items():
            for name, spec in (items or {}).items():
                spec = spec if isinstance(spec, dict) else {"re": spec}
                skills.append(Skill(name=name, rx=re.compile(spec["re"], re.I), weight=float(spec.get("weight", 2)),
                                    have=float(spec.get("have", 0)), group=group))
        titles = [(float(t["score"]), re.compile(p, re.I)) for t in tx.get("titles") or [] for p in t["patterns"]]
        domains = [(float(d["score"]), re.compile(p, re.I)) for d in tx.get("domains") or [] for p in d["patterns"]]
        profile = str(rc.get("profile_text") or (cfg.get("ai") or {}).get("resume") or "")
        s = cfg.get("scoring", {})
        corpus = Corpus(texts)
        r = cls(skills=skills, titles=titles, domains=domains, profile_vec={}, profile_text=profile, corpus=corpus,
                degree=str(rc.get("degree", "master")), years=int(s.get("years_experience", 4)),
                weights={**DEFAULT_WEIGHTS, **(rc.get("weights") or {})},
                home=[re.compile(x, re.I) for x in s.get("home_locations") or []],
                stretch=[re.compile(x, re.I) for x in (cfg.get("titles") or {}).get("stretch") or []],
                entry=[re.compile(x, re.I) for x in (cfg.get("titles") or {}).get("entry") or []])
        r.refresh_profile()
        return r

    def refresh_profile(self) -> None:
        """Recompute the resume's word weights after the corpus changed."""
        names = " ".join(s.name for s in self.skills if s.have >= 0.5)
        self.profile_vec = self.corpus.vector(self.profile_text + "\n" + names)

    # ---- scoring
    def score(self, title: str, desc: str, *, location: str = "", loc_class: str = "us", posted: datetime | None = None,
              now: datetime | None = None, focus: bool = False, company: str = "") -> "Fit":
        sec = sections(desc)
        weighted = [(sec[k], SECTION_WEIGHT[k]) for k in ("required", "duties", "other", "preferred")]
        has_sections = bool(sec["required"] or sec["preferred"])
        hits: dict[str, tuple[Skill, float]] = {}
        for sk in self.skills:
            best = 1.0 if sk.rx.search(title or "") else 0.0
            for text, w in weighted:
                if w > best and text and sk.rx.search(text):
                    best = w
            if best > 0:
                hits[sk.name] = (sk, best)
        asked = sum(sk.weight * m for sk, m in hits.values())
        got = sum(sk.weight * m * sk.have for sk, m in hits.values())
        coverage = got / asked if asked else 0.0
        strength = min(1.0, got / float(self.weights["strength_full"]))
        skills_part = 0.55 * coverage + 0.45 * strength if asked else 0.0
        has = [n for n, (sk, m) in sorted(hits.items(), key=lambda kv: -kv[1][0].weight * kv[1][1] * kv[1][0].have)
               if sk.have >= 0.5]
        req_level = 1.0 if has_sections else 0.7
        missing = [n for n, (sk, m) in sorted(hits.items(), key=lambda kv: -kv[1][0].weight)
                   if sk.have < 0.5 and m >= req_level and sk.weight >= 1.5]

        # title
        t = " " + (title or "") + " "
        title_part = max((s for s, rx in self.titles if rx.search(t)), default=0.45)
        stretch = any(rx.search(t) for rx in self.stretch)
        junior = any(rx.search(t) for rx in self.entry)
        if stretch:
            title_part -= 0.2
        elif junior:
            title_part -= 0.1

        # years
        body = "\n".join(sec[k] for k in ("required", "duties", "other", "preferred"))
        yrs, ynote = years_required(body, self.degree)
        over = (yrs - self.years) if yrs is not None else None
        if over is None:
            exp_part = 0.8                                   # not stated
        elif over <= 1:
            exp_part = 1.0                                   # up to one year more than the candidate has
        else:
            exp_part = max(0.1, 1.0 - 0.25 * (over - 1))     # 2 more: 0.75, 3 more: 0.5, 4 more: 0.25
        phd = phd_required(sec)
        if phd:
            exp_part = min(exp_part, 0.3)

        # industry
        dom_text = f"{title}\n{body}\n{company}"
        domain_part = max((s for s, rx in self.domains if rx.search(dom_text)), default=0.7)
        if focus:
            domain_part = max(domain_part, 0.85)

        # overlap with the resume's words
        sim, common = self.corpus.similarity(self.profile_vec, f"{title}\n{body}") if self.corpus.n >= 30 else (0.0, [])
        sim_part = min(1.0, sim / float(self.weights["similarity_full"]))

        # freshness and place
        age = (now - posted).days if (posted and now) else None
        fresh = 0.7 if age is None else 1.0 if age <= 3 else 0.9 if age <= 7 else 0.75 if age <= 30 else 0.6 if age <= 60 else 0.45
        place = 1.0 if any(rx.search(location or "") for rx in self.home) else 0.5 if loc_class == "unknown" else 0.8
        logistics = 0.5 * fresh + 0.5 * place

        w = self.weights
        if self.corpus.n < 30:   # too few stored postings for word weights: give that share to skills
            w = {**w, "skills": w["skills"] + w["similarity"], "similarity": 0.0}
        total = (w["skills"] * skills_part + w["title"] * title_part + w["experience"] * exp_part +
                 w["domain"] * domain_part + w["similarity"] * sim_part + w["logistics"] * logistics)
        score = 100.0 * total / sum(w[k] for k in ("skills", "title", "experience", "domain", "similarity", "logistics"))
        contract = bool(self.contract.search(f"{title}\n{sec['required']}\n{sec['other']}\n{sec['duties']}"))
        detail = (f"skills {skills_part:.2f} (covers {coverage:.0%} of what it asks) · title {title_part:.2f} · "
                  f"experience {exp_part:.2f} · industry {domain_part:.2f} · overlap {sim_part:.2f} · "
                  f"freshness/place {logistics:.2f}")
        parts = {"skills": skills_part, "title": title_part, "experience": exp_part, "domain": domain_part,
                 "similarity": sim_part, "logistics": logistics}
        return Fit(score=score, has=has, missing=missing, years=yrs, years_note=ynote, coverage=coverage,
                   got=got, asked=asked, stretch=stretch, junior=junior, phd=phd, contract=contract,
                   detail=detail, common=common, parts=parts)


DEFAULT_WEIGHTS = {"skills": 0.42, "title": 0.15, "experience": 0.15, "domain": 0.10, "similarity": 0.08,
                   "logistics": 0.10, "strength_full": 14, "similarity_full": 0.30}


@dataclass
class Fit:
    score: float
    has: list[str]
    missing: list[str]
    years: int | None
    years_note: str
    coverage: float
    got: float
    asked: float
    stretch: bool
    junior: bool
    phd: bool
    contract: bool
    detail: str
    common: list[str]
    parts: dict = field(default_factory=dict)


PARTS = ("skills", "title", "experience", "domain", "similarity", "logistics")


def combine(parts: dict, weights: dict) -> float:
    total = sum(weights[k] for k in PARTS) or 1.0
    return 100.0 * sum(weights[k] * parts.get(k, 0.0) for k in PARTS) / total


# ---------------------------------------------------------------- stored job texts

class TextStore:
    """Job descriptions kept next to the match history, so every saved job can be scored again when the
    rules change. One small gzip file per day; older days are never rewritten."""

    def __init__(self, folder: str, keep_days: int = 120):
        self.folder = folder
        self.keep_days = keep_days
        self.texts: dict[str, str] = {}
        self.new: dict[str, str] = {}

    def load(self) -> "TextStore":
        import gzip
        import json
        if not os.path.isdir(self.folder):
            return self
        for name in sorted(os.listdir(self.folder)):
            if name.endswith(".json.gz"):
                try:
                    with gzip.open(os.path.join(self.folder, name), "rt", encoding="utf-8") as f:
                        self.texts.update(json.load(f))
                except (OSError, ValueError):
                    continue
        return self

    def get(self, uid: str) -> str:
        return self.texts.get(uid, "")

    def put(self, uid: str, text: str) -> None:
        text = (text or "").strip()[:15000]
        if text and self.texts.get(uid) != text:
            self.texts[uid] = text
            self.new[uid] = text

    def save(self, today: str) -> None:
        import gzip
        import json
        if not self.new:
            return
        os.makedirs(self.folder, exist_ok=True)
        path = os.path.join(self.folder, f"{today}.json.gz")
        day: dict[str, str] = {}
        if os.path.exists(path):
            try:
                with gzip.open(path, "rt", encoding="utf-8") as f:
                    day = json.load(f)
            except (OSError, ValueError):
                day = {}
        day.update(self.new)
        tmp = path + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as f:
            json.dump(day, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, path)
        self.new = {}
        # drop whole days older than keep_days
        cutoff = sorted(n for n in os.listdir(self.folder) if n.endswith(".json.gz"))
        for name in cutoff:
            try:
                d = datetime.fromisoformat(name[:10])
            except ValueError:
                continue
            if (datetime.fromisoformat(today) - d).days > self.keep_days:
                os.remove(os.path.join(self.folder, name))


# ---------------------------------------------------------------- skills report

def skills_report(ranker: "Ranker", jobs: list[tuple[str, str]], label: str = "") -> str:
    """Markdown: how often each skill is asked for (required or main duties) across the given jobs
    [(title, text)], split into skills the resume shows and skills it does not."""
    asked: Counter = Counter()
    required: Counter = Counter()
    n = 0
    for title, text in jobs:
        if not text:
            continue
        n += 1
        sec = sections(text)
        for sk in ranker.skills:
            if sk.rx.search(sec["required"]) or sk.rx.search(title or ""):
                required[sk.name] += 1
                asked[sk.name] += 1
            elif sk.rx.search(sec["duties"]) or sk.rx.search(sec["other"]):
                asked[sk.name] += 1
    if not n:
        return ""
    by_name = {s.name: s for s in ranker.skills}

    def table(names):
        rows = ["| Skill | Asked for in | Required in |", "|---|---:|---:|"]
        for name in names:
            rows.append(f"| {name} | {asked[name]} jobs ({asked[name] / n:.0%}) | {required[name]} ({required[name] / n:.0%}) |")
        return rows

    have = [k for k, _ in asked.most_common() if by_name[k].have >= 0.5][:25]
    lack = [k for k, _ in asked.most_common() if by_name[k].have < 0.5][:20]
    lines = [f"# {label or 'Skills'}: what the matching jobs ask for", "",
             f"Counted over the job descriptions of **{n} current matches** (boilerplate left out). "
             "\"Asked for\" = named in the requirements or the main duties; \"Required\" = named under "
             "required/basic qualifications or in the title.", "",
             "## Skills the resume shows, most asked first", "",
             "Make sure each of these is written on the resume in the words employers use: recruiters' "
             "screening software matches words.", "", *table(have), "",
             "## Skills the resume does not show, most asked first", "",
             "If you have one of these, add it to the resume (and to profiles/automotive/skills.yaml as "
             "`have: 1`). If not, these are the ones worth learning first.", "", *table(lack), ""]
    return "\n".join(lines)
