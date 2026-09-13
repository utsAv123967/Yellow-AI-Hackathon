# Nexus Loop Testing Runbook

**Generated:** 2026-09-13
**Repository:** `/home/aayush/Projects/Yellow-AI-Hackathon`

## Purpose

This runbook validates the Nexus Loop from ingestion through detection, diagnosis, prescription, replay verification, human approval/rejection, scoring, and unseen-corpus robustness testing before dashboard development.

---

## Current Validation Status

| Status | Item |
|---|---|
| PASS | Unit tests: 19/19 passed |
| PASS | Full practice pipeline: schema valid |
| PASS | Detection: 3/3 real regressions found |
| PASS | Decoys: 3/3 correctly dismissed |
| PASS | Practice machine score: 55/55 |
| PASS | Deterministic output |
| PASS | Replay service started successfully |
| PASS | Replay request generation |
| PASS | Approval/rejection backend |
| PASS | One replay verification recorded |
| PASS | Closed-loop report rebuilt successfully |
| **CHECK** | Unseen-corpus suite: one layout scored 53.53/55 because the load decoy was not examined. Investigate the load-event scanner before calling the system fully sealed-run ready. |

The current practice report **finds**:

1. `kb.gap`
2. `tool.contract_break`
3. `prompt.regression`

The current practice report **dismisses**:

1. `traffic_mix`
2. `load`
3. `judge_change`

The current test replay returned an improved affected cohort but **failed the golden set guard**. This means the change must not automatically be treated as safe to ship.

---

## 0. Open the Project

```bash
cd /home/aayush/Projects/Yellow-AI-Hackathon
```

---

## 1. Check Required Input Files

Run:

```bash
test -f kit/catalog.json && echo "catalog: OK"
test -f kit/manifest.json && echo "manifest: OK"
test -f kit/corpus/sessions.jsonl.gz && echo "sessions: OK"
test -f kit/corpus/agent_steps.jsonl.gz && echo "steps: OK"
test -f kit/corpus/config_timeline.csv && echo "config timeline: OK"
test -f tools/nexus-loop-kit/schema/loop-report.schema.json && echo "schema: OK"
```

**Expected result:** every line ends in `OK`.

---

## 2. Run the Automated Test Suite

```bash
python -m unittest discover -s tests -v
```

**Expected result:**

```
Ran 19 tests
OK
```

The tests cover:

- Changepoint detection and recovery detection
- Proportion and mean statistics
- Silent tool failure semantics
- Wildcard tenant config joins
- Cardinality refusal
- Decision merging
- Replay verification merging
- Schema validation
- Deterministic output
- Rule 1 and Rule 2 source guards
- Absence of corpus-specific hardcoding
- Sample end-to-end pipeline execution

> Do not continue to dashboard work if this fails.

---

## 3. Run the Fast Sample Pipeline

Use this for quick iteration:

```bash
python -m nexus_loop.pipeline \
  --kit kit \
  --corpus-subdir corpus_sample \
  --team nexus-team \
  --out loop-report.sample.json
```

**Expected output includes:**

```
schema: VALID against tools/nexus-loop-kit/schema/loop-report.schema.json
```

The sample corpus is faster but has lower statistical power. The current sample run found one regression and three dismissed lookalikes. **It is not the final score.**

---

## 4. Run the Authoritative Full Practice Pipeline

```bash
python -m nexus_loop.pipeline \
  --kit kit \
  --team nexus-team \
  --out loop-report.practice.json
```

**Expected output shape:**

```
loaded 80252 sessions, 16 config changes, 56 days
schema: VALID against tools/nexus-loop-kit/schema/loop-report.schema.json
findings: 3 regressions, 3 dismissed lookalikes | prescriptions: 3
```

The three real causes should be:

- `kb.gap`
- `tool.contract_break`
- `prompt.regression`

The three dismissals should be:

- `traffic_mix`
- `load`
- `judge_change`

Inspect the high-level report contents:

```python
import json
r = json.load(open("loop-report.practice.json"))
print("regressions:", sum(f["is_regression"] for f in r["findings"]))
print("dismissals:", sum(not f["is_regression"] for f in r["findings"]))
print("prescriptions:", len(r["prescriptions"]))
print("gaps:", [(g["ask_id"], g["verdict"]) for g in r["gaps"]])
print("sections:", list(r.keys()))
```

