# Nexus Loop Detector Audit

**`nexus_loop/` (ours) vs `build_report.py` (teammate): before and after the fixes · 13 Sep 2026**

Both approaches were run on the practice corpus, its 5% sample, six unseen sealed-style corpora and
one corpus with renamed tenants, then traced line by line against the PS and `CLAUDE.md`. The
replay server was never started, and the ground-truth files were used only by `score.py` to score
outputs, never as an input to either system.

---

## 1. Verdict

| | `build_report.py` (teammate) | `nexus_loop` before fixes | `nexus_loop` after fixes |
|---|---|---|---|
| Machine score, practice corpus | 55.0 | 54.5, and **crashes unmodified** | **55.0** |
| Machine score, 7 unseen corpora | 55.0 on each | 41.2–47.8 | **55.0 on each** |
| Every figure computed from data | ❌ answer-key fallbacks, asserted evidence | ✅ | ✅ |
| Runs untouched on this machine | ✅ | ❌ `ModuleNotFoundError: jsonschema` | ✅ |
| Safe to submit on day 6 | ❌ | ❌ | ✅ |

**Decision: submit `nexus_loop`.** After the fixes it matches the teammate's score on every corpus
tested. It does that without the practice answer key in its code, without claims it never computed,
and without guessing tenant names.

---

## 2. How the approaches were tested

| Corpus | How it was made | What it tests |
|---|---|---|
| practice A (full) | shipped kit | baseline |
| practice A 5% sample | shipped kit | behaviour on thin data |
| sealed-style alpha, bravo, charlie, delta | `generate.py --sealed <phrase>`, the organisers' own generator | faults and decoys on new days and with new lengths |
| alpha, tenants renamed | `acme-bank → zenith-credit`, `northwind-retail → orbit-mart` in a copy | the PS says day 6 changes **tenants**; the public generator does not |
| holdout echo, foxtrot | generated **after** the fixes were written, never inspected | guards against fitting the fixes to the corpora used for debugging |

### Scores (machine points, out of 55)

| Corpus | Fault onsets (F1 · F2 · F3) | Ours before | Teammate | **Ours after** |
|---|---|---|---|---|
| practice A | 34 · 40 · 46 | 54.50¹ | 55.00 | **55.00** |
| practice 5% sample | same | 43.60 (F3 missed, D3 false alarm) | 47.70 (F3 missed, stray) | 41.67 (F2, F3 below noise gate; no false alarm) |
| alpha | 7 · 14 · 22 | 47.83 (F3 missed) | 55.00 | **55.00** |
| bravo | 23 · 5 · 6 | 41.17 (F2 + F3 missed) | 55.00 | **55.00** |
| charlie | 9 · 31 · 26 | 47.83 (F3 missed) | 55.00 | **55.00** |
| delta | 8 · 28 · 23 | 41.17 (F2 + F3 missed) | 55.00 | **55.00** |
| alpha, renamed tenants | 7 · 14 · 22 | 47.83 | 55.00² | **55.00** |
| holdout echo | 26 · 30 · 9 | — | 55.00 | **55.00** |
| holdout foxtrot | 7 · 11 · 23 | — | 55.00 | **55.00** |

¹ Only with schema validation bypassed. Unmodified, the pipeline stops at `report_builder.py:35`
before writing any file, which would score 0.
² It still scores, but it published metrics for tenants that do not exist in the data (§4.2).

All outputs pass the kit schema. The sample isn't what gets scored. After the fixes, F2 there sits at
z = 3.98 on 20 empty responses, just under the z ≥ 4 gate. The gate was deliberately not lowered to
fit it, because the PS weighs a false alarm above a miss.

---

## 3. What was wrong in `nexus_loop`

### 3.1 Fatal: no report on this machine
`report_builder.validate_report` imported `jsonschema`, which isn't installed here, and it ran
*before* the file was written. `CLAUDE.md` §9 warns about exactly this. A sealed run would have
produced nothing.

### 3.2 Fatal: recovery-edge changepoint bug (F3 missed on 5/5 unseen corpora, F2 on 2/5)
`scan_turn_inflation` and `scan_silent_tool_failures` used `criterion="abs_delta"`, which picks the
biggest jump in *either* direction. A fault that ends inside the log has an onset and a recovery of
almost identical size. When the recovery won, the scanner saw turns *falling* (ratio 0.6 < 1.3) or
empty payloads falling (negative delta) and discarded a real fault.

