"""AI check of each job's full description against the candidate's resume (Auto Radar only).

Turned on by `ai: mode: resume` in a profile's config. Uses every free AI service it has a key for and
moves to the next one when a service is busy or out of free requests for the day:

  NVIDIA_API_KEY        build.nvidia.com (free, about 40 requests a minute)
  GEMINI_API_KEY        Google AI Studio (Flash-Lite models, a few hundred requests a day each)
  GROQ_API_KEY          Groq (about 50 full job checks a day per model)
  MISTRAL_API_KEY, OPENROUTER_API_KEY   used too if present

Each check sends the job's title, company, location and description, and a short summary of the
resume's skills and experience (no name or contact details). The answer is JSON: fit, a 0-100 score,
the resume skills the job asks for, the ones it asks for that the resume lacks, years required,
clearance/citizenship, and a short reason.

Check which keys work:  python -m radar.ai_fit --check --profile automotive
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field

import requests

log = logging.getLogger("radar.ai_fit")

# name, env var, chat URL, models URL (None: no model list), models to try in order, seconds between requests
PROVIDERS = {
    "nvidia": ("NVIDIA_API_KEY", "https://integrate.api.nvidia.com/v1/chat/completions",
               "https://integrate.api.nvidia.com/v1/models",
               ["deepseek-ai/deepseek-v4.1-flash", "z-ai/glm-5.3-flash", "minimaxai/minimax-m3.1-flash",
                "google/gemma-4-31b-it", "openai/gpt-oss-20b", "mistralai/mistral-large-2-instruct"],
               r"deepseek-v\d|glm-\d|minimax-m|gemma-4|kimi-k|mistral-large|gpt-oss|nemotron-70b", 2.0),
    "gemini": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
               "https://generativelanguage.googleapis.com/v1beta/openai/models",
               ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemma-4-31b-it", "gemma-4-26b-a4b-it"],
               r"gemini-[\d.]+-flash-lite$", 4.5),
    "github": ("GITHUB_MODELS_TOKEN", "https://models.github.ai/inference/chat/completions",
               "https://models.github.ai/catalog/models",
               ["openai/gpt-4.1-mini", "openai/gpt-4o-mini", "meta/llama-3.3-70b-instruct"],
               r"gpt-4\.1-mini|gpt-4o-mini|llama-3\.3-70b", 4.5),
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1/chat/completions",
             "https://api.groq.com/openai/v1/models",
             ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"],
             r"gpt-oss|qwen", 20.0),
    "mistral": ("MISTRAL_API_KEY", "https://api.mistral.ai/v1/chat/completions", "https://api.mistral.ai/v1/models",
                ["mistral-small-latest", "mistral-medium-latest"], r"mistral-(small|medium)-latest", 1.5),
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1/chat/completions",
                   "https://openrouter.ai/api/v1/models", ["openrouter/free"], r":free$", 4.0),
}
# GitHub Models stopped answering in 2026 (it replies "OK"); it is kept only for a token set by hand.
DEFAULT_ORDER = ["nvidia", "gemini", "groq", "mistral", "openrouter", "github"]

INSTRUCTIONS = """You check one job posting against one candidate's resume and answer only with a JSON object.

Candidate (resume summary):
{resume}

Judge ONLY from the posting text. Do not guess requirements that are not written there.
Fields:
- "fit": "yes" = the job is the candidate's kind of work (automotive or similar embedded / controls /
  model-based software, AUTOSAR, ADAS/chassis/steering/powertrain/battery/motor-control software,
  software integration, MIL/SIL/HIL test and verification); "related" = adjacent embedded or controls
  work the candidate could reasonably do; "no" = a different job (web, cloud, IT, data, mechanical,
  manufacturing, quality, hardware-only, PLC, management, sales, technician, internship, etc.).
- "score": 0-100, how well the candidate's actual tools and experience cover what the posting asks for.
  90+ = nearly everything required is on the resume; 70-89 = most of it; 50-69 = about half;
  below 50 = little overlap or a different job. Visa sponsorship must NOT lower the score.
