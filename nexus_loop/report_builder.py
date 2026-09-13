"""Assemble the final dict to the exact shape of schema/loop-report.schema.json
and validate it with `jsonschema` before it's ever written to disk. There is no
`nlkit.schema` module to lean on (see plan.md's Setup Validation) — we build the
dict by hand and validate against the kit's own schema file directly.
"""
from __future__ import annotations

import datetime
import json
from typing import List

from . import metrics as metrics_mod


def build_report(team: str, corpus_variant: str, sessions: List[dict], step_cubes,
                  findings: List[dict], diagnoses: List[dict], gaps: List[dict],
                  kit_dir: str, system_notes: str, standard: List[dict] = None) -> dict:
    headline = metrics_mod.build_headline_metrics(sessions, step_cubes, kit_dir)
    report = {
        "team": team,
        "corpus": corpus_variant,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "system_notes": system_notes,
        "metrics": [metrics_mod.strip_internal_fields(m) for m in headline],
        "findings": findings,
        "diagnoses": diagnoses,
        "gaps": gaps,
    }
    if standard:
        report["standard"] = standard
    return report


def validate_report(report: dict, schema_path: str) -> None:
    import jsonschema
    with open(schema_path, encoding="utf-8") as f:
        schema = json.load(f)
    jsonschema.validate(instance=report, schema=schema)
