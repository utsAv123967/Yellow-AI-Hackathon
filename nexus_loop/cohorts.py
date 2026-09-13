"""Session-side indices and day-range rollups.

Sessions are kept fully in memory (small: ~80k rows). We bucket references to
them by day under several keys (tenant+intent, tenant+agent_id, tenant alone,
day alone) so that computing a metric over an arbitrary [from_day, to_day]
window for an arbitrary cohort is a cheap slice-and-fold, not a re-scan of the
whole corpus.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Callable, Dict, List, Optional


def index_by_day(sessions: List[dict], key_fn: Callable[[dict], Optional[tuple]]) -> Dict[tuple, Dict[int, List[dict]]]:
    """key_fn(session) -> key tuple, or None to exclude. Returns key -> day -> [sessions]."""
    out: Dict[tuple, Dict[int, List[dict]]] = defaultdict(lambda: defaultdict(list))
    for s in sessions:
        k = key_fn(s)
        if k is None:
            continue
        out[k][s["day"]].append(s)
    return out


def build_indices(sessions: List[dict]) -> dict:
    return {
        "by_tenant_intent": index_by_day(sessions, lambda s: (s["tenant"], s["intent"])),
        "by_tenant_agent": index_by_day(sessions, lambda s: (s["tenant"], s["agent_id"])),
        "by_tenant": index_by_day(sessions, lambda s: (s["tenant"],)),
        "by_all": index_by_day(sessions, lambda s: ("*",)),
    }


def slice_sessions(index: Dict[tuple, Dict[int, List[dict]]], key: tuple, from_day: int, to_day: int) -> List[dict]:
    by_day = index.get(key, {})
    out = []
    for d in range(from_day, to_day + 1):
        out.extend(by_day.get(d, ()))
    return out


class Rollup:
    """Aggregate stats over a list of sessions. All fields are auditable back to
    the raw session dicts that produced them."""

    def __init__(self, sessions: List[dict]):
        self.n = len(sessions)
        self.resolved = sum(1 for s in sessions if s["session_end"] == "resolved")
        self.handoff = sum(1 for s in sessions if s["session_end"] == "handoff")
        self.handoff_by_design = sum(1 for s in sessions if s["session_end"] == "handoff" and s.get("handoff_by_design"))
        self.handoff_not_by_design = self.handoff - self.handoff_by_design
        self.abandoned = sum(1 for s in sessions if s["session_end"] == "abandoned")
        self.v3 = [s for s in sessions if s["agent_kind"] == "v3_agent"]
        self.v3_n = len(self.v3)
        self.turns_hist = Counter(s["turns"] for s in sessions)
        self.cost_sum = sum(s.get("cost_usd") or 0.0 for s in self.v3)
        by_jv: Dict[str, list] = defaultdict(list)
        for s in sessions:
            if s.get("quality_score") is not None:
                by_jv[s.get("judge_version")].append(s["quality_score"])
        self.quality_by_judge_version = by_jv
        self.csat = [s["csat"] for s in sessions if s.get("csat") is not None]

    @property
    def resolution_rate(self) -> Optional[float]:
        return self.resolved / self.n if self.n else None

    @property
    def containment_rate(self) -> Optional[float]:
        return (self.resolved + self.handoff_by_design) / self.n if self.n else None

    @property
    def mean_turns(self) -> Optional[float]:
        tot = sum(k * v for k, v in self.turns_hist.items())
        cnt = sum(self.turns_hist.values())
        return tot / cnt if cnt else None

    @property
    def median_turns(self) -> Optional[float]:
        cnt = sum(self.turns_hist.values())
        if not cnt:
            return None
        vals = sorted(self.turns_hist.elements())
        mid = cnt // 2
        if cnt % 2:
            return float(vals[mid])
        return (vals[mid - 1] + vals[mid]) / 2.0

    def mean_quality(self, judge_version: Optional[str] = None) -> Optional[float]:
        if judge_version is not None:
            vals = self.quality_by_judge_version.get(judge_version, [])
        else:
            vals = [v for vs in self.quality_by_judge_version.values() for v in vs]
        return sum(vals) / len(vals) if vals else None


def daily_series(index: Dict[tuple, Dict[int, List[dict]]], key: tuple, days: range,
                  value_fn: Callable[[List[dict]], Optional[float]]) -> Dict[int, Optional[float]]:
    by_day = index.get(key, {})
    return {d: value_fn(by_day.get(d, [])) for d in days}
