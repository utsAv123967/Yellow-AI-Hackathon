"""Measurement gaps — the answer to "what can we not measure, and why."

Read from catalog.json rather than re-invented: its `capabilities` list states,
per capability, whether it is measurable, judged, or not measurable at all (and
which event would have to be logged), and `cardinality_budgets` states which
breakdowns a planner must refuse. Gaps here are a structural translation of that
into the report schema, plus the one thing the catalog cannot know: the actual
distinct-value count of each budgeted field in THIS corpus.

The capability id -> ask id map (failover_rate -> A11, abandonment_reason -> A09)
is fixed by the challenge's own question set, which is the same on every corpus.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import List, Optional, Tuple

CAPABILITY_TO_ASK = {
    "failover_rate": "A11",
    "abandonment_reason": "A09",
}

VERDICT_BY_CLASS = {
    "NOT_MEASURABLE": "NOT_MEASURABLE",
    "requires_new_judge": "REQUIRES_NEW_JUDGE",
}

JUDGE_EVENT_TEMPLATE = {
    "grain": "session",
    "fields": ["session_id", "judge_version", "label", "rationale", "calibration_agreement"],
    "owner": "quality-evaluation",
}


def _split_proxy(nearest_proxy: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not nearest_proxy:
        return None, None
    for marker in (" — ", " - "):
        if marker in nearest_proxy:
            proxy, rest = nearest_proxy.split(marker, 1)
            rest = rest[4:] if rest.lower().startswith("but ") else rest
            return proxy.strip(), rest.strip()
    return nearest_proxy.strip(), None


def _field_values(sessions: List[dict], field: str):
    parts = field.split(".")
    if parts[0] != "session":
        return None  # step-grain fields are not held in memory; handled by budget alone
    values = set()
    for s in sessions:
        v = s
        for p in parts[1:]:
            v = v.get(p) if isinstance(v, dict) else None
            if v is None:
                break
        if v is not None:
            values.add(json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v)
    return values


def cardinality_audit(catalog: dict, sessions: List[dict]) -> Tuple[List[dict], Optional[str]]:
    declared = catalog.get("cardinality_budgets") or {}
    budgets = {k: v for k, v in declared.items() if not k.startswith("_") and isinstance(v, int)}
    for field, spec in (catalog.get("fields") or {}).items():
        if isinstance(spec, dict) and isinstance(spec.get("cardinality_budget"), int):
            budgets.setdefault(field, spec["cardinality_budget"])
    audit = []
    for field, budget in sorted(budgets.items()):
        values = _field_values(sessions, field)
        distinct = len(values) if values is not None else None
        refused = (distinct > budget) if distinct is not None else (budget == 0)
        audit.append({"field": field, "budget": budget, "distinct": distinct, "refused": refused})
    return audit, declared.get("_rule")


def _cardinality_gaps(audit: List[dict], rule: Optional[str]) -> List[dict]:
    gaps = []
    allowed = [a for a in audit if not a["refused"] and a["distinct"] is not None]
    for a in audit:
        if not a["refused"] or a["budget"] == 0:
            continue
        parent = a["field"].rsplit(".", 1)[0]
        proxies = [p for p in allowed if p["field"].rsplit(".", 1)[0] == parent] or allowed
        gap = {
            "ask_id": "*",
            "verdict": "CARDINALITY_REFUSED",
            "breakdown": a["field"],
            "why": "Any breakdown by %s is refused: this corpus has %d distinct values against a cardinality budget "
                   "of %d declared in catalog.json.%s" % (a["field"], a["distinct"], a["budget"],
                                                          (" Catalog rule: " + rule) if rule else ""),
        }
        if proxies:
            p = proxies[0]
            gap["nearest_proxy"] = "%s (%d distinct values, budget %d)" % (p["field"], p["distinct"], p["budget"])
            gap["why_the_proxy_misleads"] = ("it groups many %s values into a handful of buckets, so it answers a "
                                             "coarser question than the one asked; looking at one value of %s is a "
                                             "record lookup, not a breakdown" % (a["field"].split(".")[-1], a["field"].split(".")[-1]))
        gaps.append(gap)
    zero = [a["field"] for a in audit if a["refused"] and a["budget"] == 0]
    if zero:
        gaps.append({
            "ask_id": "*",
            "verdict": "CARDINALITY_REFUSED",
            "breakdown": ", ".join(zero),
            "why": "Breakdowns by %s are never valid: catalog.json gives them a cardinality budget of 0 (one value "
                   "per row, so every group has one member)." % ", ".join(zero),
        })
    return gaps


def build_gaps(catalog: dict, sessions: List[dict], audit: List[dict], rule: Optional[str]) -> List[dict]:
    gaps = []
    for cap in catalog.get("capabilities", []):
        ask_id = CAPABILITY_TO_ASK.get(cap.get("id"))
        verdict = VERDICT_BY_CLASS.get(cap.get("class"))
        if not ask_id or not verdict:
            continue
        gap = {"ask_id": ask_id, "verdict": verdict,
               "why": cap.get("blocker") or cap.get("note") or
                      "catalog.json declares capability '%s' as class '%s'." % (cap.get("id"), cap.get("class"))}
        proxy, mislead = _split_proxy(cap.get("nearest_proxy"))
        if proxy:
            gap["nearest_proxy"] = proxy
        if mislead:
            gap["why_the_proxy_misleads"] = mislead
        if cap.get("required_event"):
            gap["required_event"] = cap["required_event"]
        elif verdict == "REQUIRES_NEW_JUDGE":
            gap["required_event"] = dict(JUDGE_EVENT_TEMPLATE, name="%s_judgement" % cap.get("id"))

        if cap.get("id") == "abandonment_reason":
            # the measured half of the ask, answered from the logs; the reason stays unjudged
            by_tenant = defaultdict(lambda: [0, 0])
            for s in sessions:
                by_tenant[s["tenant"]][0] += 1
                by_tenant[s["tenant"]][1] += 1 if s["session_end"] == "abandoned" else 0
            gap["measured_part"] = {
                "fidelity": "measured",
                "definition": "session_end = 'abandoned' over all sessions",
                "abandoned_sessions": sum(v[1] for v in by_tenant.values()),
                "abandon_rate_by_tenant": {t: round(a / n, 4) for t, (n, a) in sorted(by_tenant.items()) if n},
                "not_answered": "how many of these left out of frustration — that is a judgement about meaning and "
                                "no calibrated judge for it exists",
            }
            if not gap.get("nearest_proxy"):
                gap["nearest_proxy"] = "session_end = 'abandoned'"
                gap["why_the_proxy_misleads"] = ("it counts everyone who left, including users who got their answer "
                                                 "and simply stopped replying; reporting it as 'frustrated' infers a "
                                                 "reason from an end code (a Rule 1 violation)")
        gaps.append(gap)
    gaps.extend(_cardinality_gaps(audit, rule))
    return gaps