Traced on bravo: F3 truth days 6–19; the changepoint was picked on **day 20**, mean turns
7.07 → 4.41. F2 truth days 5–19; the changepoint was picked on **day 20**, empty rate 0.172 → 0.000.
The practice corpus hid this bug: its F3 runs to the last day, and its F2 recovers too close to the
end for a full window. `rise_ratio` alone would not have fixed F2, because the empty-payload
baseline is exactly 0, which ratio criteria skip.

### 3.3 Smaller issues found in the review
- No noise gate. On the 5% sample, turn inflation on an agent with a handful of sessions a day became
  a false alarm, counted against D3 (−4.2 specificity).
- F1's evidence quoted a 12-day probe window (0.393, 280 sessions) while its impact quoted the
  finding window (0.309, 230 sessions): two different numbers for one claim.
- The empty-payload test read a *missing* `result_field_count` as zero.
- No prescriptions, so loop completeness was stuck at 7.5/8 and the approval screen had nothing to act on.
- No refusal of the `customer_ref` breakdown (trap C2).
- Calibration had no warning for tiny label sets (1.00 on the sample, from 6 labels).
- Metric IDs used `tenant.split("-")[0]`, so two tenants sharing a prefix would collide.
- Standard `best` was equal to `median`, both computed over all traffic including broken windows.

---

## 4. What is wrong in `build_report.py`

It detects well on this generator. That is because its rules lean on things the generator happens to
keep constant, and it fills gaps with the practice answers.

### 4.1 Practice answer-key numbers as fallbacks

| Line | Literal | Practice ground truth |
|---|---|---|
| 490–491 | `or 0.852`, `or 0.775` | F2 order_status resolution 0.8522 → 0.7755 |
| 558–559 | `or 0.789`, `or 0.803` | F3 resolution 0.7893 → 0.8031 |
| 563–564 | `0.043`, `0.079` | F3 cost per session 0.04378 → 0.07994 |
| 296–297 | `3.88`, `3.28` | D3 mean quality 3.881 → 3.283 |
| 336–337 | `1350`, `3100` | D2 tool p95 1373 → 3157 ms |
| 437 | `"0.11-0.38"` | F1 "kb_top_score sits in the 0.11–0.38 band" |
| 484 | `"order_status"` | F2 cohort intent |
| 671–672, 1053, 1245 | `"acme-bank", 0.72` · `"northwind-retail", 0.93` | C1 true coverage per tenant |
| 420 · 477 · 550 | duration clamps 10–14 · 11–14 · 9–14 | generator's fault-length ranges |

`x or 0.852` also replaces a genuine rate of `0.0`. The PS forbids "hard-coded practice answers" and
requires "every figure is computed from the data".

### 4.2 Metrics for tenants that don't exist
On the renamed corpus, the report still published "Tool failure rate — northwind-retail" (coverage
0.93) and "— acme-bank" (0.72), for tenants absent from the data. `score.py` awarded +3 because the
literals equal the true coverage. A judge reading the screen would see fabricated metrics.

### 4.3 Evidence stated but never computed
- `:697` "typical confident range of 0.62–0.96"
- `:729` "no tool is involved in this intent"
- `:847`, `:876` "transcripts demonstrate repetitive confirmation phrasing". `turns.jsonl.gz` is never opened.
- `:875` "peer agents on the same tenant show normal turn counts"
- `:914` "per-intent resolution rates… flat within standard error"
- `:950`, `:968` "judged quality remained stable", "no config change coincided"
- `:992` "identical effect size" across tenants

### 4.4 Logic that only works on this generator
- **F2 cohort intent** is the tenant's most common intent, not the intent calling the tool. It's right
  only because `order_status` is northwind's largest intent (2,489 vs 1,239 sessions).
- **F2 onset** is the first tool, in file order, with ≥ 5 days containing any empty 200. There is no
  baseline comparison.
- **F3 baseline** is days 0–9. It breaks if a fault starts in that range, and fired on day 2 on the sample.
- **Decoy dismissals don't check the data.** Load is dismissed without checking resolution, and the
  judge boundary is taken from the config row without checking that scores moved. A real regression
  during a traffic spike would be dismissed.
