"""Headline metrics for the report's `metrics[]` array — every one carries
fidelity, coverage (a property of the SCOPE being measured, not a fixed constant)
and, where the fidelity is `judged`, a calibration against the human label set.

Each metric also carries a `result` block with the computed values, so the screen
can show the number next to its definition without recomputing anything.

Order matters for one reason: the scorer reads only the FIRST metric carrying a
given ask_id for the fidelity traps, so the canonical measured tool_failure_rate
(A04) is always listed before the derived silent-empty metric.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from typing import Dict, List

from .cohorts import Rollup

CALIBRATION_TOLERANCE = 1.0      # |judge - human| <= 1 point on the 1-5 scale counts as agreement
CALIBRATION_MIN_N = 30


def tenant_slug(tenant: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", tenant.lower()).strip("_") or "tenant"


def _tenants(sessions: List[dict]) -> List[str]:
    return sorted({s["tenant"] for s in sessions})


def _v3_coverage(sessions: List[dict], tenant: str) -> float:
    ten = [s for s in sessions if s["tenant"] == tenant]
    if not ten:
        return 0.0
    return round(sum(1 for s in ten if s["agent_kind"] == "v3_agent") / len(ten), 4)


def containment_metric(sessions: List[dict]) -> dict:
    r = Rollup(sessions)
    by_tenant = {t: round(Rollup([s for s in sessions if s["tenant"] == t]).containment_rate, 4) for t in _tenants(sessions)}
    return {
        "id": "m_containment",
        "name": "Containment rate",
        "ask_id": "A01",
        "grain": "session",
        "fidelity": "measured",
        "coverage": {
            "value": 1.0,
            "basis": "session_end and handoff_by_design are present on every session, v2_flow and v3_agent "
                     "alike; no session is excluded from this denominator.",
        },
        "calibration": None,
        "plan": {
            "source": "corpus/sessions.jsonl.gz",
            "filter": "session_end = 'resolved' OR (session_end = 'handoff' AND handoff_by_design = true)",
            "denominator": "all sessions in scope",
            "breakdowns": ["tenant", "intent", "channel", "agent_kind", "week"],
            "alternatives_offered": [
                "resolution_rate — counts by-design handoffs as not contained; reported per finding "
                "where it is the more relevant read",
            ],
        },
        "result": {"value": round(r.containment_rate, 4) if r.containment_rate is not None else None,
                   "n_sessions": r.n, "by_tenant": by_tenant},
    }


def resolution_trend_metric(sessions: List[dict]) -> dict:
    cells = defaultdict(lambda: [0, 0])
    for s in sessions:
        c = cells[(s["tenant"], s["intent"], s["week"])]
        c[0] += 1
        c[1] += 1 if s["session_end"] == "resolved" else 0
    stratified = defaultdict(dict)
    for (t, i, w), (n, res) in sorted(cells.items()):
        stratified["%s / %s" % (t, i)]["w%d" % w] = round(res / n, 4) if n else None
    return {
        "id": "m_resolution_trend",
        "name": "Resolution rate by week, stratified by tenant and intent",
        "ask_id": "A02",
        "grain": "session",
        "fidelity": "measured",
        "coverage": {"value": 1.0, "basis": "session_end is present on every session."},
        "calibration": None,
        "plan": {
            "source": "corpus/sessions.jsonl.gz",
            "filter": "session_end = 'resolved'",
            "denominator": "all sessions in the tenant x intent x week cell",
            "breakdowns": ["tenant", "intent", "week"],
            "alternatives_offered": [
                "an unstratified week-over-week trend — rejected as the answer, because a traffic-mix shift "
                "moves the global number without any cohort's own rate changing (see the dismissed "
                "traffic_mix finding).",
            ],
        },
        "result": {"by_cohort_week": dict(stratified)},
    }


def tool_failure_metrics(sessions: List[dict], step_cubes) -> List[dict]:
    out = []
    for t in _tenants(sessions):
        calls = errs = 0
        err_class = defaultdict(int)
        for (tenant, tool), by_day in step_cubes.tool_by_tool.items():
            if tenant != t:
                continue
            for rec in by_day.values():
                calls += rec["calls"]
                errs += rec["err"]
                for k, v in rec["err_class"].items():
                    err_class[k] += v
        out.append({
            "id": "m_tool_failure_" + tenant_slug(t),
            "name": "Tool failure rate — " + t,
            "ask_id": "A04",
            "grain": "step",
            "fidelity": "measured",
            "coverage": {
                "value": _v3_coverage(sessions, t),
                "basis": "share of this tenant's sessions that are v3_agent. v2_flow sessions emit no tool_call "
                         "rows and are excluded from the denominator rather than counted as zero-error.",
                "excluded": ["agent_kind = v2_flow"],
            },
            "calibration": None,
            "plan": {
                "source": "corpus/agent_steps.jsonl.gz WHERE step_type = 'tool_call' AND tenant = '%s'" % t,
                "filter": "outcome IN ('error','timeout','max_iterations','blocked')",
                "denominator": "all tool_call steps for this tenant (v3_agent only — v2_flow emits none)",
                "breakdowns": ["error_class", "tool_name", "channel", "agent_id"],
            },
            "result": {"value": round(errs / calls, 4) if calls else None, "tool_calls": calls, "failed": errs,
                       "by_error_class": dict(sorted(err_class.items()))},
        })
    return out


def silent_tool_metrics(sessions: List[dict], step_cubes) -> List[dict]:
    """The content-aware companion to tool_failure_rate: 'ok' is not the same claim
    as 'returned something the agent could use'. Declared `derived` per catalog.json's
    `silent_tool_success` capability (class derivable_not_declared)."""
    out = []
    for t in _tenants(sessions):
        ok = empty = 0
        for (tenant, tool), by_day in step_cubes.tool_by_tool.items():
            if tenant != t:
                continue
            for rec in by_day.values():
                ok += rec["ok"]
                empty += rec["silent_empty"]
        out.append({
            "id": "m_silent_tool_empty_" + tenant_slug(t),
            "name": "Silent tool-empty rate (ok with no usable payload) — " + t,
            "ask_id": "A04",
            "grain": "step",
            "fidelity": "derived",
            "coverage": {
                "value": _v3_coverage(sessions, t),
                "basis": "same v3_agent-only population as tool_failure_rate; v2_flow emits no tool_call rows.",
                "excluded": ["agent_kind = v2_flow"],
            },
            "calibration": None,
            "plan": {
                "source": "corpus/agent_steps.jsonl.gz WHERE step_type = 'tool_call' AND tenant = '%s'" % t,
                "filter": "outcome = 'ok' AND result_field_count = 0 (response_bytes <= 2 only where result_field_count is absent)",
                "denominator": "all tool_call steps with outcome = 'ok' for this tenant",
                "breakdowns": ["tool_name", "tool_version", "week"],
                "alternatives_offered": [
                    "tool_failure_rate (outcome based) — does not catch this: a call that returns HTTP 200 "
                    "with an empty body is recorded as outcome='ok'",
                ],
            },
            "result": {"value": round(empty / ok, 4) if ok else None, "ok_calls": ok, "empty_ok_calls": empty},
        })
    return out


def kb_metrics(sessions: List[dict], step_cubes) -> List[dict]:
    out = []
    for t in _tenants(sessions):
        lookups = hits = 0
        score_sum = 0.0
        for (tenant, intent), by_day in step_cubes.kb_by_intent.items():
            if tenant != t:
                continue
            for rec in by_day.values():
                lookups += rec["lookups"]
                hits += rec["hits"]
                score_sum += rec["top_score_sum"]
        out.append({
            "id": "m_kb_hit_" + tenant_slug(t),
            "name": "KB hit rate — " + t,
            "ask_id": "A06",
            "grain": "step",
            "fidelity": "measured",
            "coverage": {
                "value": _v3_coverage(sessions, t),
                "basis": "kb_lookup rows only occur on v3_agent sessions.",
                "excluded": ["agent_kind = v2_flow"],
            },
            "calibration": None,
            "plan": {
                "source": "corpus/agent_steps.jsonl.gz WHERE step_type = 'kb_lookup' AND tenant = '%s'" % t,
                "filter": "kb_hit = true",
                "denominator": "all kb_lookup steps for this tenant",
                "breakdowns": ["intent", "week"],
                "alternatives_offered": [
                    "'actually answering what people ask' also has a judged reading (did the returned document "
                    "satisfy the question) — that needs a versioned judge calibrated against labels, which is "
                    "not built; kb_hit / kb_top_score is the measured reading reported here.",
                ],
            },
            "result": {"value": round(hits / lookups, 4) if lookups else None, "lookups": lookups,
                       "mean_kb_top_score": round(score_sum / lookups, 4) if lookups else None},
        })
    return out


def cost_metrics(sessions: List[dict]) -> List[dict]:
    out = []
    for t in _tenants(sessions):
        ten = [s for s in sessions if s["tenant"] == t]
        v3 = [s for s in ten if s["agent_kind"] == "v3_agent"]
        by_intent = defaultdict(lambda: [0.0, 0, 0])
        by_week = defaultdict(float)
        for s in v3:
            c = by_intent[s["intent"]]
            c[0] += s.get("cost_usd") or 0.0
            c[1] += 1
            c[2] += 1 if s["session_end"] == "resolved" else 0
            by_week["w%d" % s["week"]] += s.get("cost_usd") or 0.0
        coverage = {
            "value": round(len(v3) / len(ten), 4) if ten else 0.0,
            "basis": "cost_usd accrues on llm_call steps, which v2_flow sessions do not have; the v2 share of "
                     "this tenant's traffic is a coverage gap, not zero cost.",
            "excluded": ["agent_kind = v2_flow"],
        }
        out.append({
            "id": "m_spend_" + tenant_slug(t),
            "name": "Spend on serving customers — " + t,
            "ask_id": "A08",
            "grain": "session",
            "fidelity": "measured",
            "coverage": dict(coverage),
            "calibration": None,
            "plan": {
                "source": "corpus/sessions.jsonl.gz WHERE tenant = '%s' AND agent_kind = 'v3_agent'" % t,
                "filter": "cost_usd IS NOT NULL",
                "denominator": "v3_agent sessions for this tenant",
                "breakdowns": ["week", "intent"],
            },
            "result": {"total_cost_usd": round(sum(v[0] for v in by_intent.values()), 2), "v3_sessions": len(v3),
                       "by_week_usd": {k: round(v, 2) for k, v in sorted(by_week.items())},
                       "by_intent_usd": {i: round(v[0], 2) for i, v in sorted(by_intent.items())}},
        })
        ranked = sorted(((i, v[0] / v[2], v[2]) for i, v in by_intent.items() if v[2]), key=lambda x: -x[1])
        out.append({
            "id": "m_cost_per_resolved_" + tenant_slug(t),
            "name": "Cost per resolved conversation by intent — " + t,
            "ask_id": "A03",
            "grain": "session",
            "fidelity": "measured",
            "coverage": dict(coverage),
            "calibration": None,
            "plan": {
                "source": "corpus/sessions.jsonl.gz WHERE tenant = '%s' AND agent_kind = 'v3_agent'" % t,
                "filter": "sum(cost_usd) over the intent's v3 sessions / count(session_end = 'resolved')",
                "denominator": "resolved v3_agent sessions of the intent",
                "breakdowns": ["intent"],
                "alternatives_offered": [
                    "cost per session — hides intents that are cheap per attempt but rarely resolve",
                ],
            },
            "result": {"ranked": [{"intent": i, "cost_per_resolved_usd": round(c, 4), "resolved": n} for i, c, n in ranked]},
        })
    return out


def _load_jsonl(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def quality_metric(sessions: List[dict], kit_dir: str) -> dict:
    by_id = {s["session_id"]: s for s in sessions}
    labels = _load_jsonl(os.path.join(kit_dir, "labels", "rubric_scores.jsonl"))
    pairs = defaultdict(list)
    for lab in labels:
        s = by_id.get(lab.get("session_id"))
        if not s or s.get("quality_score") is None or lab.get("human_quality") is None:
            continue
        version = s.get("judge_version")
        if lab.get("judge_version_at_label_time") and lab["judge_version_at_label_time"] != version:
            continue  # a label made under a different rubric cannot calibrate this score
        pairs[version].append(abs(s["quality_score"] - lab["human_quality"]) <= CALIBRATION_TOLERANCE)

    all_pairs = [p for ps in pairs.values() for p in ps]
    calibration = None
    if all_pairs:
        agreement = round(sum(all_pairs) / len(all_pairs), 4)
        calibration = {
            "agreement": agreement,
            "n": len(all_pairs),
            "judge_version": "+".join(sorted(str(v) for v in pairs)),
            "method": "share of human-labelled sessions (labels/rubric_scores.jsonl) where |quality_score - "
                      "human_quality| <= %.1f, each label compared only with a score from the judge_version it "
                      "was labelled under" % CALIBRATION_TOLERANCE,
            "by_judge_version": {str(v): {"agreement": round(sum(ps) / len(ps), 4), "n": len(ps)} for v, ps in sorted(pairs.items())},
        }
        warnings = []
        if len(all_pairs) < CALIBRATION_MIN_N:
            warnings.append("only %d labelled sessions matched this corpus; below %d this agreement is not "
                            "evidence either way" % (len(all_pairs), CALIBRATION_MIN_N))
        if agreement >= 0.995:
            warnings.append("agreement >= 0.995 against labels with ~7.5%% human disagreement is a red flag "
                            "(too few labels, or a leak between judge and labels), not a good judge")
        if warnings:
            calibration["warnings"] = warnings

    by_jv = defaultdict(list)
    for s in sessions:
        if s.get("quality_score") is not None:
            by_jv[s.get("judge_version")].append(s["quality_score"])
    return {
        "id": "m_quality_score",
        "name": "Quality score (judged rubric)",
        "ask_id": "A02",
        "grain": "session",
        "fidelity": "judged",
        "coverage": {"value": round(sum(1 for s in sessions if s.get("quality_score") is not None) / max(1, len(sessions)), 4),
                     "basis": "share of sessions carrying a quality_score."},
        "calibration": calibration,
        "plan": {
            "source": "corpus/sessions.jsonl.gz",
            "filter": "mean quality_score WITHIN one judge_version",
            "denominator": "sessions with a quality_score in the scope and judge_version",
            "breakdowns": ["judge_version", "tenant", "intent", "week"],
            "alternatives_offered": [
                "quality_score is only comparable within one judge_version; a trend across a version boundary "
                "measures the rubric change, not the agent — see the dismissed judge_change finding.",
            ],
        },
        "result": {"mean_by_judge_version": {str(k): round(sum(v) / len(v), 4) for k, v in sorted(by_jv.items()) if v}},
    }


def apply_breakdown_budgets(metrics: List[dict], audit: List[dict]) -> None:
    """Planner guard: a breakdown over its catalog cardinality budget is removed from
    the plan and recorded with the budget cited — refused, never silently truncated."""
    refused = {a["field"].split(".")[-1]: a for a in audit if a["refused"]}
    for m in metrics:
        keep, dropped = [], []
        for b in m["plan"].get("breakdowns", []):
            a = refused.get(b)
            if a:
                dropped.append("%s refused: %s distinct values vs cardinality budget %d (catalog)"
                               % (b, a["distinct"] if a["distinct"] is not None else "unbounded", a["budget"]))
            else:
                keep.append(b)
        m["plan"]["breakdowns"] = keep
        if dropped:
            m["plan"]["refused_breakdowns"] = dropped


def build_headline_metrics(sessions: List[dict], step_cubes, kit_dir: str, cardinality_audit: List[dict]) -> List[dict]:
    metrics = [containment_metric(sessions), resolution_trend_metric(sessions)]
    metrics += tool_failure_metrics(sessions, step_cubes)     # canonical measured A04 first
    metrics += silent_tool_metrics(sessions, step_cubes)
    metrics += kb_metrics(sessions, step_cubes)
    metrics += cost_metrics(sessions)
    metrics.append(quality_metric(sessions, kit_dir))
    apply_breakdown_budgets(metrics, cardinality_audit)
    return metrics
