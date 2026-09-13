# NEXUS LOOP — Approach

## Goal

From eight weeks of agent logs we have never seen, produce a `loop-report.json`, valid against
`tools/nexus-loop-kit/schema/loop-report.schema.json`, that:

- states metrics with honest fidelity, coverage and calibration;
- finds the real regressions, names the slice of traffic, quantifies the impact and attributes each to
  a cause and, where the evidence supports it, a specific config change;
- examines the lookalikes and dismisses them in writing;
- refuses what cannot be measured and specifies the event that would make it measurable;
- proposes a fix per regression as a decision a human approves or rejects, and records human
  decisions and replay outcomes when the team supplies them.

It must do this **unattended on the sealed day-6 corpus**, where faults move to different days,
lengths and tenants.

## How to run

```bash
# authoritative run (writes the report, then validates it; exit 0 = valid)
python -m nexus_loop.pipeline --kit kit --out my-loop-report.json

# fast iteration on the 5% sample
python -m nexus_loop.pipeline --kit kit --corpus-subdir corpus_sample --out my-loop-report.sample.json

# sealed run
python -m nexus_loop.pipeline --kit <sealed-kit-dir> --team <team> --out loop-report.json

# after the team has decided on fixes and replayed them by hand
python -m nexus_loop.pipeline --kit kit --out my-loop-report.json \
       --decisions decisions.json --verifications verifications.json

# self-score (practice only; ground truth is never an input to the pipeline)
python tools/nexus-loop-kit/score.py --report my-loop-report.json --ground-truth kit/ground_truth/ground_truth.json

# approval gate back end: list prescriptions, record a decision (writes the report + decisions.json)
python -m nexus_loop.decide --report my-loop-report.json --list
python -m nexus_loop.decide --report my-loop-report.json --prescription p1 --verdict accepted --by "Ops lead" --reason "…"

# replay: dry run prints every request body; --send posts ONE prescription and records it
python -m nexus_loop.replay_client --report my-loop-report.json --team <team>
python -m nexus_loop.replay_client --report my-loop-report.json --team <team> --prescription p1 --send --url http://127.0.0.1:8719

# pre-freeze check on unseen layouts (generates corpora outside the repo, scores with score.py)
python scripts/sealed_suite.py

# tests: algorithms, rule guards (no corpus names / answer key / model / network in the pipeline), determinism
python -m unittest discover -s tests -v
```

Stdlib only (Python 3.9+). No language model, no network call, and the replay service is never
contacted by the system. Exit codes: `0` written and valid, `2` written but failed validation (the
problems are printed), `1` no report could be produced.

`decisions.json` (written by the approval screen or by hand):

```json
{"p1": {"verdict": "accepted", "decided_by": "ops lead", "reason": "…", "at": "2026-09-18T11:00:00Z",
        "signature": "kb.gap|acme-bank|{\"intent\": \"…\"}|34"}}
```

`verifications.json` (the replay responses the team recorded):

```json
[{"prescription_id": "p1", "replay_run_id": "rp_…", "verdict": "improved",
  "metric": "resolution_rate", "before": 0.31, "after": 0.83, "golden_set_pass": true}]
```

An entry is merged only if its prescription exists in this run and, when it carries a `signature`, the
signature matches the same diagnosis. A verification is merged only with a real `rp_` run id. Anything
else is rejected with a printed reason, so a stale decision can never attach to a different finding.

## The two rules, and how the code keeps them

**Rule 1: never ask a model what the logs record.** The package makes zero model calls. Every fact
(errors, empty payloads, turns, handoffs, costs, versions) is counted from `sessions` and `agent_steps`.
`quality_score` is the corpus's own judged signal; we calibrate it against `labels/rubric_scores.jsonl`
and never re-judge. We built no new judge, so A09's *reason* is a `REQUIRES_NEW_JUDGE` gap, with only
its measured half (abandonment counts) answered.

**Rule 2: change the agent, never the yardstick.** Prescriptions target KB content, tool integrations
and prompts only. `predicted_delta` is the finding's observed value returning to the baseline the
finding already names. The golden set is derived once and deterministically from the human outcome
labels, and nothing edits it.

## Pipeline

```
ingest -> cohorts -> detector -> diagnoser -> gap_analyzer -> metrics -> prescriber -> standard -> loop_state -> report_builder
(load)   (index)    (find)      (explain)    (refuse)        (measure)  (propose)    (baseline)  (record)     (assemble, validate)
```