- **`break` after the first match** in every scanner: only one instance of anything can ever be reported.
- **Honesty defects:** an A09 metric declared `judged` with `calibration: null` and a judge that
  doesn't exist; the quality calibration is labelled `v1` but computed over v1 and v2; A03 is refused
  as `COVERAGE_TOO_LOW` although asks.md says it is measurable; `revert` is used for the prompt fix.
- **Silent failure:** a missing config CSV continues with 0 events and no error.

### 4.5 What it did better (and is now in `nexus_loop`)
Prescriptions with decision blocks and predicted deltas; using the correlated config day as onset;
retry counts and the KB score range as evidence. These are ported, but rebuilt from computed values.

---

## 5. Stage by stage: how each reaches its conclusion (after fixes)

| Stage | `nexus_loop` (after) | `build_report.py` |
|---|---|---|
| **F1 KB gap** | Intent first seen after the first 10% of the log; resolution vs the tenant's mature intents **over the same window** (drop ≥ 0.15, z ≥ 4); `kb.gap` when kb_hit < 0.35; KB change ±6 days attributed. Baseline = peer traffic (no before-period). | Any intent with ≥ 5 days at kb_hit < 0.25; onset from a KB note containing "launch"/"pack"; duration clamped. |
| **F2 silent tool** | Per (tenant, tool): **rise** in empty-`ok` share (+0.05, z ≥ 4); declared error rate flat ±0.02; intent = the one issuing the calls; `kind='tool'` + target attribution. Baseline = own past. | First tool with ≥ 5 days of any empty 200; tenant's biggest intent. |
| **F3 prompt regression** | Per (tenant, agent): **rise** in mean turns (×1.3, Welch z ≥ 4); resolution flat ±0.06; prompt change on that agent (`model.change` if only a model change fits). Baseline = own 7 days before. | Days 0–9 baseline + 1.4 turns; resolution never checked. |
| **D1 mix shift** | Share rise/fall ≥ 0.15; own rate stable ±0.10; **every other intent's move measured** and quoted; nearby quality-relevant config changes listed. | ≥ 2.3× share; "per-intent flat" asserted. |
| **D2 load** | Volume ×1.9; dismissed **only if** resolution stays within ±0.06; p95 latency, error rate, within-version quality quoted. | ≥ 2.1× volume; dismissed unconditionally. |
| **D3 judge boundary** | **Fall** ≥ 0.35 overall and in **every tenant**; resolution flat; `tenant='*'` judge row or a `judge_version` change required. | First judge / `*` config row; nothing checked. |
| **Metrics** | Per tenant (IDs derived from tenant names), values in `result`, calibration per judge version with warnings, A04 measured first, A03 answered. | Tenant names hard-coded; uncalibrated judged A09. |
| **Gaps** | From `catalog.capabilities` (A11 with failover event; A09 + its measured half) and C2 refusals from `cardinality_budgets` vs real distinct counts. | A11, A09, and an incorrect A03 refusal. |
| **Prescriptions** | `kb.add` / `tool.validate` / `prompt.edit`, L1; decision text from numbers; `replay_request` with a golden set of human-labelled resolved sessions. | Three hand-worded prescriptions, `revert` for F3. |

---

## 6. PS rules checklist (final code)

| Requirement | `nexus_loop` after fixes |
|---|---|
| Rule 1: no model asked what the logs record | ✅ zero model or network calls |
| Rule 2: definitions, judge versions, golden set untouched | ✅ fixes target KB, tool integration, prompt only |
| Every judged number versioned and calibrated, not ≥ 0.995 | ✅ 0.9479 (n = 96), per version v1 0.917 / v2 0.979; warns on small n |
| Fidelity + coverage on every metric; v2 excluded from tool/KB/cost | ✅ coverage 0.7205 / 0.9308, measured from data |
| A01, A04, A09, A11 answered or refused; A04 measured listed first | ✅ |
| `tenant='*'` join and `kind='tool'` filter | ✅ |
| Step measures re-aggregated before session averages | ✅ step cubes per day, sessions from session rows |
| Breakdown over budget refused, budget cited (C2) | ✅ `customer_ref`: 80,252 distinct vs budget 200 |
| Regressions carry impact + derivation naming the baseline, audience, cost of inaction | ✅ |
| Decoys dismissed with `not_a_regression_because` + matching cause class | ✅ |
| Nothing hard-coded to variant A | ✅ grep for tenant, intent, tool names and fault days: none |
| Every figure computed, nothing asserted | ✅ |
| One command, unattended, deterministic, fails loudly | ✅ exit 0 / 2 / 1 |
| Replay not called by the system | ✅ `replay_request` only |