**Expected:**

```
regressions: 3
dismissals: 3
prescriptions: 3
```

---

## 5. Score the Practice Report

```bash
python tools/nexus-loop-kit/score.py \
  --report loop-report.practice.json \
  --ground-truth kit/ground_truth/ground_truth.json \
  --json
```

**Current verified result:**

```
machine_score: 55.0
```

**Expected section scores:**

| Section | Score |
|---|---|
| Diagnostic accuracy | 20/20 |
| Specificity | 15/15 |
| Honesty | 12/12 |
| Loop completeness | 8/8 |

> Before replay and approval artifacts exist, loop completeness will report that the return arrow is incomplete. That is expected at that stage.

---

## 6. Check Determinism

```bash
python -m nexus_loop.pipeline \
  --kit kit \
  --corpus-subdir corpus_sample \
  --out /tmp/report-one.json

python -m nexus_loop.pipeline \
  --kit kit \
  --corpus-subdir corpus_sample \
  --out /tmp/report-two.json

cmp \
  <(python -c 'import json; r=json.load(open("/tmp/report-one.json")); r.pop("generated_at", None); print(json.dumps(r, sort_keys=True))') \
  <(python -c 'import json; r=json.load(open("/tmp/report-two.json")); r.pop("generated_at", None); print(json.dumps(r, sort_keys=True))') \
  && echo "deterministic: PASS"
```

**Current verified result:**

```
deterministic: PASS
```

---

## 7. Test the Approval Gate

List prescriptions:

```bash
python -m nexus_loop.decide \
  --report loop-report.practice.json \
  --list
```

**Expected prescriptions:**

```
p1  kb.add
p2  tool.validate
p3  prompt.edit
```

Record a test rejection:

```bash
python -m nexus_loop.decide \
  --report loop-report.practice.json \
  --prescription p1 \
  --verdict rejected \
  --by "test operator" \
  --reason "Rejecting during validation because the proposed change has not passed replay." \
  --decisions decisions.json
```

Verify the recorded state:

```bash
python -m nexus_loop.decide \
  --report loop-report.practice.json \
  --list
```

**Expected output contains:**

```
p1 ... rejected by test operator
```

The decision is stored in the prescription and in `decisions.json`.

---

## 8. Print Replay Requests Without Spending Budget

```bash
python -m nexus_loop.replay_client \
  --report loop-report.practice.json \
  --team nexus-team
```

This is a **dry run**. It must not contact the replay service.

Each request should contain:

- team
- tenant
- change type
- target
- cohort
- finding window
- golden set

> The replay budget is 40 runs per team. Do not resend identical requests.

---

## 9. Start the Local Replay Service

Use a separate terminal:

```bash
cd /home/aayush/Projects/Yellow-AI-Hackathon

python tools/nexus-loop-kit/replay/serve.py \
  --kit kit \
  --port 8719
```

**Expected output:**

```
replay listening on http://127.0.0.1:8719
```

> Keep this terminal open during replay tests.

---

## 10. Send One Replay Request

In the original terminal:

```bash
python -m nexus_loop.replay_client \
  --report loop-report.practice.json \
  --team nexus-team \
  --prescription p1 \
  --send \
  --url http://127.0.0.1:8719 \
  --verifications verifications.json \
  --ledger replay_ledger.json
```

**Current verified result:**

```
run_id: rp_7322eadb
verdict: improved
before: 0.3332
after: 0.7976
golden_set_pass: false
```

**Interpretation:**

- The proposed fix improved the affected cohort.
- The golden set detected collateral damage.
- The fix must not automatically be treated as safe to ship.
- The dashboard must display both the improvement and the failed guard.

---

## 11. Rebuild the Report with Approval and Replay Data

```bash
python -m nexus_loop.pipeline \
  --kit kit \
  --team nexus-team \
  --out loop-report.closed-loop.json \
  --decisions decisions.json \
  --verifications verifications.json
```

Inspect the closed-loop fields:

