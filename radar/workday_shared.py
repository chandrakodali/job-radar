"""The shared Workday listings file (written by scripts/workday_sweep.py, read by every radar)."""
from __future__ import annotations

import dataclasses
import gzip
import json
import os
from datetime import datetime

from .models import Job


def job_to_dict(j: Job) -> dict:
    d = dataclasses.asdict(j)
    d["posted_at"] = j.posted_at.isoformat() if j.posted_at else None
    for k in ("description", "detail_tried", "found_by"):
        d.pop(k, None)
    return d


def dict_to_job(d: dict) -> Job:
    d = dict(d)
    p = d.get("posted_at")
    d["posted_at"] = datetime.fromisoformat(p) if p else None
    return Job(**{k: v for k, v in d.items() if k in Job.__dataclass_fields__})


def load_cache(path: str) -> dict:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and "boards" in data else {"boards": {}}
    except (OSError, ValueError):
        return {"boards": {}}


def save_cache(path: str, cache: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(cache, f, separators=(",", ":"))
    os.replace(tmp, path)
