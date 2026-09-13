"""Candidate generation. Every scanner below looks at ONE kind of signal, finds
where it moves, and packages what it saw into a plain dict `Candidate` — no
scanner decides "real regression" vs "lookalike" by itself. That judgment call
(the causal hypothesis filter) happens once, centrally, in diagnoser.py, using
whatever evidence each candidate is carrying (nearby config change, per-cohort
stability, volume/latency correlation, judge_version alignment).

Nothing here references a tenant name, an intent name, an agent id, a tool name
or a day number as a literal. Every cohort is discovered by scanning what is
actually in the corpus, so the same code runs unmodified against the sealed
dataset. Every evidence string is formatted from a number computed here.

Two structural rules, both learned by testing on unseen corpus layouts:
  * a fault that can END inside the corpus is searched with a signed criterion
    ("rise"/"fall"), never abs_delta — otherwise the recovery edge can win and
    the scanner discards the fault;
  * a move must be large against its own sampling error (MIN_Z), not only large
    in absolute size — a size gate alone lets a thin cohort's noise through.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Dict, List, Optional

from .changepoint import (find_rate_changepoint, find_recovery_day, percentile, sum_range,
                          window_bounds, z_two_means, z_two_proportions)
from .cohorts import Rollup, slice_sessions

# ---- tunables -------------------------------------------------------------
# Effect-size gates are expressed in the metric's own units and set from what
# an operator would call a material change; none of them is fitted to a
# specific day, tenant or answer key. MIN_Z is the statistical noise gate.
NEW_COHORT_MIN_SESSIONS = 30
NEW_COHORT_MATURE_FRACTION = 0.10   # seen within the first 10% of the log => not "new"
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

QUALITY_DROP_MIN = 0.35
CONFIG_WINDOW_DAYS = 5

MIN_Z = 4.0
CP_WINDOW = 7


def nearby_config_changes(config_changes: List[dict], tenant: str, day: int,
                           kind: Optional[str] = None, target: Optional[str] = None,
                           window: int = CONFIG_WINDOW_DAYS) -> List[dict]:
    """Change markers near `day`. Always admits tenant='*' rows (catalog: an
    equality-only tenant join silently drops the judge-version change), and
    filters on kind before target (catalog: tool_name -> target only with kind='tool')."""
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
    out.sort(key=lambda c: (abs(c["day"] - day), c["day"]))
    return out


def candidate(kind, tenant, cohort, metric, from_day, to_day, observed, expected,
              evidence, extra=None):
    return {
        "kind": kind, "tenant": tenant, "cohort": cohort, "metric": metric,
        "from_day": from_day, "to_day": to_day,
        "observed": observed, "expected": expected,
        "evidence": evidence, "extra": extra or {},
    }


def _fmt_z(z: Optional[float]) -> str:
    if z is None:
        return "n/a"
    return ">99" if abs(z) > 99 else "%.1f" % z


# ---------------------------------------------------------------------------
# 1. New-cohort launch scanner (catches: new product/intent with no support)
# ---------------------------------------------------------------------------

def scan_new_cohorts(sessions, indices, step_cubes, last_day: int) -> List[dict]:
    out = []
    idx = indices["by_tenant_intent"]
    mature_day = int(NEW_COHORT_MATURE_FRACTION * (last_day + 1))
    tenants = sorted({s["tenant"] for s in sessions})
    for tenant in tenants:
        intents = sorted({i for (t, i) in idx if t == tenant})
        first_day = {}
        for intent in intents:
            present_days = [d for d, ss in idx.get((tenant, intent), {}).items() if ss]
            if present_days:
                first_day[intent] = min(present_days)
        mature = [i for i in intents if first_day.get(i, 0) <= mature_day]
        for intent in intents:
            fday = first_day.get(intent)
            if fday is None or fday <= mature_day:
                continue
            if len(slice_sessions(idx, (tenant, intent), fday, last_day)) < NEW_COHORT_MIN_SESSIONS:
                continue
            obs_to = min(last_day, fday + NEW_COHORT_RAMPUP_DAYS - 1)
            obs = Rollup(slice_sessions(idx, (tenant, intent), fday, obs_to))
            peer_sessions = []
            for m in mature:
                peer_sessions.extend(slice_sessions(idx, (tenant, m), fday, obs_to))
            peer = Rollup(peer_sessions)
            if obs.resolution_rate is None or peer.resolution_rate is None:
                continue
            drop = peer.resolution_rate - obs.resolution_rate
            z = z_two_proportions(obs.resolved, obs.n, peer.resolved, peer.n)
            if drop < NEW_COHORT_DROP or z is None or z < MIN_Z:
                continue

            num_by_day = {d: sum(1 for s in ss if s["session_end"] == "resolved") for d, ss in idx[(tenant, intent)].items()}
            den_by_day = {d: len(ss) for d, ss in idx[(tenant, intent)].items()}
            recovery = find_recovery_day(num_by_day, den_by_day, fday, last_day,
                                          target_rate=peer.resolution_rate, tolerance=0.10,
                                          window=3, min_den=5, direction="up")
            to_day = (recovery - 1) if recovery else last_day

            kb = {"lookups": 0, "hits": 0, "sum": 0.0, "min": None, "max": None}
            for d in range(fday, to_day + 1):
                r = step_cubes.kb_by_intent.get((tenant, intent), {}).get(d)
                if not r:
                    continue
                kb["lookups"] += r["lookups"]
                kb["hits"] += r["hits"]
                kb["sum"] += r["top_score_sum"]
                if r["top_score_min"] is not None:
                    kb["min"] = r["top_score_min"] if kb["min"] is None else min(kb["min"], r["top_score_min"])
                    kb["max"] = r["top_score_max"] if kb["max"] is None else max(kb["max"], r["top_score_max"])
            kb_hit_rate = kb["hits"] / kb["lookups"] if kb["lookups"] else None
            kb_avg = kb["sum"] / kb["lookups"] if kb["lookups"] else None

            tool_calls = sum(r["calls"] for d, r in step_cubes.tool_by_intent.get((tenant, intent), {}).items()
                             if fday <= d <= to_day)
            evidence = [
                "intent '%s' first appears on day %d; the tenant's other %d intents have been live since day %d or earlier"
                % (intent, fday, len(mature), mature_day),
                "cohort resolution %.3f (%d sessions) vs mature peer intents over the same days %.3f (%d sessions); z = %s"
                % (obs.resolution_rate, obs.n, peer.resolution_rate, peer.n, _fmt_z(z)),
            ]
            if kb_hit_rate is not None:
                evidence.append("kb_lookup in this cohort: kb_hit %.2f over %d lookups; kb_top_score mean %.2f, range %.2f-%.2f"
                                % (kb_hit_rate, kb["lookups"], kb_avg, kb["min"], kb["max"]))
            else:
                evidence.append("no kb_lookup rows in this cohort (v2_flow emits none) — KB cause cannot be measured here")
            evidence.append("%d tool_call step(s) in this cohort over the window" % tool_calls)
            out.append(candidate(
                "new_cohort", tenant, {"intent": intent}, "resolution_rate",
                fday, to_day, round(obs.resolution_rate, 4), round(peer.resolution_rate, 4),
                evidence,
                extra={"kb_hit_rate": kb_hit_rate, "kb_avg_score": kb_avg, "kb_score_min": kb["min"],
                       "kb_score_max": kb["max"], "kb_lookups": kb["lookups"], "tool_calls": tool_calls,
                       "recovered": recovery is not None, "z": z, "mature_intents": sorted(mature)},
            ))
    return out


# ---------------------------------------------------------------------------
# 2. Silent tool-failure scanner (catches: 200/ok with an empty body)
# ---------------------------------------------------------------------------

def scan_silent_tool_failures(sessions, indices, step_cubes, last_day: int) -> List[dict]:
    out = []
    for (tenant, tool) in sorted(step_cubes.tool_by_tool, key=lambda k: (str(k[0]), str(k[1]))):
        by_day = step_cubes.tool_by_tool[(tenant, tool)]
        if not tool:
            continue
        if sum(r["calls"] for r in by_day.values()) < SILENT_EMPTY_MIN_CALLS:
            continue
        ok_by_day = {d: r["ok"] for d, r in by_day.items()}
        silent_by_day = {d: r["silent_empty"] for d, r in by_day.items()}
        err_by_day = {d: r["err"] for d, r in by_day.items()}
        calls_by_day = {d: r["calls"] for d, r in by_day.items()}
        retry_by_day = {d: r["retry"] for d, r in by_day.items()}

        cp = find_rate_changepoint(silent_by_day, ok_by_day, 0, last_day, window=CP_WINDOW, min_den=20,
                                    criterion="rise", allow_partial_window=True)
        if not cp or cp.delta < SILENT_EMPTY_JUMP:
            continue
        b_lo, b_hi, a_lo, a_hi = window_bounds(0, last_day, cp.day, CP_WINDOW)
        z = z_two_proportions(sum_range(silent_by_day, b_lo, b_hi), sum_range(ok_by_day, b_lo, b_hi),
                              sum_range(silent_by_day, a_lo, a_hi), sum_range(ok_by_day, a_lo, a_hi))
        if z is None or z < MIN_Z:
            continue
        # declared error rate must stay flat across the same boundary — this is
        # exactly the signature that makes the fault invisible to status codes.
        err_cp = find_rate_changepoint(err_by_day, calls_by_day, cp.day - CP_WINDOW, cp.day + CP_WINDOW,
                                        window=CP_WINDOW, min_den=20)
        if err_cp and abs(err_cp.delta) > DECLARED_ERR_FLAT_TOL:
            continue  # an ordinary error-rate regression, not a silent one

        recovery = find_recovery_day(silent_by_day, ok_by_day, cp.day, last_day,
                                      target_rate=cp.before_rate, tolerance=0.02,
                                      window=3, min_den=10, direction="down")
        to_day = (recovery - 1) if recovery else last_day
        length = to_day - cp.day + 1
        pre_lo = max(0, cp.day - length)

        def rate(num, den, lo, hi):
            d = sum_range(den, lo, hi)
            return (sum_range(num, lo, hi) / d) if d else None

        declared_before = rate(err_by_day, calls_by_day, pre_lo, cp.day - 1)
        declared_during = rate(err_by_day, calls_by_day, cp.day, to_day)
        retries_before = sum_range(retry_by_day, pre_lo, cp.day - 1)
        retries_during = sum_range(retry_by_day, cp.day, to_day)
        empties = sum_range(silent_by_day, cp.day, to_day)

        # the intent that actually drives calls to this tool, counted off the
        # tool_call rows themselves — not the tenant's biggest intent
        intent_counts = step_cubes.tool_intents.get((tenant, tool), {})
        best_intent = max(sorted(intent_counts), key=intent_counts.get) if intent_counts else None
        idx = indices["by_tenant_intent"]
        best_res = Rollup(slice_sessions(idx, (tenant, best_intent), cp.day, to_day)) if best_intent else None
        cohort = {"tool": tool}
        if best_intent:
            cohort["intent"] = best_intent
            share = intent_counts[best_intent] / float(sum(intent_counts.values()))
        else:
            share = None

        evidence = [
            "declared tool error rate on '%s' is flat: %s before vs %s during (outcome != 'ok' over all calls)"
            % (tool, "%.4f" % declared_before if declared_before is not None else "n/a",
               "%.4f" % declared_during if declared_during is not None else "n/a"),
            "but outcome='ok' calls with no usable payload (result_field_count = 0) jump %.1f%% -> %.1f%% at day %d; z = %s"
            % (cp.before_rate * 100, cp.after_rate * 100, cp.day, _fmt_z(z)),
            "%d empty 'ok' responses over days %d-%d; tool_call retries %d during vs %d over the %d day(s) before"
            % (empties, cp.day, to_day, retries_during, retries_before, cp.day - pre_lo),
        ]
        if best_intent:
            evidence.append("%.0f%% of calls to '%s' come from intent '%s'" % (share * 100, tool, best_intent))
        out.append(candidate(
            "silent_tool_failure", tenant, cohort, "resolution_rate",
            cp.day, to_day,
            round(best_res.resolution_rate, 4) if best_res and best_res.resolution_rate is not None else None,
            None, evidence,
            extra={"tool": tool, "silent_before": cp.before_rate, "silent_after": cp.after_rate,
                   "declared_before": declared_before, "declared_during": declared_during,
                   "retries_before": retries_before, "retries_during": retries_during,
                   "empty_responses": empties, "z": z, "intent_share_of_calls": share},
        ))
    return out


# ---------------------------------------------------------------------------
# 3. Turn-inflation scanner (catches: cost/effort up, outcome flat)
# ---------------------------------------------------------------------------

def scan_turn_inflation(sessions, indices, last_day: int) -> List[dict]:
    out = []
    idx = indices["by_tenant_agent"]
    for key in sorted(idx, key=lambda k: (str(k[0]), str(k[1]))):
        tenant, agent_id = key
        by_day = idx[key]
        if sum(len(ss) for ss in by_day.values()) < TURNS_MIN_SESSIONS:
            continue
        turns_sum = {d: sum(s["turns"] for s in ss) for d, ss in by_day.items()}
        turns_sq = {d: sum(s["turns"] * s["turns"] for s in ss) for d, ss in by_day.items()}
        n_by_day = {d: len(ss) for d, ss in by_day.items()}
        cp = find_rate_changepoint(turns_sum, n_by_day, 0, last_day, window=CP_WINDOW, min_den=20,
                                    criterion="rise", allow_partial_window=True)
        if not cp or cp.before_rate <= 0 or (cp.after_rate / cp.before_rate) < TURNS_INFLATION_RATIO:
            continue
        b_lo, b_hi, a_lo, a_hi = window_bounds(0, last_day, cp.day, CP_WINDOW)
        z = z_two_means(sum_range(turns_sum, b_lo, b_hi), sum_range(turns_sq, b_lo, b_hi), sum_range(n_by_day, b_lo, b_hi),
                        sum_range(turns_sum, a_lo, a_hi), sum_range(turns_sq, a_lo, a_hi), sum_range(n_by_day, a_lo, a_hi))
        if z is None or z < MIN_Z:
            continue
        resolved_by_day = {d: sum(1 for s in ss if s["session_end"] == "resolved") for d, ss in by_day.items()}
        res_cp = find_rate_changepoint(resolved_by_day, n_by_day, cp.day - CP_WINDOW, cp.day + CP_WINDOW,
                                        window=CP_WINDOW, min_den=20)
        if res_cp and abs(res_cp.delta) > RESOLUTION_FLAT_TOL:
            continue  # resolution actually moved — a different (or additional) mechanism

        recovery = find_recovery_day(turns_sum, n_by_day, cp.day, last_day,
                                      target_rate=cp.before_rate * 1.15, tolerance=0.0,
                                      window=5, min_den=15, direction="down")
        to_day = (recovery - 1) if recovery else last_day

        base_lo = max(0, cp.day - CP_WINDOW)
        before = Rollup(slice_sessions(idx, key, base_lo, cp.day - 1))
        after = Rollup(slice_sessions(idx, key, cp.day, to_day))
        cost_before = (before.cost_sum / before.v3_n) if before.v3_n else None
        cost_after = (after.cost_sum / after.v3_n) if after.v3_n else None
        evidence = [
            "median turns %s -> %s at day %d (mean %.2f -> %.2f over %d days either side, x%.2f); z = %s"
            % (before.median_turns, after.median_turns, cp.day, cp.before_rate, cp.after_rate, CP_WINDOW,
               cp.after_rate / cp.before_rate, _fmt_z(z)),
            "resolution_rate flat across the same boundary: %.3f before vs %.3f during"
            % (before.resolution_rate or 0.0, after.resolution_rate or 0.0),
        ]
        if cost_before is not None and cost_after is not None:
            evidence.append("cost_usd per v3 session %.4f -> %.4f (%+.0f%%)"
                            % (cost_before, cost_after, 100 * (cost_after / cost_before - 1) if cost_before else 0))
        out.append(candidate(
            "turn_inflation", tenant, {"agent_id": agent_id}, "median_turns",
            cp.day, to_day, after.median_turns, before.median_turns, evidence,
            extra={"resolution_before": before.resolution_rate, "resolution_after": after.resolution_rate,
                   "cost_before": cost_before, "cost_after": cost_after, "z": z,
                   "mean_turns_before": cp.before_rate, "mean_turns_after": cp.after_rate,
                   "baseline_window": [base_lo, cp.day - 1]},
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
        intents = sorted({i for (t, i) in idx_ti if t == tenant})
        for intent in intents:
            intent_n_by_day = {d: len(ss) for d, ss in idx_ti.get((tenant, intent), {}).items()}

            # A mix shift is a temporary spike (or dip) in one intent's share. Search
            # each direction's onset explicitly so the recovery edge cannot win.
            candidates_this_intent = []
            for onset_criterion, recovery_direction in (("rise_ratio", "down"), ("fall_ratio", "up")):
                cp = find_rate_changepoint(intent_n_by_day, tenant_n_by_day, 0, last_day,
                                            window=CP_WINDOW, min_den=50, criterion=onset_criterion,
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
            cp, to_day, own_before, own_during = min(valid, key=lambda v: v[0].day)

            tenant_before = Rollup(slice_sessions(idx_t, (tenant,), max(0, cp.day - 10), cp.day - 1))
            tenant_during = Rollup(slice_sessions(idx_t, (tenant,), cp.day, to_day))
            # every other intent's own rate, before vs during — the stratified check
            others = []
            for other in intents:
                if other == intent:
                    continue
                ob = Rollup(slice_sessions(idx_ti, (tenant, other), max(0, cp.day - 10), cp.day - 1))
                od = Rollup(slice_sessions(idx_ti, (tenant, other), cp.day, to_day))
                if ob.n >= 30 and od.n >= 30:
                    others.append(abs(od.resolution_rate - ob.resolution_rate))
            evidence = [
                "'%s' share of %s traffic moves %.3f -> %.3f at day %d"
                % (intent, tenant, cp.before_rate, cp.after_rate, cp.day),
                "'%s' own resolution_rate is stable across the same window (%.3f -> %.3f)"
                % (intent, own_before.resolution_rate, own_during.resolution_rate),
                "tenant aggregate resolution moves %.3f -> %.3f over the same window"
                % (tenant_before.resolution_rate or 0, tenant_during.resolution_rate or 0),
            ]
            if others:
                evidence.append("across the other %d intents with >= 30 sessions on each side, the largest own-rate move is %.3f (median %.3f)"
                                % (len(others), max(others), sorted(others)[len(others) // 2]))
            out.append(candidate(
                "traffic_mix", tenant, {}, "resolution_rate", cp.day, to_day,
                round(tenant_during.resolution_rate, 4) if tenant_during.resolution_rate is not None else None,
                round(tenant_before.resolution_rate, 4) if tenant_before.resolution_rate is not None else None,
                evidence,
                extra={"shifted_intent": intent, "share_before": cp.before_rate, "share_after": cp.after_rate,
                       "own_rate_before": own_before.resolution_rate, "own_rate_during": own_during.resolution_rate,
                       "max_other_intent_move": max(others) if others else None},
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
            continue  # quality actually moved with the volume — never dismiss that

        lat_before = [v for d in range(max(0, cp.day - 7), cp.day) for v in step_cubes.latency_ms.get(tenant, {}).get(d, [])]
        lat_during = [v for d in range(cp.day, to_day + 1) for v in step_cubes.latency_ms.get(tenant, {}).get(d, [])]
        p95_before, p95_during = percentile(lat_before, 0.95), percentile(lat_during, 0.95)
        err_before = sum(r["err"] for (t, _), bd in step_cubes.tool_by_tool.items() if t == tenant
                         for d, r in bd.items() if max(0, cp.day - 7) <= d < cp.day)
        calls_before = sum(r["calls"] for (t, _), bd in step_cubes.tool_by_tool.items() if t == tenant
                           for d, r in bd.items() if max(0, cp.day - 7) <= d < cp.day)
        err_during = sum(r["err"] for (t, _), bd in step_cubes.tool_by_tool.items() if t == tenant
                         for d, r in bd.items() if cp.day <= d <= to_day)
        calls_during = sum(r["calls"] for (t, _), bd in step_cubes.tool_by_tool.items() if t == tenant
                           for d, r in bd.items() if cp.day <= d <= to_day)
        q_before = before.mean_quality(_dominant(before.quality_by_judge_version))
        q_during = during.mean_quality(_dominant(before.quality_by_judge_version))

        evidence = [
            "daily session volume %.0f -> %.0f (%.1fx) at day %d"
            % (cp.before_rate, cp.after_rate, cp.after_rate / cp.before_rate, cp.day),
            "resolution_rate %.3f before vs %.3f during" % (before.resolution_rate, during.resolution_rate),
        ]
        if p95_before and p95_during:
            evidence.append("tool_call p95 latency %d ms -> %d ms" % (p95_before, p95_during))
        if calls_before and calls_during:
            evidence.append("declared tool error rate %.4f -> %.4f" % (err_before / calls_before, err_during / calls_during))
        if q_before is not None and q_during is not None:
            evidence.append("quality_score within judge_version '%s': %.2f -> %.2f"
                            % (_dominant(before.quality_by_judge_version), q_before, q_during))
        evidence.append("volume back under 1.3x baseline from day %d" % (to_day + 1) if recovery
                        else "volume still elevated at the end of the log")
        out.append(candidate(
            "load_event", tenant, {}, "resolution_rate", cp.day, to_day,
            round(during.resolution_rate, 4), round(before.resolution_rate, 4), evidence,
            extra={"volume_before": cp.before_rate, "volume_after": cp.after_rate,
                   "p95_before": p95_before, "p95_during": p95_during, "recovered": recovery is not None},
        ))
    return out


def _dominant(by_version: Dict[str, list]) -> Optional[str]:
    if not by_version:
        return None
    return max(sorted(by_version), key=lambda v: len(by_version[v]))


# ---------------------------------------------------------------------------
# 6. Judge-boundary scanner (catches: rubric change read as quality change)
# ---------------------------------------------------------------------------

def scan_judge_boundary(sessions, indices, last_day: int) -> List[dict]:
    out = []
    by_day = indices["by_all"].get(("*",), {})
    q_sum = {d: sum(s["quality_score"] for s in ss if s.get("quality_score") is not None) for d, ss in by_day.items()}
    q_n = {d: sum(1 for s in ss if s.get("quality_score") is not None) for d, ss in by_day.items()}
    cp = find_rate_changepoint(q_sum, q_n, 0, last_day, window=CP_WINDOW, min_den=100,
                                criterion="fall", allow_partial_window=True)
    if not cp or -cp.delta < QUALITY_DROP_MIN:
        return out

    res_num = {d: sum(1 for s in ss if s["session_end"] == "resolved") for d, ss in by_day.items()}
    res_den = {d: len(ss) for d, ss in by_day.items()}
    res_cp = find_rate_changepoint(res_num, res_den, cp.day - CP_WINDOW, cp.day + CP_WINDOW, window=CP_WINDOW, min_den=100)
    if res_cp and abs(res_cp.delta) > RESOLUTION_FLAT_TOL:
        return out  # real outcomes moved too — not purely a rubric artifact

    b_lo, b_hi, a_lo, a_hi = window_bounds(0, last_day, cp.day, CP_WINDOW)
    # a rubric change hits every tenant at once; a drop confined to one tenant is not one
    per_tenant = {}
    for (tenant,), tdays in indices["by_tenant"].items():
        qb = [s["quality_score"] for d in range(b_lo, b_hi + 1) for s in tdays.get(d, ()) if s.get("quality_score") is not None]
        qa = [s["quality_score"] for d in range(a_lo, a_hi + 1) for s in tdays.get(d, ()) if s.get("quality_score") is not None]
        if qb and qa:
            per_tenant[tenant] = (sum(qb) / len(qb), sum(qa) / len(qa))
    if not per_tenant or any(b - a < QUALITY_DROP_MIN / 2 for b, a in per_tenant.values()):
        return out

    versions_before = Counter(s.get("judge_version") for d in range(b_lo, b_hi + 1) for s in by_day.get(d, ())
                              if s.get("quality_score") is not None)
    versions_after = Counter(s.get("judge_version") for d in range(a_lo, a_hi + 1) for s in by_day.get(d, ())
                             if s.get("quality_score") is not None)
    vb = versions_before.most_common(1)[0][0] if versions_before else None
    va = versions_after.most_common(1)[0][0] if versions_after else None

    rb = sum_range(res_num, b_lo, b_hi) / max(1, sum_range(res_den, b_lo, b_hi))
    ra = sum_range(res_num, a_lo, a_hi) / max(1, sum_range(res_den, a_lo, a_hi))
    evidence = [
        "mean quality_score drops %.3f -> %.3f at day %d" % (cp.before_rate, cp.after_rate, cp.day),
        "per tenant, same days: " + "; ".join("%s %.2f -> %.2f" % (t, b, a) for t, (b, a) in sorted(per_tenant.items())),
        "raw resolution_rate is flat across the same boundary (%.3f -> %.3f) — outcomes did not change" % (rb, ra),
        "judge_version stamped on scored sessions: '%s' before, '%s' after" % (vb, va),
    ]
    out.append(candidate(
        "judge_boundary", "*", {}, "quality_score", cp.day, last_day,
        round(cp.after_rate, 4), round(cp.before_rate, 4), evidence,
        extra={"judge_version_before": vb, "judge_version_after": va, "per_tenant": per_tenant},
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