```python
import json
r = json.load(open("loop-report.closed-loop.json"))
print("approvals:")
for p in r["prescriptions"]:
    print(" ", p["id"], p.get("approval", {}).get("verdict", "undecided"))
print("verifications:")
for v in r["verifications"]:
    print(" ", v["prescription_id"], v["replay_run_id"], v["verdict"], "golden_set_pass=" + str(v.get("golden_set_pass")))
print("self_assessment:")
print(r.get("self_assessment"))
```

**Expected shape:**

```
approvals:
  p1 rejected
  p2 undecided
  p3 undecided
verifications:
  p1 rp_... improved golden_set_pass=False
self_assessment:
  cycles: 1
```

---

## 12. Score the Closed-Loop Report

```bash
python tools/nexus-loop-kit/score.py \
  --report loop-report.closed-loop.json \
  --ground-truth kit/ground_truth/ground_truth.json \
  --json
```

**Current verified result:**

```
machine_score: 55.0
verification: 1 replayed, 1 improved
self_assessment: present
human verdict: 1/3 prescriptions
```

> The machine score remains 55/55. The failed golden-set guard is still a serious human-facing result and must be visible in the dashboard.

---

## 13. Run the Unseen-Corpus Suite

```bash
python scripts/sealed_suite.py
```

**Current verified results:**

| Layout | Score |
|---|---|
| nexus-suite-layout-1 | 55.00 |
| nexus-suite-layout-2 | 53.53 |
| nexus-suite-layout-3 | 55.00 |
| nexus-suite-layout-1-renamed | 55.00 |

**Current issue:**

```
QUIET D2 - not flagged, but never examined either
```

Investigate these areas before final sealed-run readiness:

- `nexus_loop/detector.py`
- `scan_load_events`
- `nexus_loop/diagnoser.py`
- load dismissal logic

> The load event does not need to be classified as a regression when resolution is stable, but it must be examined and dismissed so specificity is preserved.

---

## 14. Final Readiness Checklist

- [x] Unit test suite
- [x] Full practice pipeline exits successfully
- [x] Full practice report schema-valid
- [x] Practice score 55/55
- [x] Three real regressions found
- [x] Three lookalikes dismissed
- [x] A11 reported as `NOT_MEASURABLE`
- [x] A09 reported as `REQUIRES_NEW_JUDGE`
- [x] Tool metrics include v2 coverage information
- [x] Deterministic output
- [x] Replay request generation
- [x] Approval/rejection persistence
- [x] Replay result with `rp_` run ID
- [ ] **Load decoy examined on every unseen layout**

---

## 15. Dashboard Scope After the Checklist

The dashboard should consume `loop-report.json`. Detection logic should remain in `nexus_loop/` and should not be duplicated in the UI.

**Minimum first screen:**

### A. Finding list
- Regression or dismissed lookalike
- Tenant
- Cohort
- Window
- Severity
- Cause class

### B. Finding detail
- Observed value
- Expected/baseline value
- Impact
- Conversations affected
- Share of traffic
- Cost of inaction
- Evidence
- Config change attribution

### C. Decision panel
- Proposed change
- Risk if diagnosis is wrong
- Do-not-ship condition
- Approve
- Reject
- Defer
- Required reason

### D. Verification panel
- Replay run ID
- Before value
- After value
- Predicted delta
- Verdict
- Golden-set result

### E. Honesty panel
- A11 refusal
- Required missing event
- A09 new-judge requirement
- Metric fidelity
- Metric coverage
- Calibration

**Critical message for operators:**

> An improved affected cohort does not necessarily mean a safe-to-ship change.

The first replay demonstrated this directly: the target cohort improved, but the golden-set regression guard failed.

---

## 16. Final Command Sequence After Dashboard Work

```bash
python -m unittest discover -s tests -v

python -m nexus_loop.pipeline \
  --kit kit \
  --team nexus-team \
  --out loop-report.practice.json

python tools/nexus-loop-kit/score.py \
  --report loop-report.practice.json \
  --ground-truth kit/ground_truth/ground_truth.json \
  --json

python scripts/sealed_suite.py
```

Then manually verify in the dashboard that clicking **Approve**, **Reject**, and **Defer**:

- Updates the correct prescription
- Requires and persists the reason
- Writes the decision into the report
- Preserves the finding and diagnosis links
- Displays replay and golden-set status correctly

---

*End of runbook*