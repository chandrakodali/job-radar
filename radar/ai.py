"""Optional AI review of each new match before it is saved or alerted.

Turns on when one of these repository secrets exists (first one found wins):
  GEMINI_API_KEY      Google AI Studio key (has a free tier)
  OPENROUTER_API_KEY  OpenRouter key (free models available)
  GROQ_API_KEY        Groq key (free tier)
  OPENAI_API_KEY      OpenAI key
  ANTHROPIC_API_KEY   Anthropic key
AI_MODEL (optional secret or variable) overrides the model name.

The reviewer answers, per job: is the role really DevOps / SRE / platform / cloud /
infrastructure / DevSecOps (or closely related), what level it is, the minimum years,
sponsorship and clearance signals, a 0-100 fit score and a short reason.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import requests

log = logging.getLogger("radar.ai")

PROVIDERS = [
    # env var, kind, endpoint, default model, model-preference regex for auto-pick
    ("GEMINI_API_KEY", "openai", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
     "gemini-flash-latest", r"gemini-[\d.]+-flash(?!.*(image|tts|live|audio|thinking|lite))"),
    ("OPENROUTER_API_KEY", "openai", "https://openrouter.ai/api/v1/chat/completions",
     "openrouter/free", r":free$"),
    ("GROQ_API_KEY", "openai", "https://api.groq.com/openai/v1/chat/completions",
     "llama-3.3-70b-versatile", r"llama.*70b|gpt-oss-120b|qwen"),
    ("OPENAI_API_KEY", "openai", "https://api.openai.com/v1/chat/completions", "gpt-4.1-mini", r"gpt-[\d.]+-mini$"),
    ("ANTHROPIC_API_KEY", "anthropic", "https://api.anthropic.com/v1/messages", "claude-haiku-5-5", r"haiku"),
]

ROLES = ("devops", "sre", "platform", "cloud", "infrastructure", "devsecops", "related", "other")

SYSTEM = """You screen job postings for one candidate and answer only with JSON.
Candidate: recent US computer-science graduate on F-1 OPT (STEM OPT eligible), 0-2 years of experience,
skills in Linux, AWS/Azure, Docker, Kubernetes, Terraform, CI/CD, Python/Bash.
Wanted roles ONLY: DevOps, DevSecOps, Site Reliability (SRE), Platform Engineering, Cloud Engineering /
Cloud Operations, Infrastructure Engineering, and closely related hands-on roles (Linux/Systems
Administrator, Build/Release, Production Engineer, cloud-focused Systems or Network Engineer, MLOps).
NOT wanted: help desk, desktop/customer/application support, data-center technicians, sales/solutions/
pre-sales, generic software or data engineering, ERP/Salesforce/ServiceNow, management.
Rules: "fit" is "yes" for a wanted role, "related" for a closely related role, "no" otherwise.
"level" is entry (0-2 yrs), mid (3-4 yrs) or senior (5+ yrs or lead/staff). "min_years" is the smallest
number of years the posting requires (null if not stated). "sponsor" is "yes" if it offers visa
sponsorship, "no" if it says no sponsorship / must not need sponsorship now or later, else "unknown".
"clearance" is true if it needs a security clearance or US citizenship/green card.
"score" 0-100 = how worth applying this is for the candidate (role fit, entry level, sponsorship, freshness).
"why" = at most 14 words."""


def _config():
    model_override = (os.environ.get("AI_MODEL") or "").strip()
    for env, kind, url, model, pref in PROVIDERS:
        key = (os.environ.get(env) or "").strip()
        if key:
            return {"env": env, "kind": kind, "url": url, "model": model_override or model, "pref": pref, "key": key}
    return None


class Reviewer:
    def __init__(self, max_requests: int = 12, batch: int = 5, system: str | None = None,
                 roles: list[str] | None = None, keep_senior: bool = False, max_min_years: int = 5):
        """`system`, `roles`, `keep_senior` and `max_min_years` let a profile describe another candidate
        (see the `ai:` section of profiles/<name>/config.yaml); the defaults are the DevOps radar's."""
        self.system = (system or SYSTEM).strip()
        self.roles = tuple(roles or ROLES)
        self.keep_senior = keep_senior
        self.max_min_years = max_min_years
        self.cfg = _config()
        self.max_requests = max_requests
        self.batch = batch
        self.requests = 0
        self.failures = 0
        self.disabled_reason = ""

    @property
    def available(self) -> bool:
        return bool(self.cfg) and not self.disabled_reason and self.requests < self.max_requests

    def describe(self) -> str:
        if not self.cfg:
            return "off (no AI key secret)"
        return f"{self.cfg['env'].split('_')[0].lower()} / {self.cfg['model']}"

    # ---- HTTP
    def _call(self, prompt: str) -> str:
        c = self.cfg
        if c["kind"] == "anthropic":
            r = requests.post(c["url"], timeout=90, headers={
                "x-api-key": c["key"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": c["model"], "max_tokens": 1500, "system": self.system,
                      "messages": [{"role": "user", "content": prompt}]})
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            return "".join(b.get("text", "") for b in r.json().get("content", []))
        headers = {"Authorization": f"Bearer {c['key']}", "Content-Type": "application/json"}
        if "openrouter" in c["url"]:
            headers["X-Title"] = "job-radar"
        body = {"model": c["model"], "temperature": 0,
                "messages": [{"role": "system", "content": self.system}, {"role": "user", "content": prompt}]}
        r = requests.post(c["url"], timeout=90, headers=headers, json=body)
        if r.status_code in (400, 404) and "model" in r.text.lower() and self._autopick():
            body["model"] = self.cfg["model"]
            r = requests.post(c["url"], timeout=90, headers=headers, json=body)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        msg = (r.json().get("choices") or [{}])[0].get("message") or {}
        return msg.get("content") or ""

    def _autopick(self) -> bool:
        """The default model name went stale: list the provider's models and pick a suitable one."""
        c = self.cfg
        try:
            r = requests.get(c["url"].rsplit("/chat/completions", 1)[0] + "/models", timeout=30,
                             headers={"Authorization": f"Bearer {c['key']}"})
            ids = [m.get("id", "") for m in r.json().get("data", [])]
        except Exception:
            return False
        ids = [i.split("/", 1)[1] if i.startswith("models/") else i for i in ids]
        good = sorted((i for i in ids if re.search(c["pref"], i)), reverse=True)
        if not good or good[0] == c["model"]:
            return False
        log.info("AI model %s unavailable; using %s", c["model"], good[0])
        c["model"] = good[0]
        return True

    # ---- review
    def review(self, items: list[dict]) -> dict[str, dict]:
        """items: [{"uid","title","company","location","workplace","posted","flags","description"}].
        Returns {uid: verdict} for the items it managed to review."""
        out: dict[str, dict] = {}
        for i in range(0, len(items), self.batch):
            if not self.available:
                break
            chunk = items[i:i + self.batch]
            lines = []
            for n, it in enumerate(chunk):
                desc = re.sub(r"\s+", " ", it.get("description") or "")[:1800]
                lines.append(f"### Job {n}\nTitle: {it['title']}\nCompany: {it['company']}\nLocation: {it['location']} "
                             f"({it.get('workplace') or 'workplace not stated'})\nPosted: {it.get('posted') or 'unknown'}\n"
                             f"Radar notes: {it.get('flags') or 'none'}\nDescription: {desc or '(not available)'}")
            prompt = ("Review these jobs. Answer with a JSON object {\"jobs\": [...]} holding one entry per job, "
                      "in order, each {\"i\": n, \"fit\", \"role\", \"level\", \"min_years\", \"sponsor\", "
                      "\"clearance\", \"score\", \"why\"} where role is one of " + ", ".join(self.roles) + ".\n\n"
                      + "\n\n".join(lines))
            self.requests += 1
            try:
                text = self._call(prompt)
                verdicts = _parse(text)
            except Exception as e:
                self.failures += 1
                log.warning("AI review failed (%s): %s", self.describe(), str(e)[:200])
                if "HTTP 401" in str(e) or "HTTP 403" in str(e):
                    self.disabled_reason = "key rejected"
                elif self.failures >= 3:
                    self.disabled_reason = "repeated errors"
                if "HTTP 429" in str(e):
                    time.sleep(8)
                continue
            for v in verdicts:
                try:
                    n = int(v.get("i"))
                except (TypeError, ValueError):
                    continue
                if 0 <= n < len(chunk):
                    out[chunk[n]["uid"]] = _clean(v, self.roles, self.keep_senior, self.max_min_years)
            time.sleep(1.5)
        return out


def _parse(text: str) -> list[dict]:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    m = re.search(r"\{.*\}|\[.*\]", t, re.S)
    if not m:
        raise ValueError("no JSON in answer")
    data = json.loads(m.group(0))
    if isinstance(data, dict):
        data = data.get("jobs") or data.get("results") or []
    return [d for d in data if isinstance(d, dict)]


def _clean(v: dict, roles=ROLES, keep_senior: bool = False, max_min_years: int = 5) -> dict:
    def num(x, lo, hi):
        try:
            return max(lo, min(hi, int(float(x))))
        except (TypeError, ValueError):
            return None
    fit = str(v.get("fit") or "").lower()
    level = str(v.get("level") or "").lower()
    role = str(v.get("role") or "").lower()
    clearance = v.get("clearance") in (True, "true", "yes", 1)
    min_years = num(v.get("min_years"), 0, 30)
    score = num(v.get("score"), 0, 100)
    too_senior = (level == "senior" and not keep_senior) or (min_years is not None and min_years >= max_min_years)
    keep = fit in ("yes", "related") and not too_senior and not clearance
    reasons = []
    if fit not in ("yes", "related"):
        reasons.append("not a target role")
    if too_senior:
        reasons.append("too senior")
    if clearance:
        reasons.append("clearance/citizenship")
    return {
        "keep": keep, "fit": fit, "role": role if role in roles else "other", "level": level,
        "min_years": min_years, "sponsor": str(v.get("sponsor") or "unknown").lower(),
        "clearance": clearance, "score": score if score is not None else 0,
        "why": re.sub(r"\s+", " ", str(v.get("why") or ""))[:140],
        "reject_reason": ", ".join(reasons),
    }
