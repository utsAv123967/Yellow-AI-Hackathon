#!/usr/bin/env python3
"""One command, end to end: Detect -> Diagnose -> Prescribe (-> record Verify/Decide).

    python -m nexus_loop.pipeline --kit kit --out my-loop-report.json
    python -m nexus_loop.pipeline --kit <sealed-kit> --corpus-subdir corpus --out loop-report.json
    python -m nexus_loop.pipeline --kit kit --decisions decisions.json --verifications replays.json

No tenant, intent, agent id, tool or day number is hardcoded anywhere in this
package — every cohort is discovered from whatever is in --kit. No language model
and no network call is made. The replay service is never contacted; prescriptions
carry a replay_request for a person to send.

Exit codes: 0 report written and valid; 2 report written but failed validation
(printed); 1 the run could not produce a report.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import (answers, cohorts, detector, diagnoser, gap_analyzer, ingest, loop_state, metrics as metrics_mod,
               prescriber, report_builder, standard as standard_mod)

PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SCHEMA = os.path.join("tools", "nexus-loop-kit", "schema", "loop-report.schema.json")

SYSTEM_NOTES = (
    "REAL: every metric, finding, diagnosis, impact figure, standard and gap is computed deterministically from "
    "the corpus files (sessions, agent_steps, config_timeline, catalog, labels) with no language model and no "
    "network call. Six independent scanners look for a new under-served intent, empty-but-ok tool responses, "
    "turn inflation, a traffic-mix shift, a load spike and a judge-version boundary; every move must clear an "
    "effect-size gate and a z >= 4 noise gate; lookalikes are dismissed only after their own checks pass. "
    "Config changes are attributed by tenant (including tenant '*'), kind and target. "
    "Operator asks: A01, A03, A04, A06, A08 as per-tenant measured metrics; A02 stratified by intent and at a fixed "
    "intent mix month over month; A05 as milestone funnels from catalog.milestones; A07 as a before/after on the "
    "changed agent at a fixed intent mix that refuses quality across a judge-version boundary; A10 as a deterministic "
    "review scope ranked by the judged score within its version; A09 and A11 as gaps; C2 breakdown refused. "
    "JUDGED: quality_score is the corpus's pre-computed judge output; it is calibrated here against "
    "labels/rubric_scores.jsonl, never re-judged. No new judge was built, so abandonment REASON (A09) is reported "
    "as REQUIRES_NEW_JUDGE with only its measured half answered. "
    "STUBBED / HUMAN: prescriptions are proposals at autonomy rung L1 (a person applies them); replay verification "
    "is not run by this system — each prescription carries a replay_request and a golden set drawn from "
    "labels/outcome_labels.jsonl, and recorded outcomes/decisions are merged back via --verifications/--decisions. "
    "{loop_state} "
    "AT 100x SCALE: sessions are held in memory (~80k rows today) and would need to become a streamed, "
    "per-day aggregate like the step cubes; per-day latency lists would need a sketch (e.g. t-digest) instead of "
    "raw values; everything else is already one streaming pass."
)


def _resolve_schema(path: str) -> str:
    for candidate in (path, os.path.join(PACKAGE_ROOT, path), os.path.join(PACKAGE_ROOT, DEFAULT_SCHEMA)):
        if candidate and os.path.exists(candidate):
            return candidate
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Nexus Loop: detect, diagnose and prescribe from agent logs.")
    ap.add_argument("--kit", default="kit", help="kit directory (corpus/, catalog.json, manifest.json, labels/)")
    ap.add_argument("--corpus-subdir", default="corpus", help="corpus or corpus_sample")
    ap.add_argument("--team", default="nexus-team")
    ap.add_argument("--out", default="my-loop-report.json")
    ap.add_argument("--schema", default=DEFAULT_SCHEMA)
    ap.add_argument("--decisions", default=None, help="optional JSON of recorded human approvals")
    ap.add_argument("--verifications", default=None, help="optional JSON of recorded replay outcomes")
    a = ap.parse_args()

    t0 = time.time()
    manifest = ingest.load_manifest(a.kit)
    catalog = ingest.load_catalog(a.kit)
    sessions = ingest.load_sessions(a.kit, a.corpus_subdir)
    if not sessions:
        print("ERROR: no sessions in %s/%s — refusing to write an empty report" % (a.kit, a.corpus_subdir), file=sys.stderr)
        return 1
    config_changes = ingest.load_config_timeline(a.kit, a.corpus_subdir)
    step_cubes = ingest.build_step_cubes(a.kit, a.corpus_subdir)
    indices = cohorts.build_indices(sessions)
    last_day = max(manifest.get("days", 0) - 1, max(s["day"] for s in sessions))
    print("loaded %d sessions, %d config changes, %d days (%.1fs)" % (len(sessions), len(config_changes), last_day + 1, time.time() - t0))
    if not config_changes:
        print("WARNING: config_timeline is empty — no finding can be attributed to a change", file=sys.stderr)

    t1 = time.time()
    candidates = detector.scan_all(sessions, indices, step_cubes, last_day)
    for kind, cands in candidates.items():
        print("  scanner %-20s %d candidate(s)" % (kind, len(cands)))
    findings, diagnoses = diagnoser.build_findings_and_diagnoses(candidates, sessions, indices, config_changes)
    regressions = [f for f in findings if f["is_regression"]]

    audit, rule = gap_analyzer.cardinality_audit(catalog, sessions)
    gaps = gap_analyzer.build_gaps(catalog, sessions, audit, rule)
    metrics = metrics_mod.build_headline_metrics(sessions, step_cubes, a.kit, audit)
    quality_calibration = next((m["calibration"] for m in metrics if m["id"] == "m_quality_score"), None)
    metrics += answers.build_answers(sessions, catalog, a.kit, config_changes, findings, last_day, quality_calibration)
    metrics_mod.apply_breakdown_budgets(metrics, audit)

    golden_sets = prescriber.build_golden_sets(a.kit, sessions, regressions)
    standard = standard_mod.build_standard(sessions, regressions, {t: g["version"] for t, g in golden_sets.items()})
    prescriptions = prescriber.build_prescriptions(findings, diagnoses, golden_sets)

    warnings = loop_state.merge_decisions(prescriptions, a.decisions)
    verifications, vwarn = loop_state.build_verifications(prescriptions, a.verifications)
    warnings += vwarn
    self_assessment = loop_state.build_self_assessment(prescriptions, verifications)
    for w in warnings:
        print("WARNING: " + w, file=sys.stderr)
    decided = sum(1 for p in prescriptions if p.get("approval"))
    state_note = ("LOOP STATE: %d of %d prescription(s) carry a recorded human decision; %d replay outcome(s) recorded."
                  % (decided, len(prescriptions), len(verifications)))
    print("detection + diagnosis + prescription done (%.1fs)" % (time.time() - t1))

    report = report_builder.build_report(
        a.team, manifest.get("corpus_variant", "unknown"), metrics, findings, diagnoses, gaps,
        SYSTEM_NOTES.replace("{loop_state}", state_note), standard, prescriptions, verifications, self_assessment)

    out_dir = os.path.dirname(os.path.abspath(a.out))
    os.makedirs(out_dir, exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print("wrote %s" % a.out)

    problems = report_builder.link_errors(report)
    schema_path = _resolve_schema(a.schema)
    if schema_path:
        problems += report_builder.validate_report(report, schema_path)
        if not problems:
            print("schema: VALID against %s" % schema_path)
    else:
        print("WARNING: schema file not found (%s) — report written but NOT schema-validated" % a.schema, file=sys.stderr)

    print("-" * 70)
    print("findings: %d regressions, %d dismissed lookalikes | prescriptions: %d | gaps: %s"
          % (len(regressions), len(findings) - len(regressions), len(prescriptions), ", ".join(g["ask_id"] for g in gaps)))
    for fd in findings:
        d = next(x for x in diagnoses if x["finding_id"] == fd["id"])
        print("  [%s] %-4s %-18s %-50s day %3d-%-3d %s" % (
            "REGRESSION" if fd["is_regression"] else "dismissed ", fd["id"], fd["tenant"], json.dumps(fd["cohort"])[:50],
            fd["window"]["from_day"], fd["window"]["to_day"], d["cause_class"]))
    if problems:
        print("\nREPORT FAILED VALIDATION (%d problem(s)):" % len(problems), file=sys.stderr)
        for p in problems[:30]:
            print("  " + p, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
