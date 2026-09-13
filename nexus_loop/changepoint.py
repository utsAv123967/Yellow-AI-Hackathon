"""Generic rolling-window changepoint detection over daily numerator/denominator
series. Deliberately simple (compare a trailing window to a leading window, slide
across the corpus, keep the day with the biggest sustained jump) rather than a
fancier online algorithm — every real fault and lookalike in this corpus is a
sharp step function once you look at the right signal, so a rolling comparison
finds all of them and stays easy to audit. No tenant/day/intent name is baked in
here; every input is a plain day -> value dict discovered from the data.
"""
from __future__ import annotations

from typing import Dict, NamedTuple, Optional


class Changepoint(NamedTuple):
    day: int            # first day of the "after" window
    before_rate: float
    after_rate: float
    delta: float         # after - before
    before_n: int
    after_n: int


def find_rate_changepoint(num_by_day: Dict[int, float], den_by_day: Dict[int, float],
                           first_day: int, last_day: int, window: int = 7,
                           min_den: int = 20, criterion: str = "abs_delta",
                           allow_partial_window: bool = False) -> Optional[Changepoint]:
    """Slide a `window`-day/`window`-day boundary across [first_day, last_day];
    return the boundary that best matches `criterion`, provided both sides clear
    `min_den` denominator units (avoids chasing noise on thin days).

    allow_partial_window: False (default) requires a FULL `window` days on both
      sides of every candidate boundary, which is exactly what a narrow
      cross-check call relies on to pin a test to one specific day (or the day
      after it) — e.g. diagnoser code that passes [cp.day-7, cp.day+7] to ask
      "is THIS OTHER metric flat across the SAME boundary another scanner
      already found," rather than search a range on its own.

      True adds a FALLBACK, tried only when that full-window search finds
      nothing at all: boundaries near either edge of [first_day, last_day],
      with whichever side has fewer than `window` days available clipped to
      what's actually there (still gated by `min_den`). This exists so a fault
      flush against day 0 or still running at the last day isn't structurally
      undetectable. It is deliberately never allowed to OUTBID a real
      full-window candidate, only to stand in when there isn't one — a thinner
      window is a noisier rate estimate, and letting it compete on equal
      footing measured wrong: tried directly, it let a 4-day tail window at a
      fault's recovery edge (rate estimate from less data, so more extreme by
      chance) out-score the true 7-day-vs-7-day onset signal by a hair, which
      silently swapped a correct finding for a wrong one. Fallback-only avoids
      that: it can only ever fill in an absence, never overrule a real signal.

    criterion:
      "abs_delta" (default) — largest |after - before|. Right for a metric that
        settles at a new level (a permanent regression, a rubric change).
      "rise_ratio" / "fall_ratio" — largest after/before (or before/after) ratio.
        Right for a temporary SPIKE: a spike's onset and its recovery are both
        large by abs_delta (the recovery is just the mirror image), so abs_delta
        can lock onto the wrong edge. Ratio search only rewards the direction
        that matches the shape being hunted for.
    """
    def _scan(d_range, clip):
        best = None
        for d in d_range:
            b_from = max(first_day, d - window) if clip else d - window
            a_to = min(last_day, d + window - 1) if clip else d + window - 1
            b_den = sum(den_by_day.get(x, 0) for x in range(b_from, d))
            a_den = sum(den_by_day.get(x, 0) for x in range(d, a_to + 1))
            if b_den < min_den or a_den < min_den:
                continue
            b_num = sum(num_by_day.get(x, 0) for x in range(b_from, d))
            a_num = sum(num_by_day.get(x, 0) for x in range(d, a_to + 1))
            b_rate = b_num / b_den
            a_rate = a_num / a_den
            delta = a_rate - b_rate
            cur = Changepoint(d, b_rate, a_rate, delta, b_den, a_den)

            if criterion == "rise_ratio":
                if b_rate <= 0:
                    continue
                score = a_rate / b_rate
                if best is None or score > best[0]:
                    best = (score, cur)
            elif criterion == "fall_ratio":
                if a_rate <= 0:
                    continue
                score = b_rate / a_rate
                if best is None or score > best[0]:
                    best = (score, cur)
            else:
                if best is None or abs(delta) > abs(best.delta):
                    best = cur

        if criterion in ("rise_ratio", "fall_ratio"):
            return best[1] if best else None
        return best

    strict = _scan(range(first_day + window, last_day - window + 2), clip=False)
    if strict is not None or not allow_partial_window:
        return strict
    return _scan(range(first_day + 1, last_day + 1), clip=True)


def find_recovery_day(num_by_day: Dict[int, float], den_by_day: Dict[int, float],
                       start_day: int, last_day: int, target_rate: float,
                       tolerance: float, window: int = 3, min_den: int = 5,
                       direction: str = "up") -> Optional[int]:
    """Scan forward from start_day for the first `window`-day block whose rate
    is back within `tolerance` of target_rate (direction='up': rate must rise to
    at least target_rate - tolerance; 'down': fall back to at most target_rate +
    tolerance). Returns None if it never recovers within the data we have —
    which is itself a legitimate finding (still ongoing at the end of the log)."""
    d = start_day
    while d <= last_day:
        den = sum(den_by_day.get(x, 0) for x in range(d, min(d + window, last_day + 1)))
        num = sum(num_by_day.get(x, 0) for x in range(d, min(d + window, last_day + 1)))
        if den >= min_den:
            rate = num / den
            if direction == "up" and rate >= target_rate - tolerance:
                return d
            if direction == "down" and rate <= target_rate + tolerance:
                return d
        d += 1
    return None


def percentile(values, p: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p * (len(s) - 1)))))
    return s[k]
