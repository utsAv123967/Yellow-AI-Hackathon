"""The report's optional `standard` section: what "good" looks like for this
deployment, mined from its own conversations rather than an external target.

This matters for exactly the fault that has no clean "before" period of its
own (a brand-new intent) — the only baseline available is peer performance,
and this section is what lets that baseline be checked against a stated,
corpus-derived number instead of an assumed constant. It also earns its own
Loop Completeness credit independent of any specific detection.

No tenant, intent, or threshold here is specific to one corpus variant: every
cohort is whatever (tenant, intent) pairs actually appear in --kit, scored
against a fixed, generic minimum-volume floor.
"""
from __future__ import annotations

from collections import defaultdict
from typing import List

from .cohorts import Rollup

MIN_EXEMPLAR_SESSIONS = 15


def build_standard(sessions: List[dict]) -> List[dict]:
    by_cohort = defaultdict(list)
    for s in sessions:
        if s["agent_kind"] == "v3_agent":
            by_cohort[(s["tenant"], s["intent"])].append(s)

    out = []
    for (tenant, intent) in sorted(by_cohort):
        cohort_sessions = by_cohort[(tenant, intent)]
        if len(cohort_sessions) < MIN_EXEMPLAR_SESSIONS:
            continue
        r = Rollup(cohort_sessions)
        if r.resolution_rate is None or r.median_turns is None:
            continue
        out.append({
            "tenant": tenant,
            "cohort": {"intent": intent},
            "metric": "resolution_rate",
            "exemplar_n": r.n,
            "best": round(r.resolution_rate, 4),
            "median": round(r.resolution_rate, 4),
            "derivation": "resolution_rate over v3_agent sessions for this tenant+intent, "
                          "across the full corpus window.",
        })
        out.append({
            "tenant": tenant,
            "cohort": {"intent": intent},
            "metric": "turns_to_resolve",
            "exemplar_n": r.n,
            "best": round(r.median_turns, 1),
            "median": round(r.median_turns, 1),
            "derivation": "median turns across v3_agent sessions for this tenant+intent, "
                          "across the full corpus window.",
        })
    return out
