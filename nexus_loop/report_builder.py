"""Assemble the final dict to the shape of schema/loop-report.schema.json.

Validation uses nexus_loop.schema_check (stdlib only). The pipeline writes the
report FIRST and validates it second, so a validation problem is reported loudly
without ever leaving the sealed run with no output at all.
"""
from __future__ import annotations

import datetime
from typing import List, Optional

from . import schema_check


def build_report(team: str, corpus_variant: str, metrics: List[dict], findings: List[dict],
                  diagnoses: List[dict], gaps: List[dict], system_notes: str,
                  standard: Optional[List[dict]] = None, prescriptions: Optional[List[dict]] = None,
                  verifications: Optional[List[dict]] = None, self_assessment: Optional[dict] = None) -> dict:
    report = {
        "team": team,
        "corpus": corpus_variant,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "system_notes": system_notes,
        "metrics": metrics,
        "findings": findings,
        "diagnoses": diagnoses,
        "gaps": gaps,
    }
    if standard:
        report["standard"] = standard
    report["prescriptions"] = prescriptions or []
    report["verifications"] = verifications or []
    if self_assessment is not None:
        report["self_assessment"] = self_assessment
    return report


def validate_report(report: dict, schema_path: str) -> List[str]:
    return schema_check.validate_file(report, schema_path)


def link_errors(report: dict) -> List[str]:
    """Referential checks the JSON schema cannot express."""
    errors = []
    fids = {f["id"] for f in report["findings"]}
    dids = {d["id"] for d in report["diagnoses"]}
    pids = {p["id"] for p in report.get("prescriptions", [])}
    for d in report["diagnoses"]:
        if d["finding_id"] not in fids:
            errors.append("diagnosis %s -> missing finding %s" % (d["id"], d["finding_id"]))
    for p in report.get("prescriptions", []):
        if p["diagnosis_id"] not in dids:
            errors.append("prescription %s -> missing diagnosis %s" % (p["id"], p["diagnosis_id"]))
    for v in report.get("verifications", []):
        if v["prescription_id"] not in pids:
            errors.append("verification -> missing prescription %s" % v["prescription_id"])
    for f in report["findings"]:
        if not f["is_regression"] and not f.get("not_a_regression_because"):
            errors.append("dismissed finding %s has no not_a_regression_because" % f["id"])
    return errors
