"""Turn a detector Candidate into the schema's (finding, diagnosis) pair: name
the cause, attribute it to a configuration change where the evidence supports
one, compute impact strictly from the corpus, and write the audience-facing
narrative. This is where "observation" (the candidate) becomes "diagnosis"
(a specific, falsifiable claim about why) — the distinction the brief asks for.

Every sentence here is formatted from a computed number or a config_timeline
row. Nothing is asserted that the code did not measure.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .changepoint import z_two_proportions
from .cohorts import Rollup, slice_sessions
from .detector import RESOLUTION_FLAT_TOL, _fmt_z, nearby_config_changes

AUDIENCE_BY_CAUSE = {
    "kb.gap": ["agent_builder", "business_owner"],
    "tool.contract_break": ["platform_owner", "agent_builder"],
    "tool.outage": ["platform_owner"],
    "prompt.regression": ["agent_builder"],
    "model.change": ["agent_builder"],
    "routing.error": ["platform_owner", "agent_builder"],
    "unknown": ["agent_builder"],
}
CONFIG_ATTRIBUTION_WINDOW = 6


def _config_line(row: dict) -> str:
    return ("config_timeline day %d: %s change on tenant '%s', target '%s', %s -> %s (\"%s\")"
            % (row["day"], row["kind"], row["tenant"], row.get("target"), row.get("from_value"),
               row.get("to_value"), row.get("note")))


def _config_signal(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return None
    return {k: row.get(k) for k in ("day", "tenant", "kind", "target", "from_value", "to_value", "note")}


def _use_config_day_if_earlier(attributed: Optional[dict], from_day: int, to_day: int) -> Tuple[int, int]:
    """A correlated config change is a more precise onset marker than the day a
    cohort's first affected session happened to land in the log — a launch or a
    release can take a day to produce its first conversation. Prefer the config
    day when it is on or before what we observed; never move the window later."""
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
                   baseline_rate: Optional[float], days_running: int, baseline_note: str) -> dict:
    r = Rollup(cohort_sessions)
    n = r.n
    share = (n / len(tenant_sessions_in_window)) if tenant_sessions_in_window else 0.0
    would_have = None
    if baseline_rate is not None and r.resolution_rate is not None:
        would_have = max(0, round(n * (baseline_rate - r.resolution_rate)))
    cost = round(r.cost_sum, 2) if r.v3_n else None
    derivation = (
        "%d sessions matched this cohort over %d day(s), %.1f%% of this tenant's %d sessions in the same days. "
        "Observed resolution_rate %.3f against a baseline of %.3f — %s. would_have_resolved_at_baseline = "
        "round(%d x (%.3f - %.3f)) = %s. unplanned_handoffs counts session_end='handoff' with handoff_by_design "
        "not true (%d); abandoned counts session_end='abandoned' (%d). cost_usd sums cost_usd over the %d "
        "v3_agent sessions of %d (v2_flow emits no llm_call steps, so it is excluded, not counted as zero)."
        % (n, days_running, share * 100, len(tenant_sessions_in_window), r.resolution_rate or 0.0,
           baseline_rate or 0.0, baseline_note, n, baseline_rate or 0.0, r.resolution_rate or 0.0,
           would_have if would_have is not None else "unknown", r.handoff_not_by_design, r.abandoned, r.v3_n, n)
    )
    return {
        "conversations_affected": n,
        "share_of_traffic": round(share, 4),
        "downstream": {
            "would_have_resolved_at_baseline": would_have,
            "actually_resolved": r.resolved,
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
    ex = cand["extra"]
    kb_hit_rate = ex.get("kb_hit_rate")

    cfg = nearby_config_changes(config_changes, tenant, from_day, kind="kb", window=CONFIG_ATTRIBUTION_WINDOW)
    cause_class, confidence, attributed, cfg_row = "unknown", 0.4, None, None
    if kb_hit_rate is not None and kb_hit_rate < 0.35:
        cause_class, confidence = "kb.gap", 0.85 if cfg else 0.6
    elif cfg:
        cause_class, confidence = "kb.gap", 0.55
    if cause_class == "kb.gap" and cfg:
        cfg_row = cfg[0]
        attributed = {"kind": "kb", "day": cfg_row["day"]}

    from_day, days_running = _use_config_day_if_earlier(attributed, from_day, to_day)

    idx = indices["by_tenant_intent"]
    cohort_sessions = slice_sessions(idx, (tenant, intent), from_day, to_day)
    tenant_sessions = slice_sessions(indices["by_tenant"], (tenant,), from_day, to_day)
    # the scanner compared over its ramp-up probe; recompute cohort AND peers over the
    # finding's own window so evidence, observed/expected and impact all quote one window
    mature = ex.get("mature_intents") or []
    peer = Rollup([s for m in mature for s in slice_sessions(idx, (tenant, m), from_day, to_day)])
    cohort_r = Rollup(cohort_sessions)
    expected = round(peer.resolution_rate, 4) if peer.resolution_rate is not None else cand["expected"]
    observed = round(cohort_r.resolution_rate, 4) if cohort_r.n else cand["observed"]
    z = z_two_proportions(cohort_r.resolved, cohort_r.n, peer.resolved, peer.n)
    impact = _impact_block(cohort_sessions, tenant_sessions, expected, days_running,
                            "this intent is new, so it has no before-period; the baseline is the tenant's %d mature "
                            "intents over the same days" % len(mature))
    drop = (expected or 0) - (observed or 0)
    evidence = [cand["evidence"][0],
                "cohort resolution %.3f (%d sessions) vs the mature intents over the same days %d-%d: %.3f (%d sessions); z = %s"
                % (observed or 0.0, cohort_r.n, from_day, to_day, expected or 0.0, peer.n, _fmt_z(z))]
    evidence += cand["evidence"][2:]
    evidence += [_config_line(cfg_row)] if cfg_row else \
        ["no kb change within %d days of the launch in config_timeline" % CONFIG_ATTRIBUTION_WINDOW]

    daily_unresolved = (impact["downstream"]["would_have_resolved_at_baseline"] or 0) / max(1, days_running)
    finding = {
        "id": None, "tenant": tenant, "cohort": {"intent": intent}, "metric": "resolution_rate",
        "window": {"from_day": from_day, "to_day": to_day},
        "observed": observed, "expected": expected, "is_regression": True,
        "severity": _severity(drop, impact["share_of_traffic"]), "evidence": evidence, "impact": impact,
        "audience": AUDIENCE_BY_CAUSE.get(cause_class, ["agent_builder"]),
        "if_nothing_changes": (
            "About %.0f conversation(s) a day about '%s' keep failing that peer intents would resolve, and %d "
            "have already gone to a human the agent was not built to involve — every one of them a customer's "
            "first contact with a newly launched product."
            % (daily_unresolved, intent, impact["downstream"]["unplanned_handoffs"])
        ),
    }
    diagnosis = {
        "id": None, "finding_id": None, "cause_class": cause_class, "confidence": confidence,
        "attributed_change": attributed, "evidence": evidence,
        "signals": {"kb_hit_rate": kb_hit_rate, "kb_top_score_mean": ex.get("kb_avg_score"),
                    "kb_top_score_min": ex.get("kb_score_min"), "kb_top_score_max": ex.get("kb_score_max"),
                    "kb_lookups": ex.get("kb_lookups"), "tool_calls": ex.get("tool_calls"),
                    "z": z, "config_change": _config_signal(cfg_row)},
    }
    return finding, diagnosis


def diagnose_silent_tool_failure(cand: dict, indices, config_changes: List[dict]) -> Tuple[dict, dict]:
    tenant = cand["tenant"]
    ex = cand["extra"]
    tool = ex["tool"]
    intent = cand["cohort"].get("intent")
    from_day, to_day = cand["from_day"], cand["to_day"]

    cfg = nearby_config_changes(config_changes, tenant, from_day, kind="tool", target=tool, window=CONFIG_ATTRIBUTION_WINDOW)
    cfg_row = cfg[0] if cfg else None
    cause_class = "tool.contract_break"
    confidence = 0.88 if cfg_row else 0.65
    attributed = {"kind": "tool", "day": cfg_row["day"]} if cfg_row else None
    from_day, days_running = _use_config_day_if_earlier(attributed, from_day, to_day)

    idx = indices["by_tenant_intent"]
    if intent:
        cohort_sessions = slice_sessions(idx, (tenant, intent), from_day, to_day)
        before_lo = max(0, from_day - days_running)
        before_sessions = slice_sessions(idx, (tenant, intent), before_lo, from_day - 1)
    else:
        cohort_sessions, before_sessions, before_lo = [], [], from_day
    tenant_sessions = slice_sessions(indices["by_tenant"], (tenant,), from_day, to_day)
    baseline = Rollup(before_sessions).resolution_rate if before_sessions else None
    observed = Rollup(cohort_sessions).resolution_rate if cohort_sessions else cand["observed"]
    impact = _impact_block(cohort_sessions, tenant_sessions, baseline, days_running,
                            "this cohort looks like its peers today, so the baseline is its OWN resolution over "
                            "days %d-%d, the same number of days immediately before the change" % (before_lo, from_day - 1))
    impact["downstream"]["empty_ok_responses"] = ex.get("empty_responses")
    impact["downstream"]["tool_retries"] = ex.get("retries_during")
    drop = (baseline or 0) - (observed or 0)
    evidence = list(cand["evidence"]) + ([_config_line(cfg_row)] if cfg_row else
                                         ["no kind='tool' change targeting '%s' within %d days" % (tool, CONFIG_ATTRIBUTION_WINDOW)])
    daily_unresolved = (impact["downstream"]["would_have_resolved_at_baseline"] or 0) / max(1, days_running)

    finding = {
        "id": None, "tenant": tenant, "cohort": cand["cohort"], "metric": "resolution_rate",
        "window": {"from_day": from_day, "to_day": to_day},
        "observed": round(observed, 4) if observed is not None else None,
        "expected": round(baseline, 4) if baseline is not None else None,
        "is_regression": True, "severity": _severity(drop, impact["share_of_traffic"]),
        "evidence": evidence, "impact": impact,
        "audience": AUDIENCE_BY_CAUSE[cause_class],
        "if_nothing_changes": (
            "About %.0f conversation(s) a day that used to resolve now do not, and %d reached a human over the "
            "window, while every error-rate dashboard for '%s' stays green because the calls report success."
            % (daily_unresolved, impact["downstream"]["unplanned_handoffs"], tool)
        ),
    }
    if finding["observed"] is None:
        finding.pop("observed")
    if finding["expected"] is None:
        finding.pop("expected")
    diagnosis = {
        "id": None, "finding_id": None, "cause_class": cause_class, "confidence": confidence,
        "attributed_change": attributed, "evidence": evidence,
        "signals": {"tool": tool, "empty_ok_rate_before": ex.get("silent_before"), "empty_ok_rate_after": ex.get("silent_after"),
                    "declared_error_rate_before": ex.get("declared_before"), "declared_error_rate_during": ex.get("declared_during"),
                    "retries_before": ex.get("retries_before"), "retries_during": ex.get("retries_during"),
                    "intent_share_of_calls": ex.get("intent_share_of_calls"), "z": ex.get("z"),
                    "config_change": _config_signal(cfg_row)},
    }
    return finding, diagnosis


def diagnose_turn_inflation(cand: dict, indices, config_changes: List[dict]) -> Tuple[dict, dict]:
    tenant, agent_id = cand["tenant"], cand["cohort"]["agent_id"]
    ex = cand["extra"]
    from_day, to_day = cand["from_day"], cand["to_day"]

    cfg = nearby_config_changes(config_changes, tenant, from_day, kind="prompt", target=agent_id, window=CONFIG_ATTRIBUTION_WINDOW)
    cfg_row = cfg[0] if cfg else None
    if cfg_row:
        cause_class, confidence = "prompt.regression", 0.85
    else:
        # turns rose with outcomes flat, but no prompt change explains it: a model
        # change on the same agent is the other config that alters behaviour
        model_cfg = nearby_config_changes(config_changes, tenant, from_day, kind="model", target=agent_id,
                                          window=CONFIG_ATTRIBUTION_WINDOW)
        if model_cfg:
            cfg_row, cause_class, confidence = model_cfg[0], "model.change", 0.6
        else:
            cause_class, confidence = "prompt.regression", 0.5
    attributed = {"kind": cfg_row["kind"], "day": cfg_row["day"]} if cfg_row else None
    from_day, days_running = _use_config_day_if_earlier(attributed, from_day, to_day)

    idx = indices["by_tenant_agent"]
    cohort_sessions = slice_sessions(idx, (tenant, agent_id), from_day, to_day)
    tenant_sessions = slice_sessions(indices["by_tenant"], (tenant,), from_day, to_day)
    r = Rollup(cohort_sessions)
    share = (r.n / len(tenant_sessions)) if tenant_sessions else 0.0
    cost_before, cost_after = ex.get("cost_before"), ex.get("cost_after")
    extra_cost = round((cost_after - cost_before) * r.v3_n, 2) if (cost_before is not None and cost_after is not None) else None
    base_lo, base_hi = ex.get("baseline_window", [None, None])
    observed = r.median_turns if r.n else cand["observed"]
    extra_turns = round((r.mean_turns - ex["mean_turns_before"]) * r.n) if r.n else None

    derivation = (
        "%d sessions on agent '%s' over %d day(s), %.1f%% of this tenant's %d sessions in the same days. Baseline: this "
        "agent's own sessions on days %s-%s — its outcomes look like its peers', so only its own past shows the change. "
        "Median turns %s -> %s (mean %.2f -> %.2f); extra_turns_total = round((%.2f - %.2f) x %d) = %s. "
        "resolution_rate %.3f before vs %.3f during, so impact is effort and cost, not lost resolutions. "
        "cost_usd = (per-session cost during %s - before %s) x %d v3_agent sessions."
        % (r.n, agent_id, days_running, share * 100, len(tenant_sessions), base_lo, base_hi,
           cand["expected"], observed, ex["mean_turns_before"], r.mean_turns or 0.0, r.mean_turns or 0.0,
           ex["mean_turns_before"], r.n, extra_turns, ex.get("resolution_before") or 0.0, r.resolution_rate or 0.0,
           "%.4f" % cost_after if cost_after is not None else "n/a", "%.4f" % cost_before if cost_before is not None else "n/a", r.v3_n)
    )
    impact = {
        "conversations_affected": r.n,
        "share_of_traffic": round(share, 4),
        "downstream": {
            "extra_turns_total": extra_turns,
            "unplanned_handoffs": r.handoff_not_by_design,
            "abandoned": r.abandoned,
        },
        "cost_usd": extra_cost,
        "days_running": days_running,
        "derivation": derivation,
    }
    ratio = (ex["mean_turns_after"] / ex["mean_turns_before"]) if ex.get("mean_turns_before") else None
    evidence = list(cand["evidence"]) + ([_config_line(cfg_row)] if cfg_row else
                                         ["no prompt or model change on '%s' within %d days" % (agent_id, CONFIG_ATTRIBUTION_WINDOW)])
    finding = {
        "id": None, "tenant": tenant, "cohort": {"agent_id": agent_id}, "metric": "median_turns",
        "window": {"from_day": from_day, "to_day": to_day},
        "observed": observed, "expected": cand["expected"], "is_regression": True,
        "severity": "high" if ratio and ratio >= 1.5 else "medium", "evidence": evidence, "impact": impact,
        "audience": AUDIENCE_BY_CAUSE[cause_class],
        "if_nothing_changes": (
            "Every conversation with '%s' keeps taking about %s%% more turns%s with no gain in resolution — and "
            "no outcome alert will ever fire, because resolution_rate does not move."
            % (agent_id, round(100 * (ratio - 1)) if ratio else "an unknown share of",
               (" and roughly $%.2f more over the window" % extra_cost) if extra_cost is not None else "")
        ),
    }
    diagnosis = {
        "id": None, "finding_id": None, "cause_class": cause_class, "confidence": confidence,
        "attributed_change": attributed, "evidence": evidence,
        "signals": {"mean_turns_before": ex.get("mean_turns_before"), "mean_turns_after": ex.get("mean_turns_after"),
                    "resolution_before": ex.get("resolution_before"), "resolution_after": ex.get("resolution_after"),
                    "cost_per_session_before": cost_before, "cost_per_session_after": cost_after,
                    "z": ex.get("z"), "config_change": _config_signal(cfg_row)},
    }
    return finding, diagnosis


def dismiss_traffic_mix(cand: dict, config_changes: List[dict]) -> Tuple[dict, dict]:
    tenant = cand["tenant"]
    ex = cand["extra"]
    intent = ex["shifted_intent"]
    quality_markers = [c for c in nearby_config_changes(config_changes, tenant, cand["from_day"], window=CONFIG_WINDOW_FOR_DISMISSAL)
                       if c["kind"] in ("kb", "prompt", "model", "tool", "routing")]
    others = ex.get("max_other_intent_move")
    evidence = list(cand["evidence"]) + [
        "config changes within %d days that could alter quality: %s"
        % (CONFIG_WINDOW_FOR_DISMISSAL, "; ".join(_config_line(c) for c in quality_markers) if quality_markers else "none")]
    finding = {
        "id": None, "tenant": tenant, "cohort": {}, "metric": cand["metric"],
        "window": {"from_day": cand["from_day"], "to_day": cand["to_day"]},
        "observed": cand["observed"], "expected": cand["expected"], "is_regression": False,
        "not_a_regression_because": (
            "the tenant aggregate moves (%.3f -> %.3f) because '%s' goes from %.1f%% to %.1f%% of traffic, while "
            "'%s' itself resolves at %.3f before and %.3f during%s. Stratified by intent nothing moved, so this is a "
            "change in who is asking, not in how well the agent answers."
            % (cand["expected"] or 0, cand["observed"] or 0, intent, ex["share_before"] * 100, ex["share_after"] * 100,
               intent, ex["own_rate_before"], ex["own_rate_during"],
               (" and no other intent's rate moves by more than %.3f" % others) if others is not None else "")
        ),
        "severity": "low", "evidence": evidence,
    }
    diagnosis = {
        "id": None, "finding_id": None, "cause_class": "traffic_mix", "confidence": 0.9 if not quality_markers else 0.75,
        "attributed_change": None, "evidence": evidence,
        "signals": {"shifted_intent": intent, "share_before": ex["share_before"], "share_after": ex["share_after"],
                    "max_other_intent_move": others},
    }
    return finding, diagnosis


def dismiss_load_event(cand: dict) -> Tuple[dict, dict]:
    ex = cand["extra"]
    mult = ex["volume_after"] / ex["volume_before"] if ex.get("volume_before") else 0
    latency = ((" and tool p95 latency goes %d -> %d ms" % (ex["p95_before"], ex["p95_during"]))
               if ex.get("p95_before") and ex.get("p95_during") else "")
    finding = {
        "id": None, "tenant": cand["tenant"], "cohort": {}, "metric": cand["metric"],
        "window": {"from_day": cand["from_day"], "to_day": cand["to_day"]},
        "observed": cand["observed"], "expected": cand["expected"], "is_regression": False,
        "not_a_regression_because": (
            "session volume rises %.1fx%s, but resolution_rate is %.3f during vs %.3f before — inside the %.2f "
            "band a quality regression would have to break%s. That is a capacity event for the platform team to "
            "review, not a change in agent quality."
            % (mult, latency, cand["observed"], cand["expected"], RESOLUTION_FLAT_TOL,
               ", and volume returns to baseline inside the log" if ex.get("recovered") else "")
        ),
        "severity": "low", "evidence": cand["evidence"],
    }
    diagnosis = {
        "id": None, "finding_id": None, "cause_class": "load", "confidence": 0.85,
        "attributed_change": None, "evidence": cand["evidence"],
        "signals": {"volume_before": ex.get("volume_before"), "volume_after": ex.get("volume_after"),
                    "p95_before_ms": ex.get("p95_before"), "p95_during_ms": ex.get("p95_during")},
    }
    return finding, diagnosis


def dismiss_judge_boundary(cand: dict, config_changes: List[dict]) -> Optional[Tuple[dict, dict]]:
    ex = cand["extra"]
    cfg = nearby_config_changes(config_changes, "*", cand["from_day"], kind="judge", window=CONFIG_ATTRIBUTION_WINDOW)
    version_changed = ex.get("judge_version_before") != ex.get("judge_version_after")
    if not cfg and not version_changed:
        return None  # no rubric evidence at all — do not explain a drop away as a judge change
    cfg_row = cfg[0] if cfg else None
    attributed = {"kind": "judge", "day": cfg_row["day"] if cfg_row else cand["from_day"]}
    evidence = list(cand["evidence"]) + ([_config_line(cfg_row)] if cfg_row else
                                         ["no judge row in config_timeline; the version change is read from judge_version on the sessions"])
    finding = {
        "id": None, "tenant": "*", "cohort": {}, "metric": "quality_score",
        "window": {"from_day": cand["from_day"], "to_day": cand["to_day"]},
        "observed": cand["observed"], "expected": cand["expected"], "is_regression": False,
        "not_a_regression_because": (
            "quality_score falls %.2f -> %.2f in every tenant on the same day, resolution_rate does not move, and "
            "the scores switch from judge_version '%s' to '%s'. We measured the rubric changing, not the agent; "
            "quality_score is only trended within one judge_version from here on."
            % (cand["expected"], cand["observed"], ex.get("judge_version_before"), ex.get("judge_version_after"))
        ),
        "severity": "low", "evidence": evidence,
    }
    diagnosis = {
        "id": None, "finding_id": None, "cause_class": "judge_change", "confidence": 0.95 if cfg_row else 0.8,
        "attributed_change": attributed, "evidence": evidence,
        "signals": {"judge_version_before": ex.get("judge_version_before"), "judge_version_after": ex.get("judge_version_after"),
                    "per_tenant_mean_quality": {t: {"before": round(b, 3), "after": round(a, 3)} for t, (b, a) in ex.get("per_tenant", {}).items()},
                    "config_change": _config_signal(cfg_row)},
    }
    return finding, diagnosis


CONFIG_WINDOW_FOR_DISMISSAL = 5


def build_findings_and_diagnoses(candidates: Dict[str, List[dict]], sessions, indices,
                                  config_changes: List[dict]) -> Tuple[List[dict], List[dict]]:
    findings, diagnoses = [], []
    builders = [
        ("new_cohort", lambda c: diagnose_new_cohort(c, indices, config_changes)),
        ("silent_tool_failure", lambda c: diagnose_silent_tool_failure(c, indices, config_changes)),
        ("turn_inflation", lambda c: diagnose_turn_inflation(c, indices, config_changes)),
        ("traffic_mix", lambda c: dismiss_traffic_mix(c, config_changes)),
        ("load_event", lambda c: dismiss_load_event(c)),
        ("judge_boundary", lambda c: dismiss_judge_boundary(c, config_changes)),
    ]
    i = 0
    for kind, fn in builders:
        for cand in candidates.get(kind, []):
            built = fn(cand)
            if built is None:
                continue
            i += 1
            finding, diagnosis = built
            finding["id"] = "f%d" % i
            diagnosis["id"] = "d%d" % i
            diagnosis["finding_id"] = finding["id"]
            findings.append(finding)
            diagnoses.append(diagnosis)
    return findings, diagnoses
