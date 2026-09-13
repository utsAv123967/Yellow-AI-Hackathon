#!/usr/bin/env python3
"""The approval gate's back end: record a human decision on a prescription.

    python -m nexus_loop.decide --report my-loop-report.json --list
    python -m nexus_loop.decide --report my-loop-report.json --prescription p1 \
           --verdict accepted --by "Ops lead" --reason "KB gap confirmed; content pack is ready"

It writes the decision in two places:
  * straight into the report (prescriptions[].approval), so the file on disk shows the decision now;
  * into decisions.json (next to the report unless --decisions is given), keyed by prescription id
    and carrying the prescription's signature, so the next pipeline run re-attaches it with
    --decisions and a changed diagnosis can never inherit it.

A rejection with a reason is as valid an answer as an approval; a reason is required either way.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

from . import report_builder
from .loop_state import VERDICTS_DECISION
from .pipeline import DEFAULT_SCHEMA, _resolve_schema


def _write_json_atomic(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Record an approve/reject/defer decision on a prescription.")
    ap.add_argument("--report", required=True)
    ap.add_argument("--list", action="store_true", help="list prescriptions and their current decision")
    ap.add_argument("--prescription")
    ap.add_argument("--verdict", choices=sorted(VERDICTS_DECISION))
    ap.add_argument("--by", help="who decided")
    ap.add_argument("--reason", help="why (required)")
    ap.add_argument("--decisions", default=None, help="decisions file to upsert (default: decisions.json beside the report)")
    a = ap.parse_args()

    with open(a.report, encoding="utf-8") as f:
        report = json.load(f)
    prescriptions = report.get("prescriptions", [])

    if a.list:
        for p in prescriptions:
            ap_ = p.get("approval") or {}
            print("%-4s %-14s %-24s -> %s" % (p["id"], p["change_type"], p["target"],
                                               "%s by %s: %s" % (ap_.get("verdict"), ap_.get("decided_by"), ap_.get("reason")) if ap_ else "undecided"))
            print("     asking approval for: %s" % p["decision"]["asking_approval_for"])
        return 0

    missing = [n for n in ("prescription", "verdict", "by", "reason") if not getattr(a, n)]
    if missing:
        print("ERROR: missing --%s" % ", --".join(missing), file=sys.stderr)
        return 1
    p = next((x for x in prescriptions if x["id"] == a.prescription), None)
    if not p:
        print("ERROR: no prescription %s in %s (have: %s)" % (a.prescription, a.report, ", ".join(x["id"] for x in prescriptions)), file=sys.stderr)
        return 1

    approval = {"verdict": a.verdict, "decided_by": a.by, "reason": a.reason,
                "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    p["approval"] = approval
    note = "LOOP STATE: %d of %d prescription(s) carry a recorded human decision" % (
        sum(1 for x in prescriptions if x.get("approval")), len(prescriptions))
    notes = report.get("system_notes", "")
    if "LOOP STATE:" in notes:
        head, tail = notes.split("LOOP STATE:", 1)
        rest = tail.split(";", 1)[1] if ";" in tail else ""
        report["system_notes"] = head + note + (";" + rest if rest else ".")
    _write_json_atomic(a.report, report)

    decisions_path = a.decisions or os.path.join(os.path.dirname(os.path.abspath(a.report)), "decisions.json")
    decisions = {}
    if os.path.exists(decisions_path):
        with open(decisions_path, encoding="utf-8") as f:
            decisions = json.load(f)
    decisions[p["id"]] = dict(approval, signature=p.get("signature"))
    _write_json_atomic(decisions_path, decisions)

    problems = report_builder.link_errors(report)
    schema = _resolve_schema(DEFAULT_SCHEMA)
    if schema:
        problems += report_builder.validate_report(report, schema)
    print("recorded %s on %s (%s) -> %s and %s" % (a.verdict, p["id"], p["target"], a.report, decisions_path))
    if problems:
        print("REPORT FAILED VALIDATION after the write:", file=sys.stderr)
        for x in problems:
            print("  " + x, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
