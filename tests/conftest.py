import pytest

import radar.sources as _sources


@pytest.fixture(autouse=True)
def _no_live_sources(monkeypatch):
    """Runs of the radar in tests never call the public job feeds (tests of those readers import the
    functions directly and fake the HTTP calls)."""
    for name in ("workable_search_jobs", "hn_jobs", "remoteok_jobs", "wwr_jobs", "usajobs_jobs"):
        monkeypatch.setattr(_sources, name, lambda *a, **k: [])
    for var in ("ADZUNA_APP_ID", "ADZUNA_APP_KEY", "JOB_ALERTS_EMAIL", "JOB_ALERTS_APP_PASSWORD",
                "USAJOBS_API_KEY", "USAJOBS_EMAIL"):
        monkeypatch.delenv(var, raising=False)
