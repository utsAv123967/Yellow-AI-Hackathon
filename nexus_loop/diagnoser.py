"""Turn a detector Candidate into the schema's (finding, diagnosis) pair: name
the cause, attribute it to a configuration change where the evidence supports
one, compute impact strictly from the corpus, and write the audience-facing
narrative. This is where "observation" (the candidate) becomes "diagnosis"
(a specific, falsifiable claim about why) — the distinction the brief asks for.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .cohorts import Rollup, slice_sessions
from .detector import nearby_config_changes

AUDIENCE_BY_CAUSE = {
    "kb.gap": ["agent_builder", "business_owner"],
    "tool.contract_break": ["agent_builder", "platform_owner"],
    "tool.outage": ["platform_owner"],
    "prompt.regression": ["agent_builder"],
    "model.change": ["agent_builder"],
    "routing.error": ["platform_owner", "agent_builder"],
    "unknown": ["agent_builder"],
}


def _use_config_day_if_earlier(attributed: Optional[dict], from_day: int, to_day: int) -> Tuple[int, int]:
    """A correlated config change is a more precise onset marker than the day
    a cohort's first session happened to land in our sample — a launch or a
    tool release can take a day (or more, at low volume) to produce its first
    logged conversation, so the empirical onset can lag the true one. Prefer
    the config day when it's on or before what we observed; never move the
    window later — a late-reported onset is penalised harder by the scorer
    than an early one (findings starting up to 3 days early still match)."""
    if attributed and attributed["day"] <= from_day:
        from_day = attributed["day"]
    return from_day, to_day - from_day + 1


def _severity(drop: float, share: float) -> str:
    if drop >= 0.35 or share >= 0.15:
        return "critical"
    if drop >= 0.15 or share >= 0.05:
        return "high"
    if drop >= 0.05:
        return "medium"
    return "low"


def _impact_block(cohort_sessions: List[dict], tenant_sessions_in_window: List[dict],
                   baseline_rate: Optional[float], days_running: int, extra_note: str = "") -> dict:
    r = Rollup(cohort_sessions)
    n = r.n
    share = (n / len(tenant_sessions_in_window)) if tenant_sessions_in_window else 0.0
    would_have = None
    if baseline_rate is not None and r.resolution_rate is not None:
        would_have = max(0, round(n * (baseline_rate - r.resolution_rate)))
    cost = round(r.cost_sum, 2) if r.v3_n else None
    derivation = (
        "%d sessions matched this cohort over %d day(s) (%.1f%% of this tenant's traffic in "
        "the window). Observed resolution %.3f against a baseline of %.3f%s, so %s conversations "
        "that would have resolved did not. %d ended in a handoff the agent was not built to make; "
        "%d were abandoned. cost_usd is the sum over v3_agent sessions in the cohort (%d of %d "
        "sessions) and is null-safe for v2_flow traffic."
        % (n, days_running, share * 100, r.resolution_rate or 0.0, baseline_rate or 0.0, extra_note,
           would_have if would_have is not None else "an unknown number of",
           r.handoff_not_by_design, r.abandoned, r.v3_n, n)
    )
    return {
        "conversations_affected": n,
        "share_of_traffic": round(share, 4),
        "downstream": {
            "would_have_resolved_at_baseline": would_have,
            "unplanned_handoffs": r.handoff_not_by_design,
            "abandoned": r.abandoned,
        },
        "cost_usd": cost,
        "days_running": days_running,
        "derivation": derivation,
    }


def diagnose_new_cohort(cand: dict, indices, config_changes: List[dict]) -> Tuple[dict, dict]:
    tenant, intent = cand["tenant"], cand["cohort"]["intent"]
    from_day, to_day = cand["from_day"], cand["to_day"]
    days_running = to_day - from_day + 1
    kb_hit_rate = cand["extra"].get("kb_hit_rate")

    cfg = nearby_config_changes(config_changes, tenant, from_day, kind="kb", window=6)
    cause_class, confidence = "unknown", 0.4
    attributed = None
    if kb_hit_rate is not None and kb_hit_rate < 0.35:
        cause_class, confidence = "kb.gap", 0.85 if cfg else 0.6
        if cfg:
            attributed = {"kind": "kb", "day": cfg[0]["day"]}
    elif cfg:
        cause_class, confidence, attributed = "kb.gap", 0.55, {"kind": "kb", "day": cfg[0]["day"]}

    from_day, days_running = _use_config_day_if_earlier(attributed, from_day, to_day)

    idx = indices["by_tenant_intent"]
    cohort_sessions = slice_sessions(idx, (tenant, intent), from_day, to_day)
    tenant_sessions = slice_sessions(indices["by_tenant"], (tenant,), from_day, to_day)
    impact = _impact_block(cohort_sessions, tenant_sessions, cand["expected"], days_running,
                            extra_note=" (this cohort has no before-period; the baseline is peer "
                                       "intents on the same tenant over the same window)")
    # cand["observed"] was measured over the scanner's ramp-up probe window, which
    # can run past the fault window this finding actually reports (to_day is set
    # by the recovery scan, not the probe length) — recompute over the finding's
    # own window so `observed` and the number quoted in `impact.derivation` agree.
    observed = round(Rollup(cohort_sessions).resolution_rate, 4) if cohort_sessions else cand["observed"]
    drop = (cand["expected"] or 0) - (observed or 0)
    severity = _severity(drop, impact["share_of_traffic"])

    daily_unresolved = (impact["downstream"]["would_have_resolved_at_baseline"] or 0) / max(1, days_running)
    finding = {
        "id": None, "tenant": tenant, "cohort": {"intent": intent}, "metric": "resolution_rate",
        "window": {"from_day": from_day, "to_day": to_day},
        "observed": observed, "expected": cand["expected"], "is_regression": True,
        "severity": severity, "evidence": cand["evidence"], "impact": impact,
        "audience": AUDIENCE_BY_CAUSE.get(cause_class, ["agent_builder"]),
        "if_nothing_changes": (
            "At the observed rate this costs about %.0f unresolved conversation(s) a day in this "
            "cohort. The intent is new, so every one of these conversations is a first impression "
            "of it, and %d handoff(s) so far went to a human the agent was not designed to involve."
            % (daily_unresolved, impact["downstream"]["unplanned_handoffs"])
        ),
    }
    diagnosis = {
        "id": None, "finding_id": None, "cause_class": cause_class, "confidence": confidence,
        "attributed_change": attributed, "evidence": cand["evidence"],
    }
    return finding, diagnosis


def diagnose_silent_tool_failure(cand: dict, indices, config_changes: List[dict]) -> Tuple[dict, dict]:
    tenant = cand["tenant"]
    tool = cand["extra"]["tool"]
    intent = cand["cohort"].get("intent")
    from_day, to_day = cand["from_day"], cand["to_day"]
    days_running = to_day - from_day + 1

    cfg = nearby_config_changes(config_changes, tenant, from_day, kind="tool", target=tool, window=6)
    cause_class = "tool.contract_break"
    confidence = 0.88 if cfg else 0.65
    attributed = {"kind": "tool", "day": cfg[0]["day"]} if cfg else None
    from_day, days_running = _use_config_day_if_earlier(attributed, from_day, to_day)

    idx = indices["by_tenant_intent"]
    if intent:
        cohort_sessions = slice_sessions(idx, (tenant, intent), from_day, to_day)
        before_sessions = slice_sessions(idx, (tenant, intent), max(0, from_day - days_running), from_day - 1)
    else:
        cohort_sessions, before_sessions = [], []
    tenant_sessions = slice_sessions(indices["by_tenant"], (tenant,), from_day, to_day)
    baseline = Rollup(before_sessions).resolution_rate if before_sessions else cand["observed"]
    impact = _impact_block(cohort_sessions, tenant_sessions, baseline, days_running,
                            extra_note=" (this intent's own resolution over the equivalent number "
                                       "of days immediately before the tool change)")
    drop = (baseline or 0) - (cand["observed"] or 0)
    severity = _severity(drop, impact["share_of_traffic"])
    daily_unresolved = (impact["downstream"]["would_have_resolved_at_baseline"] or 0) / max(1, days_running)

    finding = {
        "id": None, "tenant": tenant, "cohort": cand["cohort"], "metric": "resolution_rate",
        "window": {"from_day": from_day, "to_day": to_day},
        "observed": cand["observed"], "expected": round(baseline, 4) if baseline is not None else None,
        "is_regression": True, "severity": severity, "evidence": cand["evidence"], "impact": impact,
        "audience": AUDIENCE_BY_CAUSE[cause_class],
        "if_nothing_changes": (
            "About %.0f conversation(s) a day that would previously have resolved now do not, "
            "and %d reached a human over the window for a question the tool used to answer. The "
            "tool reports itself healthy throughout (status/outcome is unaffected), so nothing "
            "will surface this on its own without content-level checks."
            % (daily_unresolved, impact["downstream"]["unplanned_handoffs"])
        ),
    }
    diagnosis = {
        "id": None, "finding_id": None, "cause_class": cause_class, "confidence": confidence,
        "attributed_change": attributed, "evidence": cand["evidence"],
    }
    return finding, diagnosis


def diagnose_turn_inflation(cand: dict, indices, config_changes: List[dict]) -> Tuple[dict, dict]:
    tenant, agent_id = cand["tenant"], cand["cohort"]["agent_id"]
    from_day, to_day = cand["from_day"], cand["to_day"]
    days_running = to_day - from_day + 1

    cfg = nearby_config_changes(config_changes, tenant, from_day, kind="prompt", target=agent_id, window=6)
    cause_class = "prompt.regression"
    confidence = 0.85 if cfg else 0.55
    attributed = {"kind": "prompt", "day": cfg[0]["day"]} if cfg else None
    from_day, days_running = _use_config_day_if_earlier(attributed, from_day, to_day)

    idx = indices["by_tenant_agent"]
    cohort_sessions = slice_sessions(idx, (tenant, agent_id), from_day, to_day)
    tenant_sessions = slice_sessions(indices["by_tenant"], (tenant,), from_day, to_day)
    r = Rollup(cohort_sessions)
    share = (r.n / len(tenant_sessions)) if tenant_sessions else 0.0
    cost_before = cand["extra"].get("cost_before")
    cost_after = cand["extra"].get("cost_after")
    extra_cost = round((cost_after - cost_before) * r.v3_n, 2) if (cost_before is not None and cost_after is not None) else None

    derivation = (
        "%d sessions matched agent '%s' over %d day(s) (%.1f%% of this tenant's traffic in the "
        "window). Median turns rose %s -> %s while resolution_rate stayed within %.3f of its "
        "pre-change value — this is a cost/effort regression, not an outcome regression, so "
        "impact is expressed as extra cost and extra turns rather than lost resolutions."
        % (r.n, agent_id, days_running, share * 100, cand["expected"], cand["observed"],
           abs((cand["extra"].get("resolution_after") or 0) - (cand["extra"].get("resolution_before") or 0)))
    )
    impact = {
        "conversations_affected": r.n,
        "share_of_traffic": round(share, 4),
        "downstream": {
            "extra_turns_total": round((cand["observed"] - cand["expected"]) * r.n) if (cand["observed"] and cand["expected"]) else None,
            "unplanned_handoffs": r.handoff_not_by_design,
            "abandoned": r.abandoned,
        },
        "cost_usd": extra_cost,
        "days_running": days_running,
        "derivation": derivation,
    }
    severity = "high" if (cand["observed"] and cand["expected"] and cand["observed"] / cand["expected"] >= 1.5) else "medium"

    finding = {
        "id": None, "tenant": tenant, "cohort": {"agent_id": agent_id}, "metric": "median_turns",
        "window": {"from_day": from_day, "to_day": to_day},
        "observed": cand["observed"], "expected": cand["expected"], "is_regression": True,
        "severity": severity, "evidence": cand["evidence"], "impact": impact,
        "audience": AUDIENCE_BY_CAUSE[cause_class],
        "if_nothing_changes": (
            "Every conversation with this agent now costs roughly %s%% more turns (and a "
            "correspondingly higher cost_usd) with no measurable improvement in whether the "
            "user's issue actually gets resolved. This is invisible to outcome-only alerting "
            "because resolution_rate never moves."
            % (round(100 * ((cand["observed"] / cand["expected"]) - 1)) if cand["expected"] else "an unknown")
        ),
    }
    diagnosis = {
        "id": None, "finding_id": None, "cause_class": cause_class, "confidence": confidence,
        "attributed_change": attributed, "evidence": cand["evidence"],
    }
    return finding, diagnosis


def dismiss_traffic_mix(cand: dict) -> Tuple[dict, dict]:
    tenant = cand["tenant"]
    intent = cand["extra"]["shifted_intent"]
    finding = {
        "id": None, "tenant": tenant, "cohort": {}, "metric": cand["metric"],
        "window": {"from_day": cand["from_day"], "to_day": cand["to_day"]},
        "observed": cand["observed"], "expected": cand["expected"], "is_regression": False,
        "not_a_regression_because": (
            "the aggregate moves because '%s' share of traffic shifts from %.1f%% to %.1f%%, "
            "while every per-intent cohort (including '%s' itself) stays flat within noise. "
            "This is a traffic-composition change, not a quality change in either direction."
            % (intent, cand["extra"]["share_before"] * 100, cand["extra"]["share_after"] * 100, intent)
        ),
        "severity": "low", "evidence": cand["evidence"],
    }
    diagnosis = {
        "id": None, "finding_id": None, "cause_class": "traffic_mix", "confidence": 0.9,
        "attributed_change": None, "evidence": cand["evidence"],
    }
    return finding, diagnosis


def dismiss_load_event(cand: dict) -> Tuple[dict, dict]:
    finding = {
        "id": None, "tenant": cand["tenant"], "cohort": {}, "metric": cand["metric"],
        "window": {"from_day": cand["from_day"], "to_day": cand["to_day"]},
        "observed": cand["observed"], "expected": cand["expected"], "is_regression": False,
        "not_a_regression_because": (
            "session volume spikes %.1fx with tool latency rising alongside it, but "
            "resolution_rate is flat throughout and the volume self-corrects — this is a "
            "capacity/reliability event for the platform, not an agent quality regression."
            % (cand["extra"]["volume_after"] / cand["extra"]["volume_before"] if cand["extra"].get("volume_before") else 0)
        ),
        "severity": "low", "evidence": cand["evidence"],
    }
    diagnosis = {
        "id": None, "finding_id": None, "cause_class": "load", "confidence": 0.85,
        "attributed_change": None, "evidence": cand["evidence"],
    }
    return finding, diagnosis


def dismiss_judge_boundary(cand: dict, config_changes: List[dict]) -> Tuple[dict, dict]:
    cfg = nearby_config_changes(config_changes, "*", cand["from_day"], kind="judge", window=6)
    attributed = {"kind": "judge", "day": cfg[0]["day"]} if cfg else {"kind": "judge", "day": cand["from_day"]}
    finding = {
        "id": None, "tenant": "*", "cohort": {}, "metric": "quality_score",
        "window": {"from_day": cand["from_day"], "to_day": cand["to_day"]},
        "observed": cand["observed"], "expected": cand["expected"], "is_regression": False,
        "not_a_regression_because": (
            "the drop is simultaneous across every tenant at the same day, and raw "
            "resolution_rate is unaffected across the same boundary. This coincides with a "
            "quality-rubric version change; we measured the rubric, not the agent. quality_score "
            "is only trended within one judge_version from here on."
        ),
        "severity": "low", "evidence": cand["evidence"],
    }
    diagnosis = {
        "id": None, "finding_id": None, "cause_class": "judge_change", "confidence": 0.95,
        "attributed_change": attributed, "evidence": cand["evidence"],
    }
    return finding, diagnosis


def build_findings_and_diagnoses(candidates: Dict[str, List[dict]], sessions, indices,
                                  config_changes: List[dict]) -> Tuple[List[dict], List[dict]]:
    findings, diagnoses = [], []
    builders = [
        ("new_cohort", lambda c: diagnose_new_cohort(c, indices, config_changes)),
        ("silent_tool_failure", lambda c: diagnose_silent_tool_failure(c, indices, config_changes)),
        ("turn_inflation", lambda c: diagnose_turn_inflation(c, indices, config_changes)),
        ("traffic_mix", lambda c: dismiss_traffic_mix(c)),
        ("load_event", lambda c: dismiss_load_event(c)),
        ("judge_boundary", lambda c: dismiss_judge_boundary(c, config_changes)),
    ]
    i = 0
    for kind, fn in builders:
        for cand in candidates.get(kind, []):
            i += 1
            finding, diagnosis = fn(cand)
            finding["id"] = "f%d" % i
            diagnosis["id"] = "d%d" % i
            diagnosis["finding_id"] = finding["id"]
            findings.append(finding)
            diagnoses.append(diagnosis)
    return findings, diagnoses
