"""Prescriptions: one proposed change per diagnosed regression, framed as a
decision a human approves or rejects — never applied by this system.

Rules this module keeps:
  * it proposes changes to the AGENT (content, tool integration, prompt) and never
    to a metric definition, judge version or golden set (Rule 2);
  * `predicted_delta` is the finding's own observed value moving back to the
    baseline the finding already names — no new number is invented here;
  * every sentence is formatted from the finding/diagnosis numbers;
  * it never calls the replay service. It writes a `replay_request` the team can
    send by hand, with a golden set drawn from the human outcome labels.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from typing import Dict, List, Optional

from .standard import session_in_finding

# direct fix class per cause. prompt.edit rather than revert: a revert also undoes
# anything else shipped in that prompt version and is a blunter change to known-good
# conversations.
CHANGE_FOR_CAUSE = {
    "kb.gap": "kb.add",
    "tool.contract_break": "tool.validate",
    "tool.outage": "tool.fallback",
    "prompt.regression": "prompt.edit",
    "routing.error": "routing.change",
}
GOLDEN_SET_SIZE = 24


def _load_jsonl(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def build_golden_sets(kit_dir: str, sessions: List[dict], regressions: List[dict]) -> Dict[str, dict]:
    """Per tenant: human-labelled resolved conversations (labels/outcome_labels.jsonl)
    that sit outside every reported regression, spread across intents. Deterministic,
    derived once, and never edited by a prescription."""
    labels = _load_jsonl(os.path.join(kit_dir, "labels", "outcome_labels.jsonl"))
    by_id = {s["session_id"]: s for s in sessions}
    pools = defaultdict(lambda: defaultdict(list))
    for lab in labels:
        s = by_id.get(lab.get("session_id"))
        if not s or lab.get("human_resolved") is not True:
            continue
        if any(session_in_finding(s, f) for f in regressions):
            continue
        pools[s["tenant"]][s["intent"]].append(s["session_id"])
    out = {}
    for tenant, by_intent in sorted(pools.items()):
        queues = [sorted(ids) for _, ids in sorted(by_intent.items())]
        chosen = []
        while len(chosen) < GOLDEN_SET_SIZE and any(queues):
            for q in queues:
                if q and len(chosen) < GOLDEN_SET_SIZE:
                    chosen.append(q.pop(0))
        if chosen:
            digest = hashlib.sha1(",".join(chosen).encode("utf-8")).hexdigest()[:10]
            out[tenant] = {"version": "gs_outcome_labels_%s" % digest, "session_ids": chosen}
    return out


def _pct(x: Optional[float]) -> str:
    return "%.1f%%" % (100 * x) if x is not None else "n/a"


def _decision(cause: str, finding: dict, diag: dict, change_type: str, target: str) -> dict:
    sig = diag.get("signals") or {}
    w = finding["window"]
    tenant = finding["tenant"]
    down = finding.get("impact", {}).get("downstream", {})
    cfg = sig.get("config_change") or {}
    if cause == "kb.gap":
        return {
            "asking_approval_for": "Publish knowledge-base content that answers '%s' questions for %s. Over days %d-%d "
                                   "this cohort resolved %s against %s for the tenant's other intents, and kb_lookup "
                                   "found a confident document on %s of %s lookups."
                                   % (target, tenant, w["from_day"], w["to_day"], _pct(finding.get("observed")),
                                      _pct(finding.get("expected")), _pct(sig.get("kb_hit_rate")), sig.get("kb_lookups")),
            "risk_if_diagnosis_wrong": "If retrieval rather than missing content is at fault, the new articles change "
                                       "nothing: resolution stays near %s and the %s unplanned handoffs continue. Adding "
                                       "accurate content does not change answers for any other intent."
                                       % (_pct(finding.get("observed")), down.get("unplanned_handoffs")),
            "would_not_ship_if": "the replay does not lift this cohort's resolution_rate, any golden-set conversation "
                                 "regresses, or the questions need a policy the business has not decided yet (then the "
                                 "right change is a scripted handoff, not an article).",
        }
    if cause in ("tool.contract_break", "tool.outage"):
        return {
            "asking_approval_for": "Validate '%s' responses before the agent answers from them on %s: an outcome='ok' "
                                   "call with result_field_count = 0 is treated as a failure and takes the fallback "
                                   "path. Empty 'ok' responses went from %s to %s of calls%s."
                                   % (target, tenant, _pct(sig.get("empty_ok_rate_before")), _pct(sig.get("empty_ok_rate_after")),
                                      (" after the %s %s -> %s change on day %s" % (cfg.get("target"), cfg.get("from_value"),
                                                                                   cfg.get("to_value"), cfg.get("day"))) if cfg else ""),
            "risk_if_diagnosis_wrong": "If some empty results are legitimate (e.g. a record that genuinely does not "
                                       "exist), those turn into fallbacks. The empty rate before the change was %s, which "
                                       "bounds how many legitimate empties exist." % _pct(sig.get("empty_ok_rate_before")),
            "would_not_ship_if": "the tool owner confirms an empty 200 is part of the contract, the replay shows no "
                                 "resolution lift on the cohort, or any golden-set conversation regresses.",
        }
    if cause == "prompt.regression":
        return {
            "asking_approval_for": "Edit the '%s' prompt%s to remove the added confirmation turns while keeping any "
                                   "wording that is required. Mean turns rose %.2f -> %.2f with resolution %s -> %s."
                                   % (target, (" (changed %s -> %s on day %s)" % (cfg.get("from_value"), cfg.get("to_value"), cfg.get("day"))) if cfg else "",
                                      sig.get("mean_turns_before") or 0, sig.get("mean_turns_after") or 0,
                                      _pct(sig.get("resolution_before")), _pct(sig.get("resolution_after"))),
            "risk_if_diagnosis_wrong": "If the extra turns are required (for example compliance confirmations), "
                                       "removing them removes that safeguard. Resolution did not improve with them, so "
                                       "no outcome gain is lost, but the owner of that wording must agree.",
            "would_not_ship_if": "compliance requires the confirmation wording as written, median turns do not fall on "
                                 "replay, or any golden-set conversation regresses.",
        }
    return {
        "asking_approval_for": "Apply %s to '%s' on %s." % (change_type, target, tenant),
        "risk_if_diagnosis_wrong": "The diagnosis is '%s' at confidence %.2f; a wrong diagnosis leaves the finding unchanged." % (cause, diag.get("confidence") or 0),
        "would_not_ship_if": "the replay shows no effect on the cohort or any golden-set conversation regresses.",
    }


def build_prescriptions(findings: List[dict], diagnoses: List[dict], golden_sets: Dict[str, dict]) -> List[dict]:
    by_finding = {f["id"]: f for f in findings}
    out = []
    for d in diagnoses:
        f = by_finding.get(d["finding_id"])
        cause = d.get("cause_class")
        change_type = CHANGE_FOR_CAUSE.get(cause)
        if not f or not f.get("is_regression") or not change_type:
            continue
        if f.get("observed") is None or f.get("expected") is None:
            continue  # nothing to predict against — do not invent a baseline
        cohort = f.get("cohort") or {}
        target = {"kb.gap": cohort.get("intent"), "prompt.regression": cohort.get("agent_id")}.get(
            cause, (d.get("signals") or {}).get("tool") or cohort.get("tool") or cohort.get("intent") or cohort.get("agent_id"))
        if not target:
            continue
        description = {
            "kb.add": "Add knowledge-base content for '%s' so kb_lookup returns a confident document for this cohort." % target,
            "tool.validate": "Add a response contract check on '%s': an outcome='ok' call with no parsed fields is "
                             "reclassified as a failure and routed to the fallback copy instead of reaching the model." % target,
            "tool.fallback": "Route failed '%s' calls to a fallback path." % target,
            "prompt.edit": "Edit the '%s' prompt to remove redundant confirmation turns (an edit, not a revert, so "
                           "other changes in that prompt version are kept)." % target,
            "routing.change": "Adjust routing for '%s'." % target,
        }[change_type]
        gs = golden_sets.get(f["tenant"])
        replay_cohort = dict(cohort)
        replay_cohort.update({"from_day": f["window"]["from_day"], "to_day": f["window"]["to_day"]})
        p = {
            "id": "p%d" % (len(out) + 1),
            "diagnosis_id": d["id"],
            "change_type": change_type,
            "target": target,
            "description": description + " Predicted: %s returns from %s to the baseline named in finding %s (%s)."
                           % (f["metric"], f["observed"], f["id"], f["expected"]),
            "autonomy_rung": "L1",
            "predicted_delta": {"metric": f["metric"], "from": f["observed"], "to": f["expected"]},
            "decision": _decision(cause, f, d, change_type, target),
            "signature": "%s|%s|%s|%d" % (cause, f["tenant"], json.dumps(cohort, sort_keys=True), f["window"]["from_day"]),
            "replay_request": {
                "note": "NOT sent by this system. Send by hand (40-run team budget); record the response with --verifications.",
                "tenant": f["tenant"],
                "change": {"type": change_type, "target": target, "description": description},
                "cohort": replay_cohort,
                "golden_set": gs["session_ids"] if gs else [],
                "golden_set_version": gs["version"] if gs else None,
            },
        }
        out.append(p)
    return out