| Module | Job |
|---|---|
| `ingest.py` | Loads sessions and the config timeline. One streaming pass reduces ~880k steps to per-day cubes: tool calls/ok/err/empty/retry per tool and per intent, KB lookups/hits/score range per intent, tool latency per tenant. `is_silent_empty` follows the catalog: `outcome='ok'` with `result_field_count = 0`, using `response_bytes <= 2` only when the field is absent (a missing value is never read as zero). |
| `cohorts.py` | Day-bucketed indices by tenant+intent, tenant+agent, tenant, all; `Rollup` for rates, handoffs, turns, v3-only cost, quality by judge version. |
| `changepoint.py` | Sliding before/after window search with criteria `rise`, `fall`, `rise_ratio`, `fall_ratio`, `abs_delta`; recovery search; pooled two-proportion z and Welch z from running sums. |
| `detector.py` | Six scanners that each look at one signal and emit candidates with computed evidence. None of them decides "regression or lookalike". |
| `diagnoser.py` | Turns candidates into findings and diagnoses: cause class, config attribution (tenant or `*`, kind, target), impact with a checkable derivation naming the baseline, audience, cost of inaction, and a `signals` block of the key numbers for the screen. |
| `gap_analyzer.py` | Reads gaps from `catalog.capabilities` (A11 `NOT_MEASURABLE` with the failover event; A09 `REQUIRES_NEW_JUDGE` plus its measured part). Audits every catalog cardinality budget against the real distinct-value count and emits `CARDINALITY_REFUSED` gaps (trap C2). |
| `metrics.py` | Per-tenant metrics (IDs derived from the tenant name) with a `result` block of values: containment (A01); stratified resolution trend and judged quality with per-version calibration (A02); tool failure then silent-empty (A04, measured first); KB hit (A06); spend (A08); cost per resolved by intent (A03). Breakdowns over budget are removed and cited. |
| `prescriber.py` | One L1 prescription per diagnosed regression: `kb.add`, `tool.validate` or `prompt.edit` (an edit, not a blunt `revert`). Decision text is formatted from the numbers. Each carries a `replay_request` for a person to send, with a golden set of human-labelled resolved conversations outside every regression. |
| `standard.py` | Per tenant+intent over v3 traffic, excluding sessions inside reported regressions: `best` = top-decile daily resolution rate, `median` = median day, `deficit`; the same for turns to resolve (bottom decile = best). |
| `loop_state.py` | Merges recorded decisions and replay outcomes; computes `prediction_error`, per-change-type hit rate, and de-weights a change type only after ≥ 3 verifications below 50%. |
| `schema_check.py` | Stdlib validator for the draft-07 subset the schema uses. `report_builder.py` also checks finding → diagnosis → prescription → verification links. |
| `answers.py` | A02 month over month raw and at a fixed intent mix; A05 milestone funnels (the journey the ask names is matched from asks.md words at runtime); A07 before/after on each model-changed agent at a fixed intent mix, refusing quality across a judge-version change; A10 review queue (deterministic scope ranked by judged quality percentile within its version). |
| `decide.py` | Approval-gate back end: records approve/reject/defer with a required reason into the report and `decisions.json`. |
| `replay_client.py` | Prints each prescription's replay request (default). With `--send`, posts one request, refuses exact repeats, counts against the 40-run budget, and records the result for `--verifications`. The only module that can open a connection; the pipeline never imports it. |

## The six scanners

| Scanner | Signal | Gates | Becomes |
|---|---|---|---|
| `scan_new_cohorts` | An intent first seen after the first 10% of the log, resolving far below the tenant's mature intents over the same days | drop ≥ 0.15, z ≥ 4, ≥ 30 sessions | `kb.gap` when kb_hit < 0.35 (KB change ±6 days attributed); baseline = **peer traffic**, because the cohort has no before-period |
| `scan_silent_tool_failures` | Rise in empty-`ok` share of ok calls per (tenant, tool) | `rise` criterion, +0.05, z ≥ 4, declared error rate flat within ±0.02 | `tool.contract_break`; cohort intent = the intent that actually makes calls to the tool; baseline = the cohort's **own past** |
| `scan_turn_inflation` | Rise in mean turns per (tenant, agent) | `rise` criterion, ×1.3, Welch z ≥ 4, resolution flat within ±0.06 | `prompt.regression` with the prompt change on that agent (`model.change` if only a model change explains it) |
| `scan_traffic_mix` | One intent's share of tenant traffic spikes or dips | share move ≥ 0.15, the intent's own rate stable within ±0.10; every other intent's move is reported | dismissed as `traffic_mix`, with nearby quality-relevant config changes listed |
| `scan_load_events` | Tenant volume spike | ×1.9, resolution flat within ±0.06 (otherwise never dismissed) | dismissed as `load`, with p95 latency, declared error rate and within-version quality |
| `scan_judge_boundary` | Global quality drop | `fall` criterion, ≥ 0.35 overall and ≥ 0.175 in **every** tenant, resolution flat, judge row (`tenant='*'`) or a `judge_version` change present | dismissed as `judge_change`; quality is only trended within one version |

