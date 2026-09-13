"""Answers to the operator asks that need more than a headline number.

  A02  Are we getting better or worse month over month?   -> per tenant, resolution in the first vs the
       last full 28-day month, raw AND at a fixed intent mix, so a mix shift cannot answer it wrongly.
  A05  Where are people dropping out of a journey?          -> milestone funnels from catalog.milestones,
       read off session.milestones_reached in catalog order. The journey the ask names is matched from
       the words of asks.md at runtime; every journey's funnel is published either way.
  A07  Did the model upgrade help or hurt?                  -> for every kind='model' config row: the
       changed agent's own v3 sessions 7 days before vs 7 days after, standardized to the before-period
       intent mix; quality_score is compared only if both windows share one judge_version.
  A10  Which conversations should a human review this week? -> a deterministic scope (inside a reported
       regression, unplanned handoff, abandoned) over the last 7 days, ranked by the judged
       quality_score's percentile within its own judge_version.

All of it is counted from the corpus. The only judged input is quality_score, which is used within one
judge_version and carries the calibration published on m_quality_score.
"""
from __future__ import annotations

import bisect
import os
import re
from collections import Counter, defaultdict
from typing import Dict, List, Optional

from .changepoint import z_two_proportions
from .standard import session_in_finding

MONTH_DAYS = 28
CHANGE_WINDOW_DAYS = 7
MIN_CELL = 10
REVIEW_WINDOW_DAYS = 7
REVIEW_QUEUE_SIZE = 25
MATERIAL_RATE_DELTA = 0.02
MATERIAL_TURNS_CHANGE = 0.10
SIGNIFICANT_Z = 2.0


def _r(x: Optional[float], nd: int = 4) -> Optional[float]:
    return round(x, nd) if x is not None else None


def _resolution(ss: List[dict]) -> float:
    return sum(1 for s in ss if s["session_end"] == "resolved") / len(ss)


def _mean(ss: List[dict], key: str) -> Optional[float]:
    vals = [s[key] for s in ss if s.get(key) is not None]
    return sum(vals) / len(vals) if vals else None


# ---------------------------------------------------------------------------
# A02
# ---------------------------------------------------------------------------

