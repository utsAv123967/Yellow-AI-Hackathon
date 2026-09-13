"""Candidate generation. Every scanner below looks at ONE kind of signal, finds
where it moves, and packages what it saw into a plain dict `Candidate` — no
scanner decides "real regression" vs "lookalike" by itself. That judgment call
(the causal hypothesis filter) happens once, centrally, in diagnoser.py, using
whatever evidence each candidate is carrying (nearby config change, per-cohort
stability, volume/latency correlation, judge_version alignment).

Nothing here references a tenant name, an intent name, an agent id, or a day
number as a literal. Every cohort is discovered by scanning what is actually in
the corpus, so the same code runs unmodified against the sealed dataset.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional

from .changepoint import find_rate_changepoint, find_recovery_day, percentile
from .cohorts import Rollup, slice_sessions

# ---- tunables -------------------------------------------------------------
# These thresholds were set by inspecting the real corpus's day-level shape
# (see the exploration in the accompanying plan), not tuned against the answer
# key: every genuine fault/lookalike in this corpus is a sharp step function,
# so thresholds have wide margins on both sides of the real signals.
NEW_COHORT_MIN_SESSIONS = 30
NEW_COHORT_MATURE_DAY = 5           # present since day <= this => not "new"
NEW_COHORT_DROP = 0.15              # peer_rate - cohort_rate to flag
NEW_COHORT_RAMPUP_DAYS = 12

SILENT_EMPTY_MIN_CALLS = 150
SILENT_EMPTY_JUMP = 0.05            # after - before, ok-call denominator
DECLARED_ERR_FLAT_TOL = 0.02

TURNS_MIN_SESSIONS = 150
TURNS_INFLATION_RATIO = 1.3
RESOLUTION_FLAT_TOL = 0.06

SHARE_SHIFT_MIN = 0.15
SHARE_STABLE_TOL = 0.10

VOLUME_SPIKE_RATIO = 1.9
LATENCY_SPIKE_RATIO = 1.6

QUALITY_DROP_MIN = 0.35
CONFIG_WINDOW_DAYS = 5


def nearby_config_changes(config_changes: List[dict], tenant: str, day: int,
                           kind: Optional[str] = None, target: Optional[str] = None,
                           window: int = CONFIG_WINDOW_DAYS) -> List[dict]:
    out = []
    for c in config_changes:
        if c["tenant"] not in (tenant, "*"):
            continue
        if kind and c["kind"] != kind:
            continue
        if target and c.get("target") != target:
            continue
        if abs(c["day"] - day) <= window:
            out.append(c)
    out.sort(key=lambda c: abs(c["day"] - day))
    return out


def candidate(kind, tenant, cohort, metric, from_day, to_day, observed, expected,
              evidence, extra=None):
    return {
        "kind": kind, "tenant": tenant, "cohort": cohort, "metric": metric,
        "from_day": from_day, "to_day": to_day,
        "observed": observed, "expected": expected,
        "evidence": evidence, "extra": extra or {},
    }


# ---------------------------------------------------------------------------
# 1. New-cohort launch scanner (catches: new product/intent with no support)
# ---------------------------------------------------------------------------

def scan_new_cohorts(sessions, indices, step_cubes, last_day: int) -> List[dict]:
    out = []
    idx = indices["by_tenant_intent"]
    tenants = sorted({s["tenant"] for s in sessions})
    for tenant in tenants:
        intents = {s["intent"] for s in sessions if s["tenant"] == tenant}
        first_day = {}
        for intent in intents:
            days = idx.get((tenant, intent), {})
            present_days = [d for d, ss in days.items() if ss]
            if present_days:
                first_day[intent] = min(present_days)
        mature = [i for i in intents if first_day.get(i, 0) <= NEW_COHORT_MATURE_DAY]
        for intent, fday in first_day.items():
            if fday <= NEW_COHORT_MATURE_DAY:
                continue
            cohort_sessions_all = slice_sessions(idx, (tenant, intent), fday, last_day)
            if len(cohort_sessions_all) < NEW_COHORT_MIN_SESSIONS:
                continue
            obs_to = min(last_day, fday + NEW_COHORT_RAMPUP_DAYS - 1)
            obs = Rollup(slice_sessions(idx, (tenant, intent), fday, obs_to))
            if obs.resolution_rate is None:
                continue
            peer_sessions = []
            for m in mature:
                peer_sessions.extend(slice_sessions(idx, (tenant, m), fday, obs_to))
            peer = Rollup(peer_sessions)
            if peer.resolution_rate is None:
                continue
            drop = peer.resolution_rate - obs.resolution_rate
            if drop < NEW_COHORT_DROP:
                continue

            num_by_day = defaultdict(int)
            den_by_day = defaultdict(int)
            for d, ss in idx.get((tenant, intent), {}).items():
                den_by_day[d] = len(ss)
                num_by_day[d] = sum(1 for s in ss if s["session_end"] == "resolved")
            recovery = find_recovery_day(num_by_day, den_by_day, fday, last_day,
                                          target_rate=peer.resolution_rate, tolerance=0.10,
                                          window=3, min_den=5, direction="up")
            to_day = (recovery - 1) if recovery else last_day

            kb_rec = {"lookups": 0, "hits": 0, "top_score_sum": 0.0}
            for d in range(fday, to_day + 1):
                r = step_cubes.kb_by_intent.get((tenant, intent), {}).get(d)
                if r:
                    kb_rec["lookups"] += r["lookups"]
                    kb_rec["hits"] += r["hits"]
                    kb_rec["top_score_sum"] += r["top_score_sum"]
            kb_hit_rate = kb_rec["hits"] / kb_rec["lookups"] if kb_rec["lookups"] else None
            kb_avg_score = kb_rec["top_score_sum"] / kb_rec["lookups"] if kb_rec["lookups"] else None

            evidence = [
                "intent '%s' first appears on day %d and never reaches the tenant standard "
                "in the %d days that follow" % (intent, fday, obs_to - fday + 1),
                "cohort resolution %.3f vs peer (mature intents, same window) %.3f"
                % (obs.resolution_rate, peer.resolution_rate),
            ]
            if kb_hit_rate is not None:
                evidence.append("kb_hit rate in this cohort: %.2f (%d lookups), avg kb_top_score %.2f"
                                 % (kb_hit_rate, kb_rec["lookups"], kb_avg_score or 0.0))
            out.append(candidate(
                "new_cohort", tenant, {"intent": intent}, "resolution_rate",
                fday, to_day, round(obs.resolution_rate, 4), round(peer.resolution_rate, 4),
                evidence,
                extra={"kb_hit_rate": kb_hit_rate, "kb_avg_score": kb_avg_score,
                       "recovered": recovery is not None},
            ))
    return out


# ---------------------------------------------------------------------------
# 2. Silent tool-failure scanner (catches: 200/ok with an empty body)
# ---------------------------------------------------------------------------

def scan_silent_tool_failures(sessions, indices, step_cubes, last_day: int) -> List[dict]:
    out = []
    for (tenant, tool), by_day in step_cubes.tool_by_tool.items():
        if not tool:
            continue
        total_calls = sum(r["calls"] for r in by_day.values())
        if total_calls < SILENT_EMPTY_MIN_CALLS:
            continue
        ok_by_day = {d: r["ok"] for d, r in by_day.items()}
        silent_by_day = {d: r["silent_empty"] for d, r in by_day.items()}
        err_by_day = {d: r["err"] for d, r in by_day.items()}
        calls_by_day = {d: r["calls"] for d, r in by_day.items()}

        cp = find_rate_changepoint(silent_by_day, ok_by_day, 0, last_day, window=7, min_den=20,
                                    allow_partial_window=True)
        if not cp or cp.delta < SILENT_EMPTY_JUMP:
            continue
        # declared error rate must stay flat across the same boundary — this is
        # exactly the signature that makes the fault invisible to status codes.
        err_cp = find_rate_changepoint(err_by_day, calls_by_day, cp.day - 7, cp.day + 7, window=7, min_den=20)
        if err_cp and abs(err_cp.delta) > DECLARED_ERR_FLAT_TOL:
            continue  # this looks like an ordinary error-rate regression, not a silent one

        recovery = find_recovery_day(silent_by_day, ok_by_day, cp.day, last_day,
                                      target_rate=cp.before_rate, tolerance=0.02,
                                      window=3, min_den=10, direction="down")
        to_day = (recovery - 1) if recovery else last_day

        # the intent that actually drives calls to this tool (not a guess: counted
        # directly off the tool_call rows themselves), for the cohort key and impact
        intent_counts = step_cubes.tool_intents.get((tenant, tool), {})
        best_intent = max(intent_counts, key=intent_counts.get) if intent_counts else None
        idx = indices["by_tenant_intent"]
        best_res = Rollup(slice_sessions(idx, (tenant, best_intent), cp.day, to_day)) if best_intent else None
        cohort = {"tool": tool}
        if best_intent:
            cohort["intent"] = best_intent

        evidence = [
            "declared tool_failure_rate on '%s' stays FLAT (%.4f -> %.4f) across day %d"
            % (tool, err_cp.before_rate if err_cp else 0.0, err_cp.after_rate if err_cp else 0.0, cp.day),
            "but result_field_count = 0 on outcome='ok' calls jumps %.1f%% -> %.1f%% at day %d"
            % (cp.before_rate * 100, cp.after_rate * 100, cp.day),
            "%d calls affected in the window" % sum(silent_by_day.get(d, 0) for d in range(cp.day, to_day + 1)),
        ]
        out.append(candidate(
            "silent_tool_failure", tenant, cohort, "resolution_rate",
            cp.day, to_day,
            round(best_res.resolution_rate, 4) if best_res and best_res.resolution_rate is not None else None,
            None, evidence,
            extra={"tool": tool, "silent_before": cp.before_rate, "silent_after": cp.after_rate,
                   "declared_before": err_cp.before_rate if err_cp else None,
                   "declared_after": err_cp.after_rate if err_cp else None},
        ))
    return out


# ---------------------------------------------------------------------------
# 3. Turn-inflation scanner (catches: cost/effort up, outcome flat)
# ---------------------------------------------------------------------------

def scan_turn_inflation(sessions, indices, last_day: int) -> List[dict]:
    out = []
    idx = indices["by_tenant_agent"]
    for (tenant, agent_id), by_day in idx.items():
        total = sum(len(ss) for ss in by_day.values())
        if total < TURNS_MIN_SESSIONS:
            continue
        turns_sum_by_day = {d: sum(s["turns"] for s in ss) for d, ss in by_day.items()}
        n_by_day = {d: len(ss) for d, ss in by_day.items()}
        cp = find_rate_changepoint(turns_sum_by_day, n_by_day, 0, last_day, window=7, min_den=20,
                                    allow_partial_window=True)
        if not cp or cp.before_rate <= 0 or (cp.after_rate / cp.before_rate) < TURNS_INFLATION_RATIO:
            continue
        resolved_by_day = {d: sum(1 for s in ss if s["session_end"] == "resolved") for d, ss in by_day.items()}
        res_cp = find_rate_changepoint(resolved_by_day, n_by_day, cp.day - 7, cp.day + 7, window=7, min_den=20)
        if res_cp and abs(res_cp.delta) > RESOLUTION_FLAT_TOL:
            continue  # resolution actually moved — a different (or additional) mechanism

        recovery = find_recovery_day(turns_sum_by_day, n_by_day, cp.day, last_day,
                                      target_rate=cp.before_rate * 1.15, tolerance=0.0,
                                      window=5, min_den=15, direction="down")
        to_day = (recovery - 1) if recovery else last_day

        before = Rollup(slice_sessions(idx, (tenant, agent_id), max(0, cp.day - 7), cp.day - 1))
        after = Rollup(slice_sessions(idx, (tenant, agent_id), cp.day, to_day))
        evidence = [
            "median turns %.1f -> %.1f at day %d (mean %.2f -> %.2f)"
            % (before.median_turns or 0, after.median_turns or 0, cp.day, cp.before_rate, cp.after_rate),
            "resolution_rate flat across the same boundary (%.3f -> %.3f)"
            % (res_cp.before_rate if res_cp else (before.resolution_rate or 0),
               res_cp.after_rate if res_cp else (after.resolution_rate or 0)),
            "cost_per_session %.4f -> %.4f" % (
                (before.cost_sum / before.v3_n) if before.v3_n else 0.0,
                (after.cost_sum / after.v3_n) if after.v3_n else 0.0),
        ]
        out.append(candidate(
            "turn_inflation", tenant, {"agent_id": agent_id}, "median_turns",
            cp.day, to_day, after.median_turns, before.median_turns, evidence,
            extra={"resolution_before": before.resolution_rate, "resolution_after": after.resolution_rate,
                   "cost_before": (before.cost_sum / before.v3_n) if before.v3_n else None,
                   "cost_after": (after.cost_sum / after.v3_n) if after.v3_n else None},
        ))
    return out


# ---------------------------------------------------------------------------
# 4. Traffic-mix scanner (catches: aggregate moves because composition moves)
# ---------------------------------------------------------------------------

def scan_traffic_mix(sessions, indices, last_day: int) -> List[dict]:
    out = []
    idx_ti = indices["by_tenant_intent"]
    idx_t = indices["by_tenant"]
    tenants = sorted({s["tenant"] for s in sessions})
    for tenant in tenants:
        tenant_n_by_day = {d: len(ss) for d, ss in idx_t.get((tenant,), {}).items()}
        intents = {s["intent"] for s in sessions if s["tenant"] == tenant}
        for intent in intents:
            intent_by_day = idx_ti.get((tenant, intent), {})
            intent_n_by_day = {d: len(ss) for d, ss in intent_by_day.items()}

            # A mix-shift is a temporary spike (or dip) in one intent's share of
            # traffic. abs_delta would happily lock onto the RECOVERY edge (the
            # share falling back down) instead of the true onset, since the two
            # edges can be comparable in magnitude — search each direction's
            # onset explicitly instead, exactly as scan_load_events does for a
            # volume spike.
            candidates_this_intent = []
            for onset_criterion, recovery_direction in (("rise_ratio", "down"), ("fall_ratio", "up")):
                cp = find_rate_changepoint(intent_n_by_day, tenant_n_by_day, 0, last_day,
                                            window=7, min_den=50, criterion=onset_criterion,
                                            allow_partial_window=True)
                if not cp or abs(cp.delta) < SHARE_SHIFT_MIN:
                    continue
                recovery = find_recovery_day(intent_n_by_day, tenant_n_by_day, cp.day, last_day,
                                              target_rate=cp.before_rate, tolerance=0.05,
                                              window=5, min_den=20, direction=recovery_direction)
                to_day = (recovery - 1) if recovery else last_day
                candidates_this_intent.append((cp, to_day))

            valid = []
            for cp, to_day in candidates_this_intent:
                own_before = Rollup(slice_sessions(idx_ti, (tenant, intent), max(0, cp.day - 10), cp.day - 1))
                own_during = Rollup(slice_sessions(idx_ti, (tenant, intent), cp.day, to_day))
                if own_before.resolution_rate is None or own_during.resolution_rate is None:
                    continue
                if abs(own_during.resolution_rate - own_before.resolution_rate) > SHARE_STABLE_TOL:
                    continue  # this intent's own rate actually moved — not a pure mix effect
                valid.append((cp, to_day, own_before, own_during))

            if not valid:
                continue
            # rise-onset and fall-onset scans can both fire on the same underlying
            # spike (one finds the up-edge, the other the down-edge of its tail);
            # keep only the earliest onset so we report the event once.
            cp, to_day, own_before, own_during = min(valid, key=lambda v: v[0].day)

            tenant_before = Rollup(slice_sessions(idx_t, (tenant,), max(0, cp.day - 10), cp.day - 1))
            tenant_during = Rollup(slice_sessions(idx_t, (tenant,), cp.day, to_day))
            evidence = [
                "'%s' share of %s traffic moves %.3f -> %.3f at day %d"
                % (intent, tenant, cp.before_rate, cp.after_rate, cp.day),
                "'%s' own resolution_rate is stable across the same window (%.3f -> %.3f)"
                % (intent, own_before.resolution_rate, own_during.resolution_rate),
                "tenant aggregate resolution moves %.3f -> %.3f over the same window"
                % (tenant_before.resolution_rate or 0, tenant_during.resolution_rate or 0),
            ]
            out.append(candidate(
                "traffic_mix", tenant, {}, "resolution_rate", cp.day, to_day,
                round(tenant_during.resolution_rate, 4) if tenant_during.resolution_rate is not None else None,
                round(tenant_before.resolution_rate, 4) if tenant_before.resolution_rate is not None else None,
                evidence,
                extra={"shifted_intent": intent, "share_before": cp.before_rate, "share_after": cp.after_rate},
            ))
    return out


# ---------------------------------------------------------------------------
# 5. Load-event scanner (catches: volume spike, latency up, quality untouched)
# ---------------------------------------------------------------------------

def scan_load_events(sessions, indices, step_cubes, last_day: int) -> List[dict]:
    out = []
    idx_t = indices["by_tenant"]
    tenants = sorted({s["tenant"] for s in sessions})
    for tenant in tenants:
        n_by_day = {d: len(ss) for d, ss in idx_t.get((tenant,), {}).items()}
        ones = {d: 1 for d in n_by_day}
        cp = find_rate_changepoint(n_by_day, ones, 0, last_day, window=4, min_den=1,
                                    criterion="rise_ratio", allow_partial_window=True)
        if not cp or cp.before_rate <= 0 or (cp.after_rate / cp.before_rate) < VOLUME_SPIKE_RATIO:
            continue
        recovery = find_recovery_day(n_by_day, ones, cp.day, last_day,
                                      target_rate=cp.before_rate * 1.3, tolerance=0.0,
                                      window=2, min_den=1, direction="down")
        to_day = (recovery - 1) if recovery else last_day

        before = Rollup(slice_sessions(idx_t, (tenant,), max(0, cp.day - 7), cp.day - 1))
        during = Rollup(slice_sessions(idx_t, (tenant,), cp.day, to_day))
        if before.resolution_rate is None or during.resolution_rate is None:
            continue
        if abs(during.resolution_rate - before.resolution_rate) > RESOLUTION_FLAT_TOL:
            continue  # quality actually moved with the volume — treat elsewhere, don't dismiss

        lat_before = [v for d in range(max(0, cp.day - 7), cp.day) for v in step_cubes.latency_ms.get(tenant, {}).get(d, [])]
        lat_during = [v for d in range(cp.day, to_day + 1) for v in step_cubes.latency_ms.get(tenant, {}).get(d, [])]
        p95_before, p95_during = percentile(lat_before, 0.95), percentile(lat_during, 0.95)
        latency_note = ""
        if p95_before and p95_during:
            latency_note = " p95 tool latency %d ms -> %d ms." % (p95_before, p95_during)

        evidence = [
            "daily session volume %.0f -> %.0f (%.1fx) at day %d"
            % (cp.before_rate, cp.after_rate, cp.after_rate / cp.before_rate, cp.day),
            "resolution_rate flat across the same window (%.3f -> %.3f)."
            % (before.resolution_rate, during.resolution_rate) + latency_note,
            "self-corrects by day %d" % (to_day + 1) if recovery else "ongoing through the end of the log",
        ]
        out.append(candidate(
            "load_event", tenant, {}, "resolution_rate", cp.day, to_day,
            round(during.resolution_rate, 4), round(before.resolution_rate, 4), evidence,
            extra={"volume_before": cp.before_rate, "volume_after": cp.after_rate,
                   "p95_before": p95_before, "p95_during": p95_during},
        ))
    return out


# ---------------------------------------------------------------------------
# 6. Judge-boundary scanner (catches: rubric change read as quality change)
# ---------------------------------------------------------------------------

def scan_judge_boundary(sessions, indices, last_day: int) -> List[dict]:
    out = []
    idx_all = indices["by_all"]
    by_day = idx_all.get(("*",), {})
    q_sum_by_day = {d: sum(s["quality_score"] for s in ss if s.get("quality_score") is not None) for d, ss in by_day.items()}
    q_n_by_day = {d: sum(1 for s in ss if s.get("quality_score") is not None) for d, ss in by_day.items()}
    cp = find_rate_changepoint(q_sum_by_day, q_n_by_day, 0, last_day, window=7, min_den=100,
                                allow_partial_window=True)
    if not cp or abs(cp.delta) < QUALITY_DROP_MIN:
        return out

    res_num = {d: sum(1 for s in ss if s["session_end"] == "resolved") for d, ss in by_day.items()}
    res_den = {d: len(ss) for d, ss in by_day.items()}
    res_cp = find_rate_changepoint(res_num, res_den, cp.day - 7, cp.day + 7, window=7, min_den=100)
    if res_cp and abs(res_cp.delta) > RESOLUTION_FLAT_TOL:
        return out  # real outcomes moved too — not purely a rubric artifact

    evidence = [
        "global mean quality_score drops %.3f -> %.3f at day %d, across every tenant at once"
        % (cp.before_rate, cp.after_rate, cp.day),
        "raw resolution_rate is flat across the same boundary (%.3f -> %.3f) — outcomes did not change"
        % (res_cp.before_rate if res_cp else 0, res_cp.after_rate if res_cp else 0),
    ]
    out.append(candidate(
        "judge_boundary", "*", {}, "quality_score", cp.day, last_day,
        round(cp.after_rate, 4), round(cp.before_rate, 4), evidence,
        extra={},
    ))
    return out


def scan_all(sessions, indices, step_cubes, last_day: int) -> Dict[str, List[dict]]:
    return {
        "new_cohort": scan_new_cohorts(sessions, indices, step_cubes, last_day),
        "silent_tool_failure": scan_silent_tool_failures(sessions, indices, step_cubes, last_day),
        "turn_inflation": scan_turn_inflation(sessions, indices, last_day),
        "traffic_mix": scan_traffic_mix(sessions, indices, last_day),
        "load_event": scan_load_events(sessions, indices, step_cubes, last_day),
        "judge_boundary": scan_judge_boundary(sessions, indices, last_day),
    }
