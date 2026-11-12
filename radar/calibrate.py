"""Check (and tune) the rule-based score against your own verdicts. No AI: a plain search over weights.

Your verdicts come from two files in the profile folder (both optional):
  labels.csv   the labeling list: put apply / maybe / no in the "label" column
  marks.json   marks copied from the page ("Move your saved/applied marks to another device" -> Copy):
               saved and applied count as apply, skipped as no

    python -m radar.calibrate --profile automotive            # how well the score agrees with you
    python -m radar.calibrate --profile automotive --tune     # best weights for your verdicts
    python -m radar.calibrate --profile automotive --make-list 60   # write labels.csv to fill in
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
from datetime import datetime, timezone

import yaml

from . import profile as _profile
from . import rank, store
from .match import Rules, classify_location

VALUE = {"apply": 1.0, "yes": 1.0, "maybe": 0.5, "no": 0.0}


def load_labels(profile_dir: str) -> dict[str, float]:
    out: dict[str, float] = {}
    path = os.path.join(profile_dir, "marks.json")
    if os.path.exists(path):
        try:
            marks = json.load(open(path, encoding="utf-8")) or {}
        except ValueError:
            marks = {}
        for uid, m in marks.items():
            s = (m or {}).get("s") if isinstance(m, dict) else m
            if s in ("saved", "applied"):
                out[uid] = 1.0
            elif s == "skipped":
                out[uid] = 0.0
    path = os.path.join(profile_dir, "labels.csv")
    if os.path.exists(path):
        for r in csv.DictReader(open(path, encoding="utf-8")):
            v = VALUE.get((r.get("label") or "").strip().lower())
            if v is not None and r.get("uid"):
                out[r["uid"]] = v
    return out


def handled_uids(profile_dir: str) -> set[str]:
    """Jobs you are done with (applied, skipped, or labeled no): left out of the report."""
    out: set[str] = set()
    path = os.path.join(profile_dir, "marks.json")
    if os.path.exists(path):
        try:
            marks = json.load(open(path, encoding="utf-8")) or {}
        except ValueError:
            marks = {}
        out |= {u for u, m in marks.items() if ((m or {}).get("s") if isinstance(m, dict) else m) in ("applied", "skipped")}
    path = os.path.join(profile_dir, "labels.csv")
    if os.path.exists(path):
        out |= {r["uid"] for r in csv.DictReader(open(path, encoding="utf-8"))
                if (r.get("label") or "").strip().lower() == "no" and r.get("uid")}
    return out


def concordance(scored: list[tuple[float, float]]) -> float | None:
    """Share of (better, worse) verdict pairs that the score puts in the same order (0.5 = coin flip)."""
    good = ties = total = 0
    for i, (si, vi) in enumerate(scored):
        for sj, vj in scored[i + 1:]:
            if vi == vj:
                continue
            total += 1
            hi, lo = (si, sj) if vi > vj else (sj, si)
            good += hi > lo
            ties += hi == lo
    return (good + 0.5 * ties) / total if total else None


def report(scored: list[tuple[float, float]]) -> str:
    by = {}
    for s, v in scored:
        by.setdefault(v, []).append(s)
    lines = [f"{len(scored)} jobs with your verdict"]
    for v, name in ((1.0, "apply"), (0.5, "maybe"), (0.0, "no")):
        xs = by.get(v, [])
        if xs:
            lines.append(f"  {name:6s} {len(xs):3d} jobs, average score {sum(xs) / len(xs):5.1f}, range {min(xs):.0f}-{max(xs):.0f}")
    c = concordance(scored)
    if c is not None:
        lines.append(f"  score puts your better verdict first in {c:.0%} of pairs (50% = random, 100% = perfect)")
    top = sorted(scored, key=lambda x: -x[0])[:20]
    if top:
        lines.append(f"  of the 20 highest-scored labeled jobs, {sum(1 for _, v in top if v >= 1.0)} are 'apply'")
    return "\n".join(lines)


def tune(parts_and_labels: list[tuple[dict, float]], base: dict, tries: int = 4000, seed: int = 7) -> tuple[dict, float]:
    """Random search over the six weights for the best concordance with your verdicts."""
    rnd = random.Random(seed)

    def quality(w):
        return concordance([(rank.combine(p, w), v) for p, v in parts_and_labels]) or 0.0
    best_w, best_q = dict(base), quality(base)
    for _ in range(tries):
        w = dict(base)
        for k in rank.PARTS:
            w[k] = round(max(0.0, rnd.uniform(0.0, 0.6)), 2)
        if w["skills"] < 0.15:      # keep the score about the job's content
            continue
        q = quality(w)
        if q > best_q + 1e-9:
            best_w, best_q = w, q
    total = sum(best_w[k] for k in rank.PARTS) or 1.0
    return {k: round(best_w[k] / total, 3) for k in rank.PARTS}, best_q


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", default="automotive")
    ap.add_argument("--tune", action="store_true")
    ap.add_argument("--make-list", type=int, default=0, metavar="N")
    args = ap.parse_args(argv)
    paths = _profile.activate(args.profile)
    base_dir = os.path.dirname(paths["config"])
    cfg = yaml.safe_load(open(paths["config"], encoding="utf-8"))
    rows = store.read_matches() + [r for r in store.read_rejected() if (r.get("reject_reason") or "").startswith("rules:")]
    if args.make_list:
        return make_list(rows, os.path.join(base_dir, "labels.csv"), args.make_list)
    labels = load_labels(base_dir)
    seen, scored = set(), []
    for r in rows:
        if r["uid"] in labels and r["uid"] not in seen:
            seen.add(r["uid"])
            scored.append((float(r["score"]), labels[r["uid"]]))
    if not scored:
        print("No verdicts yet: fill in the label column of labels.csv, or save marks.json from the page.")
        return 0
    print("Current scores\n" + report(scored))
    if args.tune:
        texts = rank.TextStore(os.path.join(paths["data"], "texts")).load()
        ranker = rank.Ranker.load(cfg, base_dir, texts.texts.values())
        rules = Rules.from_config(cfg)
        now = datetime.now(timezone.utc)
        pl = []
        for r in rows:
            if r["uid"] in labels and texts.get(r["uid"]):
                f = ranker.score(r["title"], texts.get(r["uid"]), location=r.get("location", ""),
                                 loc_class=classify_location(r.get("location", "")), now=now)
                pl.append((f.parts, labels[r["uid"]]))
        if len(pl) < 10:
            print(f"Only {len(pl)} labeled jobs have a kept job text; label more before tuning.")
            return 0
        w, q = tune(pl, {**rank.DEFAULT_WEIGHTS, **(cfg.get("ranking") or {}).get("weights", {})})
        print(f"\nBest weights for your {len(pl)} verdicts (agreement {q:.0%}):")
        print(yaml.safe_dump({"weights": w}, sort_keys=False))
    return 0


def make_list(rows: list[dict], path: str, n: int) -> int:
    """A labeling list spread over the whole score range, best known employers first within each band."""
    pool = {}
    for r in rows:
        if r.get("has") or r.get("missing"):
            pool.setdefault(r["uid"], r)
    ranked = sorted(pool.values(), key=lambda r: -int(r["score"]))
    if not ranked:
        print("No scored jobs yet.")
        return 1
    step = max(1, len(ranked) // n)
    pick = ranked[::step][:n]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["label", "score", "title", "company", "location", "has", "missing", "url", "uid"])
        for r in pick:
            w.writerow(["", r["score"], r["title"], r["company"], r["location"], r.get("has", ""),
                        r.get("missing", ""), r["url"], r["uid"]])
    print(f"Wrote {len(pick)} jobs to {path}: put apply, maybe or no in the label column.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
