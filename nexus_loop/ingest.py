"""Load the corpus. Sessions are small enough to hold in memory in full; steps
(880k+ rows on the full corpus) are streamed once and reduced into per-cohort,
per-day counters so memory stays bounded regardless of corpus size.

Nothing here imports from tools/nexus-loop-kit/nlkit — that package is the kit's
own generator internals, not a consumer library. The sanctioned interface is the
data files under <kit>/ (sessions.jsonl.gz, agent_steps.jsonl.gz, turns.jsonl.gz,
config_timeline.csv, catalog.json, manifest.json).
"""
from __future__ import annotations

import csv
import gzip
import json
import os
from collections import defaultdict
from typing import Dict, Iterator, List


def stream_jsonl_gz(path: str) -> Iterator[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def load_manifest(kit_dir: str) -> dict:
    with open(os.path.join(kit_dir, "manifest.json")) as f:
        return json.load(f)


def load_catalog(kit_dir: str) -> dict:
    with open(os.path.join(kit_dir, "catalog.json")) as f:
        return json.load(f)


def load_sessions(kit_dir: str, corpus_subdir: str = "corpus") -> List[dict]:
    """Every session, with a derived `week` field. ~80k rows on the full corpus —
    small enough to keep resident; every detector below indexes this list rather
    than re-reading the file."""
    path = os.path.join(kit_dir, corpus_subdir, "sessions.jsonl.gz")
    out = []
    for s in stream_jsonl_gz(path):
        s["week"] = s["day"] // 7
        out.append(s)
    return out


def load_config_timeline(kit_dir: str, corpus_subdir: str = "corpus") -> List[dict]:
    # config_timeline.csv is not session-sampled — corpus_sample/ (a fast-iteration
    # slice of sessions) doesn't carry its own copy, so always read the one under
    # the full corpus/ directory regardless of which session set is in use.
    path = os.path.join(kit_dir, "corpus", "config_timeline.csv")
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            row["day"] = int(row["day"])
            out.append(row)
    out.sort(key=lambda r: r["day"])
    return out


def _new_tool_bucket() -> dict:
    return {"calls": 0, "ok": 0, "err": 0, "silent_empty": 0, "retry": 0, "err_class": defaultdict(int)}


def _new_kb_bucket() -> dict:
    return {"lookups": 0, "hits": 0, "top_score_sum": 0.0, "top_score_min": None, "top_score_max": None}


# An empty JSON body ("{}" or "[]") is 2 bytes. Only consulted when a row does not
# carry result_field_count at all; the catalog names result_field_count = 0 on a
# 200 as the primary "the agent got nothing to answer with" signal.
EMPTY_BODY_BYTES = 2


def is_silent_empty(step: dict) -> bool:
    """outcome='ok' but nothing usable came back (catalog: step.result_field_count,
    step.response_bytes). A missing field is NOT treated as zero."""
    if step.get("outcome") != "ok":
        return False
    fields = step.get("result_field_count")
    if fields is not None:
        return fields == 0
    size = step.get("response_bytes")
    return size is not None and size <= EMPTY_BODY_BYTES


class StepCubes:
    """Aggregates built from ONE streaming pass over agent_steps.jsonl.gz.

    tool_by_tool[(tenant, tool_name)][day]   -> calls/err/silent_empty/retry/err_class
    tool_by_intent[(tenant, intent)][day]    -> same shape, aggregated across tools used by that intent
    kb_by_intent[(tenant, intent)][day]      -> lookups/hits/top_score_sum
    latency_ms[tenant][day]                  -> list of tool_call duration_ms (bounded: ~62k rows total)
    """

    def __init__(self):
        self.tool_by_tool: Dict[tuple, Dict[int, dict]] = defaultdict(lambda: defaultdict(_new_tool_bucket))
        self.tool_by_intent: Dict[tuple, Dict[int, dict]] = defaultdict(lambda: defaultdict(_new_tool_bucket))
        self.kb_by_intent: Dict[tuple, Dict[int, dict]] = defaultdict(lambda: defaultdict(_new_kb_bucket))
        self.latency_ms: Dict[str, Dict[int, list]] = defaultdict(lambda: defaultdict(list))
        self.step_type_counts: Dict[str, int] = defaultdict(int)
        # (tenant, tool_name) -> Counter(intent) — which intent(s) actually drive
        # calls to this tool, so a tool-level fault can be mapped back to the
        # cohort an operator would recognise, without guessing.
        self.tool_intents: Dict[tuple, Dict[str, int]] = defaultdict(lambda: defaultdict(int))


def build_step_cubes(kit_dir: str, corpus_subdir: str = "corpus") -> StepCubes:
    path = os.path.join(kit_dir, corpus_subdir, "agent_steps.jsonl.gz")
    cubes = StepCubes()
    for r in stream_jsonl_gz(path):
        st = r["step_type"]
        cubes.step_type_counts[st] += 1
        tenant, day = r["tenant"], r["day"]

        if st == "tool_call":
            is_ok = r["outcome"] == "ok"
            silent_empty = is_silent_empty(r)
            for bucket_map, key in (
                (cubes.tool_by_tool, (tenant, r.get("tool_name"))),
                (cubes.tool_by_intent, (tenant, r.get("intent"))),
            ):
                b = bucket_map[key][day]
                b["calls"] += 1
                if is_ok:
                    b["ok"] += 1
                else:
                    b["err"] += 1
                    if r.get("error_class"):
                        b["err_class"][r["error_class"]] += 1
                if silent_empty:
                    b["silent_empty"] += 1
                if (r.get("retry_count") or 0) > 0:
                    b["retry"] += 1
            if r.get("intent"):
                cubes.tool_intents[(tenant, r.get("tool_name"))][r["intent"]] += 1
            if r.get("duration_ms") is not None:
                cubes.latency_ms[tenant][day].append(r["duration_ms"])

        elif st == "kb_lookup":
            b = cubes.kb_by_intent[(tenant, r.get("intent"))][day]
            b["lookups"] += 1
            if r.get("kb_hit"):
                b["hits"] += 1
            score = r.get("kb_top_score")
            if score is not None:
                b["top_score_sum"] += score
                b["top_score_min"] = score if b["top_score_min"] is None else min(b["top_score_min"], score)
                b["top_score_max"] = score if b["top_score_max"] is None else max(b["top_score_max"], score)

    return cubes