Effect-size gates are in the metric's own units. The z ≥ 4 gate is the noise guard; it is what stops
a thin cohort from producing a false alarm.

## Design decisions that came from testing, not from the answer key

1. **Signed changepoints for anything that can recover.** The first version used `abs_delta` for
   turns and empty payloads. A fault that ends inside the log has an onset and a recovery of nearly
   equal size, and when the recovery won, the scanner saw the metric falling and discarded a real
   fault. That missed F3 on every unseen layout tested and F2 on two of four. The practice corpus hid
   this because its F3 runs to the last day.
2. **A noise gate on every rate.** On the 5% sample the old code flagged turn inflation on an agent
   with a handful of sessions a day. That false alarm was caught as a D3 match and cost 4.2 points.
3. **Write the report first, validate second, with no third-party validator.** The first version
   imported `jsonschema`, which is not installed on the team machine, and crashed before writing
   anything — a zero on day 6.
4. **Every sentence is computed.** Dismissals quote the measured per-intent moves, per-tenant quality
   drops and judge versions; a dismissal whose checks fail is not emitted.
5. **One window per finding.** Evidence, `observed`/`expected` and impact are all recomputed over the
   finding's final window, after the config-day adjustment.

## Tested offline on unseen layouts

`python tools/nexus-loop-kit/generate.py --sealed "<any phrase>" --out <dir> --dev-sample 0` builds a
corpus with every fault and decoy moved to new days and lengths, plus its own answer key (≈ 20 s, no
replay budget used). The public generator keeps tenant names fixed, and the PS says day 6 changes
tenants, so we also renamed the tenants in one corpus. Scored only through `score.py`:

| Corpus | Before fixes | After fixes |
|---|---|---|
| practice A | 54.5 (crashes without the schema bypass) | **55.0** |
| sealed-style alpha / bravo / charlie / delta | 47.8 / 41.2 / 47.8 / 41.2 | **55.0 each** |
| alpha with tenants renamed | 47.8 | **55.0** |
| two fresh holdouts generated after the fixes were written | — | **55.0 each** |
| practice 5% sample | 43.6 (false alarm) | 41.7 (F2/F3 below the noise gate, no false alarm) |

The sample is not what gets scored. F2 there sits at z = 3.98 on 20 empty responses; the gate stays at
4 rather than being tuned to it, because the PS weighs a false alarm above a miss.

## Taken from the teammate's `build_report.py`, and what was not

**Adopted, rebuilt from computed values:** mining a `standard`; preferring a correlated config day as
the onset; prescriptions with decision blocks and predicted deltas; computed retry counts and the KB
score range as evidence; the measured half of A09.

**Not adopted:** fallback literals copied from the practice answer key; tool metrics hard-coded to
tenant names; evidence sentences not computed from data (e.g. transcripts claimed as read); decoy
dismissals without checks; a judged A09 metric with no judge or calibration; refusing A03, which is
answerable; `revert` as the prompt fix; duration clamps and `break` after the first match.

## Honest limits

- **Screen.** The approve/reject screen is not built yet; its back end (`nexus_loop.decide`) is. The
  screen should call it, or write `prescriptions[].approval` and `decisions.json` the same way.
- **Replay is human-triggered by design.** No `verifications` exist until the team sends requests with
  `nexus_loop.replay_client --send` (or by hand) and re-runs with `--verifications`. The response parser
  reads `run_id`, `verdict`, `before`/`after` and `golden_set` defensively; anything it cannot read is
  flagged for a manual fill, with the raw response kept in `replay_ledger.json`.
- **A05 journey match.** The journey the ask names is picked by shared words between asks.md and intent
  names; every journey's funnel is published either way, so a mismatch loses emphasis, not data.
- **C2 `ask_id`.** The cardinality refusals use `ask_id: "*"` because no ask requests that breakdown;
  the refused field is named in `breakdown`.
- **Calibration** uses a ±1 point tolerance on the 1–5 scale and publishes n and per-version agreement.
  It warns when n < 30 or agreement ≥ 0.995 (as happens on the 5% sample, where only 6 labels match).
- **At 100× scale.** Sessions are held in memory; they would need the same per-day streaming
  aggregation the step cubes use, and per-day latency lists would need a quantile sketch.
