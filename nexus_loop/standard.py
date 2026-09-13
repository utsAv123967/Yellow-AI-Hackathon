"""The report's optional `standard` section: what "good" looks like for this
deployment, mined from its own conversations rather than an external target.

For each (tenant, intent) cohort of v3_agent traffic:
  * sessions inside any reported regression's window and cohort are excluded, so
    a broken stretch never lowers the bar it is judged against;
  * resolution_rate: `best` is the top-decile DAILY rate (the schema's definition
    of the standard), `median` the median daily rate, `deficit` = best - median;
  * turns_to_resolve (resolved sessions only): `best` is the bottom-decile daily
    median (fewer turns is better), `median` the median, `deficit` = median - best.
Only days with enough sessions to be a rate count, so one lucky 3-session day
cannot become the standard.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from typing import List, Optional

from .changepoint import percentile

MIN_DAY_SESSIONS = 15
MIN_DAY_RESOLVED = 10
MIN_QUALIFYING_DAYS = 7
BEST_QUANTILE = 0.90


def session_in_finding(s: dict, f: dict) -> bool:
    if f["tenant"] not in (s["tenant"], "*"):
        return False
    w = f["window"]
    if not (w["from_day"] <= s["day"] <= w["to_day"]):
        return False
    keys = [k for k in (f.get("cohort") or {}) if k in s]
    return all(str(s[k]) == str(f["cohort"][k]) for k in keys)


def build_standard(sessions: List[dict], regressions: List[dict], golden_versions: Optional[dict] = None) -> List[dict]:
    by_cohort = defaultdict(lambda: defaultdict(list))
    excluded = defaultdict(int)
    for s in sessions:
        if s["agent_kind"] != "v3_agent":
            continue
        key = (s["tenant"], s["intent"])
        if any(session_in_finding(s, f) for f in regressions):
            excluded[key] += 1
            continue
        by_cohort[key][s["day"]].append(s)

    out = []
    for key in sorted(by_cohort):
        tenant, intent = key
        days = by_cohort[key]
        rates = [sum(1 for s in ss if s["session_end"] == "resolved") / len(ss) for ss in days.values() if len(ss) >= MIN_DAY_SESSIONS]
        turns = []
        for ss in days.values():
            resolved_turns = [s["turns"] for s in ss if s["session_end"] == "resolved"]
            if len(resolved_turns) >= MIN_DAY_RESOLVED:
                turns.append(statistics.median(resolved_turns))
        n = sum(len(ss) for ss in days.values())
        note = (" %d session(s) inside reported regression windows were excluded." % excluded[key]) if excluded[key] else ""
        common = {"tenant": tenant, "cohort": {"intent": intent}, "exemplar_n": n}
        if (golden_versions or {}).get(tenant):
            common["golden_set_version"] = golden_versions[tenant]
        if len(rates) >= MIN_QUALIFYING_DAYS:
            best, med = percentile(rates, BEST_QUANTILE), statistics.median(rates)
            out.append(dict(common, metric="resolution_rate", best=round(best, 4), median=round(med, 4),
                            deficit=round(best - med, 4),
                            derivation="daily resolution_rate over %d days with >= %d v3_agent sessions; best = %d0th "
                                       "percentile day, median = median day.%s"
                                       % (len(rates), MIN_DAY_SESSIONS, int(BEST_QUANTILE * 10), note)))
        if len(turns) >= MIN_QUALIFYING_DAYS:
            best, med = percentile(turns, 1 - BEST_QUANTILE), statistics.median(turns)
            out.append(dict(common, metric="turns_to_resolve", best=round(best, 1), median=round(med, 1),
                            deficit=round(med - best, 1),
                            derivation="daily median turns of resolved v3_agent sessions over %d days with >= %d "
                                       "resolved; best = %d0th percentile day (fewer is better), median = median day.%s"
                                       % (len(turns), MIN_DAY_RESOLVED, int(round((1 - BEST_QUANTILE) * 10)), note)))
    return out
