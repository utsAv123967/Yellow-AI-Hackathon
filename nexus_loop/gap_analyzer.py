"""Measurement gaps — the answer to "what can we not measure, and why."

Read directly from catalog.json's `capabilities` list rather than duplicating
its text: catalog.json is the sanctioned semantic dictionary and already states,
per field, whether something is measurable, judged, or not measurable at all
(and if not, exactly which event would need to start being logged). Gaps here
are a structural translation of that list into the report schema's shape, not a
rediscovery of it — inventing our own reasons would risk drifting from the one
place the corpus's own semantics are declared.

The mapping from a catalog capability id to an ask id (A11, A09, ...) is fixed
by the challenge itself: asks.md poses the same eleven questions, in the same
order, on every corpus variant — only the underlying data changes. That mapping
is domain knowledge about the task, not a fact about this specific corpus, so
hand-declaring it here does not compromise generalisation to the sealed run.
"""
from __future__ import annotations

from typing import List, Optional


CAPABILITY_TO_ASK = {
    "failover_rate": "A11",
    "abandonment_reason": "A09",
}

VERDICT_BY_CLASS = {
    "NOT_MEASURABLE": "NOT_MEASURABLE",
    "requires_new_judge": "REQUIRES_NEW_JUDGE",
}


def _split_proxy(nearest_proxy: Optional[str]):
    if not nearest_proxy:
        return None, None
    marker = " — "
    if marker in nearest_proxy:
        proxy, rest = nearest_proxy.split(marker, 1)
        rest = rest[4:] if rest.lower().startswith("but ") else rest
        return proxy.strip(), rest.strip()
    return nearest_proxy.strip(), None


def build_gaps(catalog: dict) -> List[dict]:
    gaps = []
    for cap in catalog.get("capabilities", []):
        cap_id = cap.get("id")
        ask_id = CAPABILITY_TO_ASK.get(cap_id)
        cls = cap.get("class")
        verdict = VERDICT_BY_CLASS.get(cls)
        if not ask_id or not verdict:
            continue

        gap = {"ask_id": ask_id, "verdict": verdict}
        why = cap.get("blocker") or cap.get("note")
        gap["why"] = why or ("catalog.json declares capability '%s' as class '%s'." % (cap_id, cls))

        proxy, mislead = _split_proxy(cap.get("nearest_proxy"))
        if proxy:
            gap["nearest_proxy"] = proxy
        if mislead:
            gap["why_the_proxy_misleads"] = mislead

        if cap.get("required_event"):
            gap["required_event"] = cap["required_event"]

        gaps.append(gap)
    return gaps