def mix_adjusted_month_over_month(sessions: List[dict], last_day: int) -> Optional[dict]:
    full_months = list(range((last_day + 1) // MONTH_DAYS))
    if len(full_months) < 2:
        return None
    first, last = full_months[0], full_months[-1]
    per_tenant = {}
    for tenant in sorted({s["tenant"] for s in sessions}):
        cells = defaultdict(lambda: [0, 0])
        for s in sessions:
            m = s["day"] // MONTH_DAYS
            if s["tenant"] != tenant or m not in (first, last):
                continue
            c = cells[(m, s["intent"])]
            c[0] += 1
            c[1] += 1 if s["session_end"] == "resolved" else 0
        n1 = sum(v[0] for (m, _), v in cells.items() if m == first)
        n2 = sum(v[0] for (m, _), v in cells.items() if m == last)
        if not n1 or not n2:
            continue
        raw1 = sum(v[1] for (m, _), v in cells.items() if m == first) / n1
        raw2 = sum(v[1] for (m, _), v in cells.items() if m == last) / n2
        intents = sorted({i for (_, i) in list(cells)})
        common = [i for i in intents if cells.get((first, i), [0])[0] >= MIN_CELL and cells.get((last, i), [0])[0] >= MIN_CELL]
        weight_total = sum(cells[(first, i)][0] for i in common)
        if not weight_total:
            continue
        adj1 = sum(cells[(first, i)][1] for i in common) / weight_total
        adj2 = sum(cells[(first, i)][0] / weight_total * cells[(last, i)][1] / cells[(last, i)][0] for i in common)
        by_intent = []
        for i in common:
            a, b = cells[(first, i)], cells[(last, i)]
            by_intent.append({"intent": i, "first_month": _r(a[1] / a[0]), "last_month": _r(b[1] / b[0]),
                              "delta": _r(b[1] / b[0] - a[1] / a[0]), "z": _r(z_two_proportions(a[1], a[0], b[1], b[0]), 2),
                              "share_first_month": _r(a[0] / n1), "share_last_month": _r(b[0] / n2)})
        worse = [x["intent"] for x in by_intent if x["z"] is not None and x["z"] <= -SIGNIFICANT_Z and x["delta"] <= -MATERIAL_RATE_DELTA]
        better = [x["intent"] for x in by_intent if x["z"] is not None and x["z"] >= SIGNIFICANT_Z and x["delta"] >= MATERIAL_RATE_DELTA]
        per_tenant[tenant] = {
            "raw_first_month": _r(raw1), "raw_last_month": _r(raw2), "raw_change": _r(raw2 - raw1),
            "fixed_mix_first_month": _r(adj1), "fixed_mix_last_month": _r(adj2), "fixed_mix_change": _r(adj2 - adj1),
            "change_explained_by_mix": _r((raw2 - raw1) - (adj2 - adj1)),
            "intents_significantly_worse": worse, "intents_significantly_better": better,
            "reading": "resolution %+.3f raw, %+.3f at the first month's intent mix; %d intent(s) significantly worse, %d better"
                       % (raw2 - raw1, adj2 - adj1, len(worse), len(better)),
            "by_intent": by_intent,
        }
    if not per_tenant:
        return None
    return {
        "id": "m_resolution_month_over_month",
        "name": "Resolution month over month, raw and at a fixed intent mix",
        "ask_id": "A02",
        "grain": "session",
        "fidelity": "measured",
        "coverage": {"value": 1.0, "basis": "session_end is present on every session; months are full 28-day blocks."},
        "calibration": None,
        "plan": {
            "source": "corpus/sessions.jsonl.gz",
            "filter": "session_end = 'resolved'; month = day // %d; first vs last full month" % MONTH_DAYS,
            "denominator": "sessions in the tenant x intent x month cell; fixed-mix rate weights each intent by its "
                           "first-month share (direct standardization), over intents with >= %d sessions in both months" % MIN_CELL,
            "breakdowns": ["tenant", "intent"],
            "alternatives_offered": [
                "raw month-over-month resolution — reported, but not the answer on its own: a campaign that changes "
                "which intents people ask about moves it without any cohort changing",
                "quality_score month over month — refused: the months straddle a judge_version boundary, so the "
                "movement would measure the rubric",
            ],
        },
        "result": {"months_compared": [first, last], "by_tenant": per_tenant},
    }


# ---------------------------------------------------------------------------
# A05
# ---------------------------------------------------------------------------

_STOP = {"where", "what", "which", "when", "people", "users", "customers", "dropping", "drop", "journey",
         "journeys", "from", "with", "that", "this", "have", "does", "into", "during"}


def _norm(word: str) -> str:
    w = word.lower()
    return w[:-1] if len(w) > 4 and w.endswith("s") else w


def ask_text(kit_dir: str, ask_id: str) -> str:
    path = os.path.join(kit_dir, "asks.md")
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    m = re.search(r"##\s*%s\b(.*?)(?=\n##\s|\Z)" % re.escape(ask_id), text, re.S)
    if not m:
        return ""
    return " ".join(line.strip().lstrip(">").strip() for line in m.group(1).splitlines() if line.strip().startswith(">"))


def journey_funnels(sessions: List[dict], catalog: dict, kit_dir: str) -> Optional[dict]:
    journeys = catalog.get("milestones") or {}
    if not journeys:
        return None
    ask = ask_text(kit_dir, "A05")
    tokens = {_norm(t) for t in re.findall(r"[A-Za-z]{4,}", ask) if t.lower() not in _STOP}
    by_cohort = defaultdict(list)
    for s in sessions:
        by_cohort[(s["tenant"], s["intent"])].append(s)

    funnels, asked, total, with_lists = [], [], 0, 0
    for tenant in sorted(journeys):
        for intent, steps in sorted(journeys[tenant].items()):
            cohort = by_cohort.get((tenant, intent), [])
            covered = [s for s in cohort if isinstance(s.get("milestones_reached"), list)]
            total += len(cohort)
            with_lists += len(covered)
            if not covered:
                continue
            stages, prev, prev_name = [], len(covered), "session start"
            worst = None
            for milestone in steps:
                reached = sum(1 for s in covered if milestone in s["milestones_reached"])
                lost = prev - reached
                stage = {"milestone": milestone, "reached": reached, "share_of_started": _r(reached / len(covered)),
                         "lost_since_previous": lost, "lost_share_of_previous": _r(lost / prev) if prev else None}
                stages.append(stage)
                if worst is None or lost > worst["lost"]:
                    worst = {"between": prev_name, "and": milestone, "lost": lost, "lost_share": stage["lost_share_of_previous"]}
                prev, prev_name = reached, milestone
            by_kind = {}
            for kind in sorted({s["agent_kind"] for s in covered}):
                ks = [s for s in covered if s["agent_kind"] == kind]
                by_kind[kind] = {"sessions": len(ks), "completed_share": _r(sum(1 for s in ks if steps and steps[-1] in s["milestones_reached"]) / len(ks))}
            funnel = {"tenant": tenant, "intent": intent, "journey": steps, "sessions": len(covered),
                      "coverage": _r(len(covered) / len(cohort)), "stages": stages, "largest_drop": worst,
                      "completed_share": stages[-1]["share_of_started"] if stages else None, "by_agent_kind": by_kind}
            funnels.append(funnel)
            if tokens & {_norm(p) for p in intent.split("_")}:
                asked.append(funnel)
    if not funnels:
        return None
    return {
        "id": "m_journey_dropoff",
        "name": "Journey drop-off by milestone",
        "ask_id": "A05",
        "grain": "session",
        "fidelity": "measured",
        "coverage": {"value": _r(with_lists / total) if total else 0.0,
                     "basis": "share of sessions on catalogued journeys that carry a milestones_reached list (v2_flow "
                              "and v3_agent alike)."},
        "calibration": None,
        "plan": {
            "source": "corpus/sessions.jsonl.gz (milestones_reached) + catalog.json milestones",
            "filter": "milestone m counted as reached when it appears in session.milestones_reached; stages in catalog order",
            "denominator": "sessions of the journey's intent that carry a milestone list",
            "breakdowns": ["tenant", "intent", "milestone", "agent_kind"],
            "alternatives_offered": [
                "reading transcripts to see where people give up — rejected: whether a milestone was reached is a "
                "logged fact, and transcripts cannot establish it",
            ],
        },
        "result": {
            "asked_journeys": [{"tenant": f["tenant"], "intent": f["intent"], "largest_drop": f["largest_drop"],
                                "completed_share": f["completed_share"]} for f in asked],
            "asked_journey_match": "intents whose name shares a word with the A05 ask text: %s" % (sorted(tokens) or "none found"),
            "funnels": funnels,
        },
    }


# ---------------------------------------------------------------------------
# A07
# ---------------------------------------------------------------------------

def _standardized(before: List[dict], after: List[dict], value_fn):
    b, a = defaultdict(list), defaultdict(list)
    for s in before:
        b[s["intent"]].append(s)
    for s in after:
        a[s["intent"]].append(s)
    common = sorted(i for i in b if len(b[i]) >= MIN_CELL and len(a.get(i, [])) >= MIN_CELL)
    total = sum(len(b[i]) for i in common)
    if not total:
        return None, None, common
    vb = [value_fn(b[i]) for i in common]
    va = [value_fn(a[i]) for i in common]
    if any(v is None for v in vb + va):
        return None, None, common
    wb = sum(len(b[i]) / total * v for i, v in zip(common, vb))
    wa = sum(len(b[i]) / total * v for i, v in zip(common, va))
    return wb, wa, common


def model_change_effects(sessions: List[dict], config_changes: List[dict], findings: List[dict], last_day: int,
                         quality_calibration: Optional[dict]) -> Optional[dict]:
    rows = [c for c in config_changes if c["kind"] == "model"]
    if not rows:
        return None
    by_agent = defaultdict(lambda: defaultdict(list))
    for s in sessions:
        if s["agent_kind"] == "v3_agent":
            by_agent[(s["tenant"], s["agent_id"])][s["day"]].append(s)
    tenants = sorted({s["tenant"] for s in sessions})
    judge_days = sorted({c["day"] for c in config_changes if c["kind"] == "judge"})

    results, model_known, window_total = [], 0, 0
    for c in rows:
        for tenant in (tenants if c["tenant"] == "*" else [c["tenant"]]):
            key = (tenant, c.get("target"))
            day = c["day"]
            lo, hi = max(0, day - CHANGE_WINDOW_DAYS), min(last_day, day + CHANGE_WINDOW_DAYS - 1)
            entry = {"config_change": {k: c.get(k) for k in ("day", "tenant", "kind", "target", "from_value", "to_value", "note")},
                     "tenant": tenant, "agent_id": c.get("target"), "before_days": [lo, day - 1], "after_days": [day, hi]}
            before = [s for d in range(lo, day) for s in by_agent.get(key, {}).get(d, ())]
            after = [s for d in range(day, hi + 1) for s in by_agent.get(key, {}).get(d, ())]
            window_total += len(before) + len(after)
            model_known += sum(1 for s in before + after if s.get("model"))
            if len(before) < 2 * MIN_CELL or len(after) < 2 * MIN_CELL:
                entry.update(verdict="not measurable", reason="fewer than %d v3 sessions on each side of the change" % (2 * MIN_CELL))
                results.append(entry)
                continue

            switched_after = sum(1 for s in after if s.get("model") == c.get("to_value")) / len(after)
            on_old_before = sum(1 for s in before if s.get("model") == c.get("from_value")) / len(before)
            rb, ra, common = _standardized(before, after, _resolution)
            tb, ta, _ = _standardized(before, after, lambda ss: _mean(ss, "turns"))
            cb, ca, _ = _standardized(before, after, lambda ss: _mean(ss, "cost_usd"))
            nb_res = sum(1 for s in before if s["session_end"] == "resolved")
            na_res = sum(1 for s in after if s["session_end"] == "resolved")
            z = z_two_proportions(nb_res, len(before), na_res, len(after))

            confounds = [
                {k: o.get(k) for k in ("day", "tenant", "kind", "target", "note")}
                for o in config_changes
                if o is not c and o["tenant"] in (tenant, "*") and lo <= o["day"] <= hi
                and (o.get("target") == c.get("target") or o["kind"] in ("kb", "judge"))
            ]
            same_agent = [o for o in confounds if o.get("target") == c.get("target")]
            overlapping = [
                {"finding_id": f["id"], "is_regression": f["is_regression"], "cohort": f.get("cohort")}
                for f in findings
                if f["tenant"] in (tenant, "*") and not (f["window"]["to_day"] < lo or f["window"]["from_day"] > hi)
                and (f.get("cohort") or {}).get("agent_id") in (None, c.get("target"))
                and (f["is_regression"] or f["tenant"] != "*")
            ]

            versions_b = {s.get("judge_version") for s in before if s.get("quality_score") is not None}
            versions_a = {s.get("judge_version") for s in after if s.get("quality_score") is not None}
            crossing = [d for d in judge_days if lo < d <= hi]
            if crossing or versions_b != versions_a or len(versions_b) != 1:
                quality = {"compared": False,
                           "reason": "the window crosses a judge_version change%s; quality_score is only comparable "
                                     "within one version (catalog: UNSOUND_ACROSS_JUDGE_VERSIONS)"
                                     % ((" on day %s" % ", ".join(map(str, crossing))) if crossing else "")}
            else:
                qb, qa = _standardized(before, after, lambda ss: _mean(ss, "quality_score"))[:2]
                quality = {"compared": True, "fidelity": "judged", "judge_version": next(iter(versions_b)),
                           "before": _r(qb, 3), "after": _r(qa, 3),
                           "calibration_agreement": (quality_calibration or {}).get("agreement")}

            res_delta = (ra - rb) if rb is not None else None
            turns_change = (ta / tb - 1) if tb else None
            cost_change = (ca / cb - 1) if cb else None
            if same_agent or [f for f in overlapping if f["is_regression"]]:
                verdict = "confounded"
            elif res_delta is not None and z is not None and abs(z) >= SIGNIFICANT_Z and abs(res_delta) >= MATERIAL_RATE_DELTA:
                verdict = "helped" if res_delta > 0 else "hurt"
            elif turns_change is not None and turns_change >= MATERIAL_TURNS_CHANGE:
                verdict = "hurt (effort)"
            elif turns_change is not None and turns_change <= -MATERIAL_TURNS_CHANGE:
                verdict = "helped (effort)"
            else:
                verdict = "no measurable change"
            entry.update({
                "sessions_before": len(before), "sessions_after": len(after),
                "share_on_old_model_before": _r(on_old_before), "share_on_new_model_after": _r(switched_after),
                "intents_compared": common,
                "resolution_fixed_mix": {"before": _r(rb), "after": _r(ra), "delta": _r(res_delta), "z_raw": _r(z, 2)},
                "mean_turns_fixed_mix": {"before": _r(tb, 3), "after": _r(ta, 3), "change": _r(turns_change)},
                "cost_per_session_fixed_mix": {"before": _r(cb, 5), "after": _r(ca, 5), "change": _r(cost_change)},
                "quality": quality, "other_changes_in_window": confounds, "findings_overlapping_window": overlapping,
                "verdict": verdict,
                "reading": "resolution %s (z %s), mean turns %s, cost per session %s%s"
                           % ("%+.3f" % res_delta if res_delta is not None else "n/a", "%.1f" % z if z is not None else "n/a",
                              "%+.0f%%" % (100 * turns_change) if turns_change is not None else "n/a",
                              "%+.0f%%" % (100 * cost_change) if cost_change is not None else "n/a",
                              "; other changes in the window make this confounded" if verdict == "confounded" else ""),
            })
            results.append(entry)
    return {
        "id": "m_model_change_effect",
        "name": "Model upgrade effect on the changed agent",
        "ask_id": "A07",
        "grain": "session",
        "fidelity": "measured",
        "coverage": {"value": _r(model_known / window_total) if window_total else 0.0,
                     "basis": "share of the changed agents' v3 sessions in the before/after windows that record which "
                              "model served them; v2_flow agents have no model changes and are out of scope."},
        "calibration": None,
        "plan": {
            "source": "corpus/config_timeline.csv WHERE kind = 'model' + corpus/sessions.jsonl.gz",
            "filter": "agent_id = config.target AND agent_kind = 'v3_agent'; %d days before vs %d days from the change day"
                      % (CHANGE_WINDOW_DAYS, CHANGE_WINDOW_DAYS),
            "denominator": "the changed agent's sessions in each window, standardized to the before-window intent mix "
                           "(intents with >= %d sessions on both sides)" % MIN_CELL,
            "breakdowns": ["intent"],
            "alternatives_offered": [
                "raw tenant-wide before/after — rejected: other agents and a traffic-mix shift move it",
                "quality_score before/after — only when both windows sit inside one judge_version; otherwise refused",
            ],
        },
        "result": {"changes": results,
                   "verdict_rule": "confounded if another change to the same agent or an overlapping regression sits in "
                                   "the window; else helped/hurt when |resolution delta| >= %.2f with |z| >= %.0f; else "
                                   "effort helped/hurt when mean turns move >= %.0f%%; else no measurable change"
                                   % (MATERIAL_RATE_DELTA, SIGNIFICANT_Z, 100 * MATERIAL_TURNS_CHANGE)},
    }


# ---------------------------------------------------------------------------
# A10
# ---------------------------------------------------------------------------

def review_queue(sessions: List[dict], findings: List[dict], last_day: int, quality_calibration: Optional[dict]) -> Optional[dict]:
    lo = max(0, last_day - REVIEW_WINDOW_DAYS + 1)
    regressions = [f for f in findings if f["is_regression"]]
    scores_by_version = defaultdict(list)
    for s in sessions:
        if s.get("quality_score") is not None:
            scores_by_version[s.get("judge_version")].append(s["quality_score"])
    for v in scores_by_version.values():
        v.sort()

    items, reasons_count, tenants_count = [], Counter(), Counter()
    for s in sessions:
        if not (lo <= s["day"] <= last_day):
            continue
        reasons = []
        in_regs = [f["id"] for f in regressions if session_in_finding(s, f)]
        if in_regs:
            reasons.append("inside regression %s" % ",".join(in_regs))
        if s["session_end"] == "handoff" and not s.get("handoff_by_design"):
            reasons.append("unplanned handoff")
        if s["session_end"] == "abandoned":
            reasons.append("abandoned")
        if not reasons:
            continue
        pct = None
        if s.get("quality_score") is not None:
            scores = scores_by_version[s.get("judge_version")]
            pct = bisect.bisect_left(scores, s["quality_score"]) / len(scores)
        for r in reasons:
            reasons_count[r.split(" f")[0] if r.startswith("inside") else r] += 1
        tenants_count[s["tenant"]] += 1
        items.append({"session_id": s["session_id"], "tenant": s["tenant"], "intent": s["intent"], "day": s["day"],
                      "reasons": reasons, "quality_score": s.get("quality_score"), "judge_version": s.get("judge_version"),
                      "quality_percentile_in_version": _r(pct, 3)})
    if not items:
        return None
    items.sort(key=lambda x: (x["quality_percentile_in_version"] if x["quality_percentile_in_version"] is not None else 2.0,
                              -len(x["reasons"]), x["session_id"]))
    scored = sum(1 for x in items if x["quality_score"] is not None)
    return {
        "id": "m_review_queue",
        "name": "Conversations for human review this week",
        "ask_id": "A10",
        "grain": "session",
        "fidelity": "derived",
        "coverage": {"value": _r(scored / len(items)),
                     "basis": "share of in-scope sessions that carry a quality_score to rank by; unscored ones sort last."},
        "calibration": dict(quality_calibration) if quality_calibration else None,
        "plan": {
            "source": "corpus/sessions.jsonl.gz + this report's regression findings",
            "filter": "day in the last %d days AND (inside a reported regression's window and cohort OR "
                      "session_end = 'handoff' with handoff_by_design not true OR session_end = 'abandoned')" % REVIEW_WINDOW_DAYS,
            "denominator": "sessions in the last %d days" % REVIEW_WINDOW_DAYS,
            "breakdowns": ["tenant", "intent"],
            "alternatives_offered": [
                "rank by raw quality_score — rejected: scores from different judge versions are not comparable, so "
                "the rank uses the percentile within the session's own judge_version",
            ],
        },
        "result": {"window_days": [lo, last_day], "in_scope": len(items), "by_reason": dict(sorted(reasons_count.items())),
                   "by_tenant": dict(sorted(tenants_count.items())),
                   "ranking": "lowest judged quality percentile within judge_version first; ties: more reasons first, then session_id",
                   "queue": items[:REVIEW_QUEUE_SIZE]},
    }


def build_answers(sessions: List[dict], catalog: dict, kit_dir: str, config_changes: List[dict], findings: List[dict],
                  last_day: int, quality_calibration: Optional[dict]) -> List[dict]:
    out = [
        mix_adjusted_month_over_month(sessions, last_day),
        journey_funnels(sessions, catalog, kit_dir),
        model_change_effects(sessions, config_changes, findings, last_day, quality_calibration),
        review_queue(sessions, findings, last_day, quality_calibration),
    ]
    return [m for m in out if m]
