"""The return arrow: human decisions and replay outcomes, recorded by people and
merged back into the report on every run.

Nothing here is generated. Both inputs are optional JSON files the team writes:

  --decisions      {"p1": {"verdict": "accepted", "decided_by": "...", "reason": "...", "at": "...",
                           "signature": "<optional, copied from the prescription>"}, ...}
  --verifications  [{"prescription_id": "p1", "replay_run_id": "rp_...", "verdict": "improved",
                     "metric": "resolution_rate", "before": 0.31, "after": 0.83,
                     "golden_set_pass": true, "signature": "<optional>"}, ...]

An entry is used only if it points at a prescription that exists in THIS run (and,
when it carries a signature, the same diagnosis), and a verification only if its
run id is a real replay id (rp_...). Anything else is rejected with a printed
reason — a stale decision must never attach itself to a different finding.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Dict, List, Tuple

VERDICTS_DECISION = {"accepted", "rejected", "deferred"}
VERDICTS_REPLAY = {"improved", "no_effect", "regressed"}
DOWNWEIGHT_MIN_N = 3
DOWNWEIGHT_MAX_HIT_RATE = 0.5


def _load(path: str):
    if not path:
        return None
    if not os.path.exists(path):
        raise FileNotFoundError("loop-state file not found: %s" % path)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _matches(p: dict, entry: dict) -> bool:
    return not entry.get("signature") or entry["signature"] == p.get("signature")


def merge_decisions(prescriptions: List[dict], path: str) -> List[str]:
    data = _load(path)
    if data is None:
        return []
    warnings = []
    by_id = {p["id"]: p for p in prescriptions}
    for pid, entry in data.items():
        p = by_id.get(pid)
        if not p:
            warnings.append("decision for %s ignored: no such prescription in this run" % pid)
        elif not _matches(p, entry):
            warnings.append("decision for %s ignored: signature does not match this run's diagnosis" % pid)
        elif entry.get("verdict") not in VERDICTS_DECISION:
            warnings.append("decision for %s ignored: verdict must be one of %s" % (pid, sorted(VERDICTS_DECISION)))
        else:
            p["approval"] = {k: entry[k] for k in ("verdict", "decided_by", "reason", "at") if entry.get(k) is not None}
    return warnings


def build_verifications(prescriptions: List[dict], path: str) -> Tuple[List[dict], List[str]]:
    data = _load(path)
    if data is None:
        return [], []
    warnings, out = [], []
    by_id = {p["id"]: p for p in prescriptions}
    for entry in data:
        p = by_id.get(entry.get("prescription_id"))
        rid = str(entry.get("replay_run_id", ""))
        if not p:
            warnings.append("verification %s ignored: no such prescription in this run" % entry.get("prescription_id"))
            continue
        if not _matches(p, entry):
            warnings.append("verification for %s ignored: signature does not match" % p["id"])
            continue
        if not rid.startswith("rp_"):
            warnings.append("verification for %s ignored: replay_run_id %r is not a replay run id" % (p["id"], rid))
            continue
        if entry.get("verdict") not in VERDICTS_REPLAY:
            warnings.append("verification for %s ignored: verdict must be one of %s" % (p["id"], sorted(VERDICTS_REPLAY)))
            continue
        v = {"prescription_id": p["id"], "replay_run_id": rid, "verdict": entry["verdict"]}
        for k in ("metric", "before", "after", "golden_set_pass"):
            if entry.get(k) is not None:
                v[k] = entry[k]
        if isinstance(entry.get("before"), (int, float)) and isinstance(entry.get("after"), (int, float)):
            predicted = p["predicted_delta"]["to"] - p["predicted_delta"]["from"]
            v["prediction_error"] = round(predicted - (entry["after"] - entry["before"]), 4)
        out.append(v)
    return out, warnings


def build_self_assessment(prescriptions: List[dict], verifications: List[dict]) -> dict:
    type_of = {p["id"]: p["change_type"] for p in prescriptions}
    stats: Dict[str, dict] = defaultdict(lambda: {"n": 0, "hits": 0, "errors": []})
    for v in verifications:
        s = stats[type_of[v["prescription_id"]]]
        s["n"] += 1
        s["hits"] += 1 if v["verdict"] == "improved" else 0
        if "prediction_error" in v:
            s["errors"].append(v["prediction_error"])
    accuracy = {}
    for ct, s in sorted(stats.items()):
        accuracy[ct] = {"n": s["n"], "hit_rate": round(s["hits"] / s["n"], 4)}
        if s["errors"]:
            accuracy[ct]["mean_prediction_error"] = round(sum(s["errors"]) / len(s["errors"]), 4)
    downweighted = [ct for ct, a in accuracy.items() if a["n"] >= DOWNWEIGHT_MIN_N and a["hit_rate"] < DOWNWEIGHT_MAX_HIT_RATE]
    cycles = len({v["replay_run_id"] for v in verifications})
    if not verifications:
        notes = "No replay outcomes recorded yet, so the loop has no hit rate to report. Prescriptions carry a " \
                "replay_request for the team to send by hand; pass the responses back with --verifications."
    else:
        notes = ("%d recorded replay cycle(s). A change type is de-weighted only after >= %d verifications with a hit "
                 "rate below %.0f%%; smaller samples are reported but not acted on."
                 % (cycles, DOWNWEIGHT_MIN_N, DOWNWEIGHT_MAX_HIT_RATE * 100))
    return {"cycles": cycles, "prescription_accuracy": accuracy, "downweighted": downweighted, "notes": notes}
