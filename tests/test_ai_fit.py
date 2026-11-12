"""The resume AI check (radar/ai_fit.py), with the AI services faked."""
import json
import time

import pytest

from radar import ai_fit

GOOD = {"fit": "yes", "score": 88, "matched": ["Simulink", "AUTOSAR", "ISO 26262"], "missing": ["HIL"],
        "min_years": 3, "level": "mid", "clearance": False, "sponsor": "unknown", "role": "EPS controls",
        "why": "Model-based steering controls work matching the resume", "evidence": "MATLAB/Simulink"}
JOB = {"uid": "u1", "title": "Embedded Controls Engineer", "company": "X", "location": "Novi, MI",
       "description": "Simulink AUTOSAR ISO 26262 " * 50}


class Resp:
    def __init__(self, status, payload=None, text="", headers=None):
        self.status_code, self._payload, self.headers = status, payload, headers or {}
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        return self._payload


def ok(answer=GOOD):
    return Resp(200, {"choices": [{"message": {"content": json.dumps(answer)}}]})


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr(ai_fit, "PROVIDERS", {k: v[:5] + (0.0,) for k, v in ai_fit.PROVIDERS.items()})


def reviewer(env, **cfg):
    return ai_fit.FitReviewer({"resume": "AUTOSAR, Simulink engineer", **cfg}, env=env)


def test_only_services_with_keys_are_used_in_order(no_wait):
    r = reviewer({"GROQ_API_KEY": "g", "NVIDIA_API_KEY": "n"})
    assert [p.name for p in r.providers] == ["nvidia", "groq"]
    assert reviewer({}).cfg_ok is False and reviewer({}).describe().startswith("off")


def test_good_answer_is_cleaned_and_kept(monkeypatch, no_wait):
    sent = []
    monkeypatch.setattr(ai_fit.requests, "post", lambda url, **kw: sent.append(kw["json"]) or ok())
    v = reviewer({"NVIDIA_API_KEY": "n"}).check(JOB, time.monotonic() + 30)
    assert v["keep"] and v["score"] == 88 and v["service"].startswith("nvidia/")
    assert "has: Simulink, AUTOSAR, ISO 26262" in v["note"] and "missing: HIL" in v["note"]
    # the whole description goes out (up to desc_chars), with the resume in the instructions
    body = sent[0]
    assert "AUTOSAR, Simulink engineer" in body["messages"][0]["content"]
    assert len(body["messages"][1]["content"]) > 1000
    assert "sponsorship must not lower the score" in body["messages"][0]["content"].lower()


def test_daily_limit_moves_on_to_the_next_service(monkeypatch, no_wait):
    calls = []

    def post(url, **kw):
        calls.append(url)
        if "nvidia" in url:
            return Resp(429, text='{"error": "requests per day limit reached"}')
        return ok()
    monkeypatch.setattr(ai_fit.requests, "post", post)
    monkeypatch.setattr(ai_fit.requests, "get", lambda *a, **k: Resp(200, {"data": []}))
    r = reviewer({"NVIDIA_API_KEY": "n", "GEMINI_API_KEY": "g"})
    out = r.check_many([dict(JOB, uid=f"u{i}") for i in range(4)], 30)
    assert len(out) == 4
    assert all(v["service"].startswith("gemini/") for v in out.values())
    assert r.providers[0].dead   # every NVIDIA model used up its day


def test_rejected_key_switches_service_off(monkeypatch, no_wait):
    monkeypatch.setattr(ai_fit.requests, "post",
                        lambda url, **kw: Resp(401, text="bad key") if "groq" in url else ok())
    r = reviewer({"GROQ_API_KEY": "x", "GITHUB_MODELS_TOKEN": "t", "AI_ORDER": ""}, providers=["groq", "github"])
    v = r.check(JOB, time.monotonic() + 30)
    assert v and v["service"].startswith("github/")
    assert "key rejected" in r.providers[0].dead


def test_unreadable_answers_give_up_without_crashing(monkeypatch, no_wait):
    monkeypatch.setattr(ai_fit.requests, "post",
                        lambda url, **kw: Resp(200, {"choices": [{"message": {"content": "no idea"}}]}))
    assert reviewer({"NVIDIA_API_KEY": "n"}).check(JOB, time.monotonic() + 10) is None


@pytest.mark.parametrize("answer,keep,reason", [
    (dict(GOOD, fit="no", score=20), False, "not this kind of job"),
    (dict(GOOD, clearance=True), False, "clearance"),
    (dict(GOOD, min_years=12), False, "12+ years"),
    (dict(GOOD, fit="related", score=55, sponsor="no"), True, ""),   # no sponsorship alone never rejects
])
def test_keep_or_reject(answer, keep, reason):
    v = ai_fit.clean(answer, max_min_years=10)
    assert v["keep"] is keep and reason in v["reject_reason"]


def test_parse_handles_code_fences_and_thinking():
    text = "<think>hmm</think>```json\n" + json.dumps(GOOD) + "\n```"
    assert ai_fit.parse(text)["score"] == 88
    with pytest.raises(ValueError):
        ai_fit.clean({"fit": "maybe"})


def test_blend_and_row_fields():
    assert ai_fit.blend(40, 90, 0.6) == 70
    f = ai_fit.row_fields(ai_fit.clean(GOOD))
    assert f["ai"] == "keep" and f["ai_score"] == 88 and "missing: HIL" in f["ai_why"]