---

## 7. Fixes applied to `nexus_loop/`

| File | Change |
|---|---|
| `schema_check.py` *(new)* | Stdlib draft-07-subset validator. |
| `report_builder.py` | Report is written first, then validated; referential link checks; prescriptions, verifications, self-assessment. |
| `pipeline.py` | Resolves the schema path from any working directory; exit codes 0 / 2 / 1; `--decisions` and `--verifications`; system notes on what is real, stubbed and breaks at 100×. |
| `changepoint.py` | `rise` / `fall` signed criteria; pooled two-proportion z and Welch z; window helpers. |
| `detector.py` | Signed criteria for F2, F3 and D3; z ≥ 4 on F1, F2 and F3; per-tenant check for D3; every other intent's move for D1; computed evidence (retries, KB score range, error rates, versions); sorted iteration for determinism. |
| `diagnoser.py` | Config-row evidence lines; `signals` block per diagnosis; F1 recomputed over one window; baselines named in every derivation; D3 not dismissed without rubric evidence. |
| `ingest.py` | Catalog-faithful empty-payload test; KB score min/max. |
| `metrics.py` | Tenant-derived IDs; `result` values; A03 cost per resolved by intent; calibration per judge version with warnings; breakdown budget guard. |
| `gap_analyzer.py` | C2 cardinality refusals from the catalog; measured half of A09. |
| `standard.py` | Top-decile daily rate as `best`; regression windows excluded; per-tenant golden-set version. |
| `prescriber.py` *(new)* | Decision-framed prescriptions and `replay_request` with a golden set from `labels/outcome_labels.jsonl`. |
| `loop_state.py` *(new)* | Merges recorded approvals and replay outcomes; rejects stale or invalid entries; hit rate, prediction error, de-weighting after ≥ 3 cycles. |

---

## 8. What the team still has to do

1. **Replay (40-run budget).** Check the request bodies with
   `python -m nexus_loop.replay_client --report <report> --team <team>` (dry run, sends nothing). Send one
   prescription at a time with `--prescription pN --send --url <server>`; it refuses exact repeats, keeps a
   ledger, and writes `verifications.json`. Then re-run the pipeline with `--verifications`.
2. **The approval screen.** Show findings, evidence and the prescription's `decision` block, and call
   `python -m nexus_loop.decide` (or write `prescriptions[].approval` + `decisions.json` the same way).
3. **Before the 12:00 freeze.** Run `python scripts/sealed_suite.py` and `python -m unittest discover -s tests`
   after any change to `nexus_loop/`.

---

## 9. Known limits

- The approve/reject screen itself is not built yet (its back end, `nexus_loop.decide`, is). A02, A05,
  A07 and A10 are answered in `nexus_loop/answers.py`; see `DETECTION_LOGIC.md` for how.
- C2 refusals use `ask_id: "*"` because no ask requests that breakdown; the field is named in `breakdown`.
- F2 and F3 are not detected on the 5% sample (z just under 4). Full-size corpora clear the gate by
  a wide margin (practice: z = 18.0, 14.7, 18.5).
- At 100× scale, sessions held in memory and raw per-day latency lists would need streaming
  aggregates and a quantile sketch.

## Reproduce

```bash
python -m nexus_loop.pipeline --kit kit --out my-loop-report.json
python tools/nexus-loop-kit/score.py --report my-loop-report.json --ground-truth kit/ground_truth/ground_truth.json

# an unseen layout
python tools/nexus-loop-kit/generate.py --sealed "any phrase you like" --out ../kit-test --dev-sample 0
python -m nexus_loop.pipeline --kit ../kit-test --out test-report.json
python tools/nexus-loop-kit/score.py --report test-report.json --ground-truth ../kit-test/ground_truth_SEALED/ground_truth.json
```