- "matched": up to 8 short names of requirements in the posting that the resume shows.
- "missing": up to 6 short names of REQUIRED (not "nice to have") items the resume does not show.
- "min_years": smallest number of years of experience the posting requires, or null if not stated.
- "level": "entry" (0-2 yrs), "mid" (3-7 yrs) or "senior" (8+ yrs, or staff/principal/lead).
- "clearance": true ONLY if the posting says a security clearance, US citizenship or ITAR/US-person
  status is required; otherwise false.
- "sponsor": "yes" if it offers visa sponsorship, "no" if it says no sponsorship, else "unknown".
- "role": 1-4 words naming the kind of job, e.g. "AUTOSAR software", "ADAS controls", "HIL test".
- "why": at most 20 words on the main reason for the score.
- "evidence": a short exact phrase (under 15 words) from the posting that best supports your verdict."""

FIELDS = '{"fit", "score", "matched", "missing", "min_years", "level", "clearance", "sponsor", "role", "why", "evidence"}'


@dataclass
class _Provider:
    name: str
    key: str
    url: str
    models_url: str | None
    models: list[str]
    pref: str
    interval: float
    model_i: int = 0
    next_ok: float = 0.0
    busy: bool = False
    dead: str = ""                 # why it is out for this run ("" = usable)
    fails: int = 0
    rate_hits: int = 0
    json_mode: bool = True
    picked: bool = False
    done: int = 0
    tried: list[str] = field(default_factory=list)
    last_error: str = ""

    @property
    def model(self) -> str:
        return self.models[self.model_i]

    def headers(self) -> dict:
        h = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        if self.name == "github":
            h["Accept"] = "application/vnd.github+json"
            h["X-GitHub-Api-Version"] = "2022-11-28"
        if self.name == "openrouter":
            h["X-Title"] = "job-radar"
        return h


class _Retry(Exception):
    """The job can be tried again (on this or another service)."""


def _key(name: str, envvar: str, env) -> str:
    key = (env.get(envvar) or "").strip()
    if not key and name == "groq":
        # A Groq key saved under the name GROK_API_KEY. (A key from xAI's Grok starts with "xai-": that
        # service is not free, so it is not used.)
        alt = (env.get("GROK_API_KEY") or "").strip()
        if alt.startswith("xai-"):
            log.warning("GROK_API_KEY is an xAI (Grok) key, not a Groq key; xAI is not free, so it is not used")
        elif alt:
            key = alt
    return key


def _providers(order: list[str] | None = None, env=os.environ) -> list[_Provider]:
    out = []
    for name in order or DEFAULT_ORDER:
        if name not in PROVIDERS:
            continue
        envvar, url, murl, models, pref, interval = PROVIDERS[name]
        key = _key(name, envvar, env)
        if not key:
            continue
        override = (env.get(f"{name.upper()}_MODEL") or "").strip()
        out.append(_Provider(name=name, key=key, url=url, models_url=murl,
                             models=([override] if override else []) + list(models), pref=pref, interval=interval))
    return out


class FitReviewer:
    def __init__(self, ai_cfg: dict, env=os.environ):
        self.cfg = ai_cfg or {}
        self.resume = re.sub(r"[ \t]+", " ", str(self.cfg.get("resume") or "")).strip()
        self.system = INSTRUCTIONS.format(resume=self.resume)
        self.desc_chars = int(self.cfg.get("desc_chars", 12000))
        self.max_min_years = int(self.cfg.get("max_min_years", 10))
        self.providers = _providers(self.cfg.get("providers"), env)
        self.lock = threading.Condition()
        self.calls = 0

    # ---- status
    @property
    def available(self) -> bool:
        return any(not p.dead for p in self.providers)

    @property
    def cfg_ok(self) -> bool:
        return bool(self.providers)

    def describe(self) -> str:
        if not self.providers:
            return "off (no AI key secret)"
        parts = []
        for p in self.providers:
            s = f"{p.name}/{p.model.split('/')[-1]} {p.done}"
            if p.dead:
                s += f" ({p.dead})"
            parts.append(s)
        return "resume check: " + ", ".join(parts)

    # ---- choosing a service
    def _take(self, deadline: float) -> _Provider | None:
        with self.lock:
            while True:
                alive = [p for p in self.providers if not p.dead]
                if not alive or time.monotonic() > deadline:
                    return None
                now = time.monotonic()
                for p in alive:          # in the configured order: the first free, ready service
                    if not p.busy and p.next_ok <= now:
                        p.busy = True
                        return p
                waits = [p.next_ok - now for p in alive if not p.busy]
                self.lock.wait(timeout=max(0.2, min(waits) if waits else 1.0))

    def _give_back(self, p: _Provider, wait: float) -> None:
        with self.lock:
            p.busy = False
            p.next_ok = time.monotonic() + max(p.interval, wait)
            self.lock.notify_all()

    # ---- one request
    def _post(self, p: _Provider, prompt: str) -> str:
        body = {"model": p.model, "temperature": 0, "max_tokens": 2000,
                "messages": [{"role": "system", "content": self.system}, {"role": "user", "content": prompt}]}
        if p.json_mode:
            body["response_format"] = {"type": "json_object"}
        if p.name == "groq" and "gpt-oss" in p.model:
            body["reasoning_effort"] = "low"
        self.calls += 1
        r = requests.post(p.url, headers=p.headers(), json=body, timeout=120)
        if r.status_code == 200:
            try:
                data = r.json()
            except ValueError:
                p.last_error = f"reply is not JSON ({r.headers.get('content-type', '?')}): {r.text[:80]!r}"
                p.dead = "service does not answer properly"
                raise _Retry()
            msg = ((data.get("choices") or [{}])[0].get("message") or {})
            content = msg.get("content") or ""
            if not content.strip():
                raise ValueError("empty answer")
            return content
        text = r.text[:400]
        low = text.lower()
        p.last_error = f"HTTP {r.status_code}: {re.sub(r'\s+', ' ', text)[:200]}"
        log.info("AI %s/%s: %s", p.name, p.model, p.last_error)
        if r.status_code == 429:
            p.rate_hits += 1
            per_minute = re.search(r"minute|per_?min|rpm|tpm|too many requests in", low)
            per_day = re.search(r"per ?day|daily|perday|rpd|tpd|tokens per day|requests per day", low)
            if (per_day and not per_minute) or p.rate_hits >= 6:
                self._next_model(p, "daily free limit reached")
                raise _Retry()
            wait = float(r.headers.get("retry-after") or 0) or 20.0
            raise _Retry(min(wait, 90.0))
        if r.status_code in (401, 403):
            p.dead = f"key rejected (HTTP {r.status_code})"
            raise _Retry()
        if r.status_code in (400, 422) and p.json_mode and "response_format" in low:
            p.json_mode = False
            raise _Retry(0.1)
        if r.status_code == 410 or (r.status_code in (400, 404, 422)
                                    and re.search(r"model|not found|does not exist|unknown", low)):
            self._next_model(p, f"model {p.model} not available")
            raise _Retry(0.1)
        if r.status_code in (400, 413) and re.search(r"too long|too large|context|maximum|tokens", low):
            raise ValueError("job text too long for this service")
        raise RuntimeError(f"HTTP {r.status_code}: {text[:160]}")

    def _next_model(self, p: _Provider, why: str) -> None:
        p.tried.append(p.model)
        p.rate_hits = 0
        if p.model_i + 1 < len(p.models):
            log.info("AI %s: %s; trying %s", p.name, why, p.models[p.model_i + 1])
            p.model_i += 1
            return
        if not p.picked and p.models_url:
            p.picked = True
            new = self._autopick(p)
            if new:
                p.models.append(new)
                p.model_i = len(p.models) - 1
                log.info("AI %s: %s; using %s", p.name, why, new)
                return
        p.dead = why

    def _autopick(self, p: _Provider) -> str | None:
        try:
            r = requests.get(p.models_url, headers=p.headers(), timeout=30)
            data = r.json()
            items = data.get("data", data) if isinstance(data, dict) else data
            ids = [str(m.get("id", "")) for m in items if isinstance(m, dict)]
        except Exception:
            return None
        ids = [i.split("/", 1)[1] if i.startswith("models/") else i for i in ids]
        good = sorted((i for i in ids if re.search(p.pref, i) and i not in p.tried), reverse=True)
        return good[0] if good else None

    # ---- one job
    def prompt(self, item: dict, chars: int | None = None) -> str:
        desc = re.sub(r"\s+", " ", item.get("description") or "").strip()[: chars or self.desc_chars]
        return (f"Job title: {item.get('title', '')}\nCompany: {item.get('company', '')}\n"
                f"Location: {item.get('location', '')}\n\nJob description:\n{desc}\n\n"
                f"Answer with one JSON object with the fields {FIELDS}.")

    def check(self, item: dict, deadline: float) -> dict | None:
        chars = self.desc_chars
        attempts = 0
        while attempts < 5:
            p = self._take(deadline)
            if p is None:
                return None
            wait = 0.0
            before = (p.model_i, p.dead)
            attempts += 1
            try:
                verdict = clean(parse(self._post(p, self.prompt(item, chars))), self.max_min_years)
                p.fails = 0
                p.done += 1
                verdict["service"] = f"{p.name}/{p.model.split('/')[-1]}"
                return verdict
            except _Retry as e:
                wait = float(e.args[0]) if e.args else 0.0
                if (p.model_i, p.dead) != before:
                    attempts -= 1     # a service or model ran out: not this job's fault, try the next one
            except ValueError as e:   # unreadable answer or text too long
                if "too long" in str(e):
                    chars = max(3000, chars // 2)
                p.last_error = f"unusable answer: {e}"
                log.info("AI %s/%s: %s", p.name, p.model, p.last_error)
            except Exception as e:     # network trouble or a server error
                p.fails += 1
                wait = 15.0
                log.warning("AI %s failed: %s", p.name, str(e)[:160])
                if p.fails >= 3:
                    p.dead = "repeated errors"
            finally:
                self._give_back(p, wait)
        return None

    def check_many(self, items: list[dict], seconds: float) -> dict[str, dict]:
        """items: [{"uid", "title", "company", "location", "description"}], best first. Checks as many as fit
        in `seconds`, one request at a time per service, all services working in parallel."""
        out: dict[str, dict] = {}
        if not self.providers or not items:
            return out
        deadline = time.monotonic() + seconds
        todo = list(items)
        todo_lock = threading.Lock()

        def worker():
            while time.monotonic() < deadline and self.available:
                with todo_lock:
                    if not todo:
                        return
                    it = todo.pop(0)
                v = self.check(it, deadline)
                if v is not None:
                    out[it["uid"]] = v

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, len(self.providers)))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(max(1.0, deadline - time.monotonic() + 150))
        return out


# ---- answers
def parse(text: str) -> dict:
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise ValueError("no JSON in answer")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("answer is not an object")
    if isinstance(data.get("jobs"), list) and data["jobs"]:
        data = data["jobs"][0]
    return data


def _num(x, lo, hi):
    try:
        return max(lo, min(hi, int(float(x))))
    except (TypeError, ValueError):
        return None


def _names(x, n) -> list[str]:
    if isinstance(x, str):
        x = re.split(r"[;,]", x)
    if not isinstance(x, list):
        return []
    return [re.sub(r"\s+", " ", str(s)).strip()[:40] for s in x if str(s).strip()][:n]


def clean(v: dict, max_min_years: int = 10) -> dict:
    fit = str(v.get("fit") or "").strip().lower()
    score = _num(v.get("score"), 0, 100)
    if fit not in ("yes", "related", "no") or score is None:
        raise ValueError("answer is missing fit or score")
    clearance = v.get("clearance") in (True, "true", "yes", "True", 1)
    min_years = _num(v.get("min_years"), 0, 30)
    sponsor = str(v.get("sponsor") or "unknown").strip().lower()
    reasons = []
    if fit == "no":
        reasons.append("not this kind of job")
    if clearance:
        reasons.append("needs clearance/citizenship")
    if min_years is not None and min_years >= max_min_years:
        reasons.append(f"asks for {min_years}+ years")
    matched, missing = _names(v.get("matched"), 8), _names(v.get("missing"), 6)
    why = re.sub(r"\s+", " ", str(v.get("why") or "")).strip()[:160]
    evidence = re.sub(r"\s+", " ", str(v.get("evidence") or "")).strip()[:120]
    note = why
    if matched:
        note += " | has: " + ", ".join(matched)
    if missing:
        note += " | missing: " + ", ".join(missing)
    return {
        "keep": not reasons, "fit": fit, "score": score, "min_years": min_years, "clearance": clearance,
        "sponsor": sponsor if sponsor in ("yes", "no") else "unknown",
        "level": str(v.get("level") or "").strip().lower()[:10],
        "role": re.sub(r"\s+", " ", str(v.get("role") or "")).strip()[:40],
        "matched": matched, "missing": missing, "why": why, "evidence": evidence,
        "note": note[:400], "reject_reason": ", ".join(reasons),
    }


def row_fields(v: dict) -> dict:
    """The AI columns of matches.csv for a verdict."""
    return {"ai": "keep" if v["keep"] else "reject", "ai_score": v["score"], "ai_role": v["role"],
            "ai_level": v["level"], "ai_sponsor": v["sponsor"], "ai_why": v["note"]}


def blend(rule_score: int, ai_score: int, weight: float) -> int:
    return round(weight * ai_score + (1 - weight) * int(rule_score))


# ---- python -m radar.ai_fit --check
SAMPLE = {
    "uid": "sample", "title": "Embedded Software Engineer - Steering Controls", "company": "Sample Supplier",
    "location": "Auburn Hills, MI",
    "description": ("Develop embedded controls software for electric power steering using MATLAB/Simulink "
                    "and Stateflow with automatic code generation (Embedded Coder). Integrate AUTOSAR "
                    "software components, perform MIL/SIL testing, and follow ISO 26262 and MISRA C. "
                    "Requirements: BS in engineering, 3+ years of automotive embedded software experience, "
                    "C/C++, CAN, DOORS. Nice to have: HIL testing with dSPACE, Python."),
}


def _check_cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Test each AI service with one sample job")
    ap.add_argument("--profile", default="automotive")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    import yaml
    from . import profile
    cfg = yaml.safe_load(open(profile.paths(args.profile)["config"], encoding="utf-8"))
    ai_cfg = cfg.get("ai") or {}
    names = [p.name for p in _providers(ai_cfg.get("providers"))]
    print("Keys found for: " + (", ".join(names) or "none") + ".  (GROQ_API_KEY set: "
          + str(bool(os.environ.get("GROQ_API_KEY"))) + ", GROK_API_KEY set: " + str(bool(os.environ.get("GROK_API_KEY"))) + ")")
    if not names:
        print("No AI key found in the environment.")
        return 1
    for p in _providers(ai_cfg.get("providers")):   # what each service offers right now
        if p.models_url:
            try:
                r = requests.get(p.models_url, headers=p.headers(), timeout=30)
                data = r.json()
                items = data.get("data", data) if isinstance(data, dict) else data
                ids = sorted(str(m.get("id", "")) for m in items if isinstance(m, dict))
                print(f"MODELS {p.name} ({len(ids)}): " + ", ".join(ids)[:3000])
            except Exception as e:
                print(f"MODELS {p.name}: could not list ({str(e)[:100]})")
    ok = 0
    for name in names:
        r = FitReviewer({**ai_cfg, "providers": [name]})
        v = r.check(SAMPLE, time.monotonic() + 240)
        p = r.providers[0]
        if v:
            ok += 1
            print(f"OK    {name:10s} {v['service']:40s} fit={v['fit']} score={v['score']} | {v['note'][:120]}")
        else:
            print(f"FAIL  {name:10s} {p.model:40s} {p.dead or 'no usable answer'} | last error: {p.last_error}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"AI check: {ok} of {len(names)} services answered.\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_check_cli())
