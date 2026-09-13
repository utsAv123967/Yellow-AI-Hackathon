"""Stdlib tests for the algorithms the report stands on, the rules the code must keep, and an end-to-end
smoke run on the 5% sample.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from nexus_loop import changepoint, gap_analyzer, loop_state, schema_check  # noqa: E402
from nexus_loop.detector import nearby_config_changes  # noqa: E402
from nexus_loop.ingest import is_silent_empty  # noqa: E402
from nexus_loop.standard import session_in_finding  # noqa: E402

KIT = os.path.join(ROOT, "kit")
SCHEMA = os.path.join(ROOT, "tools", "nexus-loop-kit", "schema", "loop-report.schema.json")
PACKAGE = os.path.join(ROOT, "nexus_loop")


class ChangepointTests(unittest.TestCase):
    def _spike(self):
        # 0.01 before, 0.20 during days 10-24, 0.00 after: the recovery edge (-0.20) is a hair
        # larger than the onset edge (+0.19) — exactly the shape that fooled abs_delta
        den = {d: 100 for d in range(41)}
        num = {d: (1 if d < 10 else 20 if d < 25 else 0) for d in range(41)}
        return num, den

    def test_abs_delta_locks_onto_recovery(self):
        num, den = self._spike()
        cp = changepoint.find_rate_changepoint(num, den, 0, 40, window=7, criterion="abs_delta")
        self.assertEqual(cp.day, 25)
        self.assertLess(cp.delta, 0)

    def test_rise_finds_onset_and_fall_finds_recovery(self):
        num, den = self._spike()
        self.assertEqual(changepoint.find_rate_changepoint(num, den, 0, 40, window=7, criterion="rise").day, 10)
        self.assertEqual(changepoint.find_rate_changepoint(num, den, 0, 40, window=7, criterion="fall").day, 25)

    def test_rise_works_from_zero_baseline_where_ratio_cannot(self):
        # a true onset from an exactly-zero baseline: the ratio criterion cannot score that boundary
        # (division by a zero before-rate), so it can only land late, on a window already contaminated
        den = {d: 100 for d in range(30)}
        num = {d: (0 if d < 12 else 15) for d in range(30)}
        self.assertEqual(changepoint.find_rate_changepoint(num, den, 0, 29, window=7, criterion="rise").day, 12)
        ratio = changepoint.find_rate_changepoint(num, den, 0, 29, window=7, criterion="rise_ratio")
        self.assertGreater(ratio.day, 12)
        self.assertGreater(ratio.before_rate, 0)

    def test_partial_window_fallback_only_when_strict_finds_nothing(self):
        den = {d: 100 for d in range(10)}
        num = {d: (0 if d < 3 else 20) for d in range(10)}
        self.assertIsNone(changepoint.find_rate_changepoint(num, den, 0, 9, window=7, criterion="rise"))
        cp = changepoint.find_rate_changepoint(num, den, 0, 9, window=7, criterion="rise", allow_partial_window=True)
        self.assertEqual(cp.day, 3)

    def test_recovery_day(self):
        num, den = self._spike()
        self.assertEqual(changepoint.find_recovery_day(num, den, 10, 40, target_rate=0.01, tolerance=0.02,
                                                       window=3, direction="down"), 25)


class StatisticsTests(unittest.TestCase):
    def test_two_proportions(self):
        self.assertGreater(changepoint.z_two_proportions(0, 100, 20, 100), 4)
        self.assertAlmostEqual(changepoint.z_two_proportions(10, 100, 10, 100), 0.0)
        self.assertIsNone(changepoint.z_two_proportions(0, 0, 1, 10))
        self.assertLess(changepoint.z_two_proportions(80, 100, 30, 100), -4)

    def test_two_means(self):
        # 100 sessions of 4 turns (with spread) vs 100 of 6 turns
        before = [4 + (i % 3 - 1) for i in range(100)]
        after = [6 + (i % 3 - 1) for i in range(100)]
        z = changepoint.z_two_means(sum(before), sum(x * x for x in before), len(before),
                                    sum(after), sum(x * x for x in after), len(after))
        self.assertGreater(z, 10)
        self.assertIsNone(changepoint.z_two_means(4, 16, 1, 6, 36, 1))


class FieldSemanticsTests(unittest.TestCase):
    def test_silent_empty(self):
        self.assertTrue(is_silent_empty({"outcome": "ok", "result_field_count": 0, "response_bytes": 2}))
        self.assertTrue(is_silent_empty({"outcome": "ok", "response_bytes": 2}))
        self.assertFalse(is_silent_empty({"outcome": "ok"}))                        # missing is not zero
        self.assertFalse(is_silent_empty({"outcome": "ok", "result_field_count": 3}))
        self.assertFalse(is_silent_empty({"outcome": "error", "result_field_count": 0}))

    def test_config_join_keeps_wildcard_and_filters_kind_target(self):
        rows = [{"day": 28, "tenant": "*", "kind": "judge", "target": "quality_rubric"},
                {"day": 29, "tenant": "t1", "kind": "tool", "target": "tool_a"},
                {"day": 29, "tenant": "t1", "kind": "prompt", "target": "tool_a"},
                {"day": 29, "tenant": "t2", "kind": "tool", "target": "tool_a"}]
        self.assertEqual([r["kind"] for r in nearby_config_changes(rows, "t1", 28, kind="judge")], ["judge"])
        tool = nearby_config_changes(rows, "t1", 28, kind="tool", target="tool_a")
        self.assertEqual([(r["tenant"], r["kind"]) for r in tool], [("t1", "tool")])

    def test_session_in_finding(self):
        f = {"tenant": "t1", "cohort": {"intent": "a", "tool": "x"}, "window": {"from_day": 5, "to_day": 9}}
        self.assertTrue(session_in_finding({"tenant": "t1", "intent": "a", "day": 5}, f))
        self.assertFalse(session_in_finding({"tenant": "t1", "intent": "b", "day": 5}, f))
        self.assertFalse(session_in_finding({"tenant": "t1", "intent": "a", "day": 10}, f))
        self.assertFalse(session_in_finding({"tenant": "t2", "intent": "a", "day": 6}, f))


class GapAndCardinalityTests(unittest.TestCase):
    def test_cardinality_refusal(self):
        catalog = {"cardinality_budgets": {"session.custom_dims.ref": 2, "session.session_id": 0, "_rule": "refuse"},
                   "fields": {"session.custom_dims.segment": {"cardinality_budget": 50}}}
        sessions = [{"session_id": "s%d" % i, "custom_dims": {"ref": "r%d" % i, "segment": "m"}} for i in range(3)]
        audit, rule = gap_analyzer.cardinality_audit(catalog, sessions)
        by_field = {a["field"]: a for a in audit}
        self.assertTrue(by_field["session.custom_dims.ref"]["refused"])
        self.assertFalse(by_field["session.custom_dims.segment"]["refused"])
        gaps = gap_analyzer._cardinality_gaps(audit, rule)
        self.assertTrue(any(g["verdict"] == "CARDINALITY_REFUSED" and "budget of 2" in g["why"] for g in gaps))


class SchemaCheckTests(unittest.TestCase):
    SCHEMA = {"type": "object", "required": ["a"], "properties": {
        "a": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["x"],
              "properties": {"x": {"enum": [1, 2]}, "flag": {"type": "boolean"}},
              "if": {"properties": {"flag": {"const": True}}, "required": ["flag"]},
              "then": {"required": ["why"]}}}}}

    def test_valid_and_invalid(self):
        self.assertEqual(schema_check.validate({"a": [{"x": 1}]}, self.SCHEMA), [])
        self.assertTrue(schema_check.validate({}, self.SCHEMA))
        self.assertTrue(schema_check.validate({"a": []}, self.SCHEMA))
        self.assertTrue(schema_check.validate({"a": [{"x": 3}]}, self.SCHEMA))
        self.assertTrue(schema_check.validate({"a": [{"x": 1, "flag": True}]}, self.SCHEMA))   # if/then
        self.assertEqual(schema_check.validate({"a": [{"x": 1, "flag": True, "why": "y"}]}, self.SCHEMA), [])


class LoopStateTests(unittest.TestCase):
    def setUp(self):
        self.p = [{"id": "p1", "change_type": "kb.add", "signature": "sig1",
                   "predicted_delta": {"metric": "resolution_rate", "from": 0.3, "to": 0.8}}]
        self.tmp = tempfile.mkdtemp()

    def _write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            json.dump(data, f)
        return path

    def test_decisions_reject_unknown_bad_and_stale(self):
        path = self._write("d.json", {"p1": {"verdict": "rejected", "reason": "r", "signature": "sig1"},
                                      "p2": {"verdict": "accepted"}})
        warnings = loop_state.merge_decisions(self.p, path)
        self.assertEqual(self.p[0]["approval"]["verdict"], "rejected")
        self.assertEqual(len(warnings), 1)
        stale = self._write("s.json", {"p1": {"verdict": "accepted", "signature": "other"}})
        p = [dict(self.p[0])]
        p[0].pop("approval", None)
        self.assertEqual(len(loop_state.merge_decisions(p, stale)), 1)
        self.assertNotIn("approval", p[0])

    def test_verifications_and_self_assessment(self):
        path = self._write("v.json", [
            {"prescription_id": "p1", "replay_run_id": "rp_1", "verdict": "improved", "before": 0.3, "after": 0.75},
            {"prescription_id": "p1", "replay_run_id": "manual", "verdict": "improved"},
            {"prescription_id": "p1", "replay_run_id": "rp_2", "verdict": "better"}])
        ver, warnings = loop_state.build_verifications(self.p, path)
        self.assertEqual(len(ver), 1)
        self.assertEqual(len(warnings), 2)
        self.assertAlmostEqual(ver[0]["prediction_error"], 0.05)
        sa = loop_state.build_self_assessment(self.p, ver)
        self.assertEqual(sa["cycles"], 1)
        self.assertEqual(sa["prescription_accuracy"]["kb.add"]["hit_rate"], 1.0)
        self.assertEqual(sa["downweighted"], [])


class RuleGuardTests(unittest.TestCase):
    """Rule-level checks on the source itself: nothing from the practice corpus is baked in."""

    def _sources(self):
        for fn in sorted(os.listdir(PACKAGE)):
            if fn.endswith(".py"):
                with open(os.path.join(PACKAGE, fn), encoding="utf-8") as f:
                    yield fn, f.read()

    @unittest.skipUnless(os.path.exists(os.path.join(KIT, "catalog.json")), "kit not present")
    def test_no_corpus_specific_names_in_code(self):
        with open(os.path.join(KIT, "catalog.json"), encoding="utf-8") as f:
            catalog = json.load(f)
        names = set()
        for tenant, spec in catalog.get("tenants", {}).items():
            names.add(tenant)
            names.update(spec.get("agents", []))
            names.update(spec.get("tools", []))
        for tenant, journeys in catalog.get("milestones", {}).items():
            names.update(journeys)
        for fn, src in self._sources():
            for name in names:
                self.assertIsNone(re.search(r"\b%s\b" % re.escape(name), src), "%s contains corpus name %r" % (fn, name))

    def test_no_answer_key_no_model_and_network_only_in_replay_client(self):
        for fn, src in self._sources():
            self.assertNotIn("ground_truth", src, fn)
            self.assertIsNone(re.search(r"import (openai|anthropic|jsonschema)|from (openai|anthropic|jsonschema)", src), fn)
            if fn != "replay_client.py":
                self.assertIsNone(re.search(r"^\s*(import|from)\s+(urllib|http|socket|requests)\b", src, re.M), fn)
        with open(os.path.join(PACKAGE, "pipeline.py"), encoding="utf-8") as f:
            self.assertNotIn("replay_client", f.read())


@unittest.skipUnless(os.path.exists(os.path.join(KIT, "corpus_sample", "sessions.jsonl.gz")), "sample corpus not present")
class PipelineSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.outs = []
        for i in range(2):
            out = os.path.join(cls.tmp, "r%d.json" % i)
            p = subprocess.run([sys.executable, "-m", "nexus_loop.pipeline", "--kit", KIT, "--corpus-subdir", "corpus_sample",
                                "--out", out], cwd=ROOT, capture_output=True, text=True)
            cls.outs.append((p.returncode, out, p.stderr))

    def test_exit_zero_and_schema_valid(self):
        for code, out, err in self.outs:
            self.assertEqual(code, 0, err)
            with open(out, encoding="utf-8") as f:
                self.assertEqual(schema_check.validate_file(json.load(f), SCHEMA), [])

    def test_deterministic(self):
        reports = []
        for _, out, _ in self.outs:
            with open(out, encoding="utf-8") as f:
                r = json.load(f)
            r.pop("generated_at")
            reports.append(json.dumps(r, sort_keys=True))
        self.assertEqual(reports[0], reports[1])

    def test_required_asks_and_honesty_fields(self):
        with open(self.outs[0][1], encoding="utf-8") as f:
            r = json.load(f)
        asks = {m["ask_id"] for m in r["metrics"]}
        for ask in ("A01", "A02", "A03", "A04", "A05", "A06", "A07", "A08", "A10"):
            self.assertIn(ask, asks)
        gaps = {g["ask_id"]: g for g in r["gaps"]}
        self.assertEqual(gaps["A11"]["verdict"], "NOT_MEASURABLE")
        self.assertEqual(set(gaps["A11"]["required_event"]["fields"]), {"from_target", "to_target", "reason", "recovered"})
        self.assertEqual(gaps["A09"]["verdict"], "REQUIRES_NEW_JUDGE")
        self.assertTrue(any(g["verdict"] == "CARDINALITY_REFUSED" for g in r["gaps"]))
        first = {}
        for m in r["metrics"]:
            first.setdefault(m["ask_id"], m["fidelity"])
            self.assertIn("value", m["coverage"])
            if m["fidelity"] == "judged":
                self.assertIsNotNone(m["calibration"])
        self.assertEqual(first["A04"], "measured")
        self.assertNotIn("A09", first)


if __name__ == "__main__":
    unittest.main()
