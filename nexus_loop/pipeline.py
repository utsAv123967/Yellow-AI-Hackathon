#!/usr/bin/env python3
"""Task 1 entry point: Detection & Diagnosis.

    python -m nexus_loop.pipeline --kit kit --corpus-subdir corpus_sample --out my-loop-report.json
    python -m nexus_loop.pipeline --kit kit --corpus-subdir corpus --out my-loop-report.json

No tenant, intent, agent id, or day number is hardcoded anywhere in this
package — every cohort is discovered from whatever is actually in --kit, so the
same command runs unmodified against the sealed corpus on day 6.
"""
from __future__ import annotations

import argparse
import json
import os
import time

from . import cohorts, detector, diagnoser, gap_analyzer, ingest, report_builder, standard as standard_mod


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kit", default="kit", help="kit directory (contains corpus/, catalog.json, manifest.json, ...)")
    ap.add_argument("--corpus-subdir", default="corpus", help="corpus or corpus_sample")
    ap.add_argument("--team", default="nexus-team")
    ap.add_argument("--out", default="my-loop-report.json")
    ap.add_argument("--schema", default=os.path.join("tools", "nexus-loop-kit", "schema", "loop-report.schema.json"))
    a = ap.parse_args()

    t0 = time.time()
    manifest = ingest.load_manifest(a.kit)
    catalog = ingest.load_catalog(a.kit)
    sessions = ingest.load_sessions(a.kit, a.corpus_subdir)
    config_changes = ingest.load_config_timeline(a.kit, a.corpus_subdir)
    step_cubes = ingest.build_step_cubes(a.kit, a.corpus_subdir)
    indices = cohorts.build_indices(sessions)
    last_day = manifest["days"] - 1
    print("loaded %d sessions, %d config changes (%.1fs)" % (len(sessions), len(config_changes), time.time() - t0))

    t1 = time.time()
    candidates = detector.scan_all(sessions, indices, step_cubes, last_day)
    for kind, cands in candidates.items():
        print("  scanner %-20s %d candidate(s)" % (kind, len(cands)))
    findings, diagnoses = diagnoser.build_findings_and_diagnoses(candidates, sessions, indices, config_changes)
    gaps = gap_analyzer.build_gaps(catalog)
    print("detection + diagnosis done (%.1fs)" % (time.time() - t1))

    standard = standard_mod.build_standard(sessions)
    print("standard: %d cohort baselines mined" % len(standard))

    system_notes = (
        "Task 1 (Detection & Diagnosis) output: metrics, findings (confirmed regressions plus "
        "explicitly examined-and-dismissed lookalikes), diagnoses, measurement gaps, and a mined "
        "standard (what 'good' looks like per tenant+intent, from this deployment's own v3_agent "
        "traffic). Zero LLM/API calls anywhere — quality_score is the corpus's own pre-computed "
        "judged signal, calibrated here against labels/rubric_scores.jsonl rather than re-judged. "
        "prescriptions/verifications/self_assessment are out of scope for this task and are "
        "omitted rather than stubbed."
    )
    report = report_builder.build_report(a.team, manifest["corpus_variant"], sessions, step_cubes,
                                          findings, diagnoses, gaps, a.kit, system_notes, standard)

    if os.path.exists(a.schema):
        report_builder.validate_report(report, a.schema)
        print("schema: VALID against %s" % a.schema)
    else:
        print("WARNING: schema not found at %s — skipped validation" % a.schema)

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print("wrote %s" % a.out)

    regs = [f for f in findings if f["is_regression"]]
    dismissed = [f for f in findings if not f["is_regression"]]
    print("-" * 70)
    print("findings: %d regressions, %d dismissed lookalikes" % (len(regs), len(dismissed)))
    for f in findings:
        tag = "REGRESSION" if f["is_regression"] else "dismissed "
        print("  [%s] %-4s %-18s %-30s day %3d-%-3d" % (
            tag, f["id"], f["tenant"], json.dumps(f["cohort"]), f["window"]["from_day"], f["window"]["to_day"]))
    print("gaps: %d (%s)" % (len(gaps), ", ".join(g["ask_id"] for g in gaps)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
