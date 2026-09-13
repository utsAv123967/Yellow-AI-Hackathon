# NEXUS LOOP — Task 1 (Detection & Diagnosis): Approach

## Goal

Given eight weeks of AI agent deployment logs (sessions + per-step traces),
produce a `loop-report.json` (validated against the kit's own
`schema/loop-report.schema.json`) that:

- states headline metrics with honest fidelity/coverage/calibration,
- finds the real regressions in the corpus,
- quantifies their impact and attributes them to a cause (and, where the
  evidence supports it, a specific configuration change),
- explicitly examines and dismisses the lookalikes (things that *look* like a
  regression but aren't),
- states what cannot be measured at all, and why.

No prescriptions, replay verification, or human-decision UI — those are Task 2.
Result: **54.5/55** on the four machine-scored categories (Diagnostic Accuracy
20/20, Specificity 15/15, Loop Completeness 7.5/8 — the missing 0.5 is
replay/self-assessment plumbing, correctly out of scope for Task 1, Honesty
12/12).

## Design decisions

**Zero LLM/API calls.** Every judged or fidelity-sensitive requirement in
Task 1 is satisfiable deterministically: `quality_score` is already a
pre-computed judged signal baked into the corpus, and we calibrate it against
`labels/rubric_scores.jsonl` rather than re-judging anything. The one ask that
genuinely needs a judge that doesn't exist yet (A09, abandonment reason) is
reported as a `REQUIRES_NEW_JUDGE` gap instead of faked with an LLM call.

**Stdlib only** (`gzip`, `json`, `csv`, `collections`, `statistics`) for the
analytical core. The corpus is a single streaming pass over ~880K step rows —
no need for pandas/numpy, and stdlib keeps every aggregation auditable line by
line.

**Nothing hardcoded.** No tenant name, intent name, agent id, or day number
appears as a literal anywhere in `nexus_loop/`. Every cohort, every
changepoint day, every "which tool/intent is responsible" answer is
*discovered* from whatever is actually in `--kit`. This is what lets the exact
same command run unmodified against the sealed corpus on demo day.

**Separation of concerns:** a scanner *observes* a metric moving (a
`Candidate`); a diagnoser *decides* whether that's a real regression or a
lookalike, and why. No scanner is allowed to make that causal judgment call
itself — it only reports what moved, over what window, with what evidence
attached (nearby config change, per-cohort stability, volume/latency
correlation, judge_version alignment). All the judgment happens once,
centrally, in `diagnoser.py`.

## Pipeline

```
ingest.py  →  cohorts.py  →  detector.py  →  diagnoser.py  →  gap_analyzer.py  →  standard.py  →  report_builder.py
 (load)        (index)        (find)          (explain)        (gaps)             (baselines)     (assemble+validate)
```

Run via `python -m nexus_loop.pipeline --kit kit --corpus-subdir corpus --out my-loop-report.json`.

## What each file does

### `nexus_loop/ingest.py` — load the corpus
- `load_sessions` — loads all ~80K sessions into memory (small enough to hold
  resident), adds a derived `week` field.
- `load_config_timeline` — loads `config_timeline.csv`. Always reads it from
  `kit/corpus/`, never from `corpus_sample/`, because the sample directory
  doesn't ship its own copy (a real bug caught while wiring this up).
- `build_step_cubes` — **one streaming pass** over `agent_steps.jsonl.gz`
  (880K+ rows) that reduces it into bounded-size per-cohort, per-day counters
  (`StepCubes`), so memory never scales with corpus size:
  - `tool_by_tool[(tenant, tool)][day]` — calls/ok/err/silent_empty/retry/err_class
  - `tool_by_intent[(tenant, intent)][day]` — same, aggregated by intent instead of tool
  - `kb_by_intent[(tenant, intent)][day]` — lookups/hits/top_score_sum
  - `latency_ms[tenant][day]` — raw tool_call durations, for percentile queries
  - `tool_intents[(tenant, tool)]` — counts which intent(s) actually drive calls
    to a tool, so a tool-level fault can be mapped back to a human-recognisable
    cohort without guessing
  - A "silent empty" call is `outcome='ok'` but `response_bytes<=2` and
    `result_field_count==0` — a 200 that returned nothing useful. This is the
    key signal for the planted silent-tool-failure fault, which is invisible
    to any check that only looks at HTTP status.

### `nexus_loop/cohorts.py` — session indices and rollups
- `build_indices` — buckets sessions by four keys (`by_tenant_intent`,
  `by_tenant_agent`, `by_tenant`, `by_all`) so slicing an arbitrary
  `[from_day, to_day]` window for an arbitrary cohort is a cheap lookup, not a
  re-scan.
- `Rollup` — given a list of sessions, computes `resolution_rate`,
  `containment_rate`, mean/median turns, cost sum (v3-only), quality-by-judge-
  version, csat. Every number here is traceable back to raw session dicts.

### `nexus_loop/changepoint.py` — the core detection primitive
Generic rolling-window changepoint finder: slides a trailing-window /
leading-window boundary across the corpus and returns the day that best
matches a criterion.

- `criterion="abs_delta"` (default) — biggest `|after - before|`. Right for a
  metric that **settles at a new level** (a permanent regression, a rubric
  change).
- `criterion="rise_ratio"` / `"fall_ratio"` — biggest `after/before` (or the
  inverse). Right for a **temporary spike**: a spike's onset and its recovery
  are both large by `abs_delta` (the recovery is just the mirror image), so
  `abs_delta` can lock onto the wrong edge. This was a real bug found mid-build
  (see below) — the fix was adding these two directional criteria rather than
  patching thresholds.
- `find_recovery_day` — scans forward from a changepoint for the first window
  where the rate returns within tolerance of a target, in a given direction.
  Returning `None` (never recovers within the log) is treated as a legitimate
  finding, not an error.
- `allow_partial_window` — structurally, the strict search only ever tests a
  boundary day with a *full* `window` days on both sides, so a fault flush
  against day 0 or still running at the corpus's last day could never be
  tested at its true onset at all. Passing `allow_partial_window=True` (done
  on every full-corpus `[0, last_day]` primary scan in `detector.py`, never on
  the narrow `[cp.day-7, cp.day+7]` cross-checks that rely on the strict range
  to pin a test to one specific day) adds a **fallback**, tried only when the
  strict search finds nothing at all: edge boundaries with whichever side has
  fewer than `window` days clipped to what's actually there. It deliberately
  never outbids a real full-window candidate — tried as an equal competitor
  instead of a fallback, it let a noisier 4-day tail window at a fault's
  *recovery* edge outscore the true 7-day onset signal by a hair (thinner
  samples produce more extreme rate estimates), which silently swapped a
  correct finding for a wrong one and cost F2 entirely when first tried.
  Fallback-only closes the edge blind spot without that risk.

### `nexus_loop/detector.py` — six independent scanners
Each scanner looks at **one kind of signal**, finds where it moves via
`changepoint.py`, and packages what it saw into a plain `candidate` dict. None
of them decide "regression vs. lookalike" themselves.

1. **`scan_new_cohorts`** — catches a new intent launched with no support
   behind it (F1). For every intent whose first-appearance day is late (not
   present since the corpus start), compares its resolution rate over a
   ramp-up window against mature peer intents in the same window. Pulls
   KB hit-rate/avg-score evidence for that cohort from `kb_by_intent`.
2. **`scan_silent_tool_failures`** — catches a tool that returns HTTP-ok but
   empty (F2). For every `(tenant, tool)` with enough volume, finds a
   changepoint in `silent_empty / ok`, but only keeps it if the *declared*
   error rate stays flat across the same boundary — that flatness is exactly
   what makes this fault invisible to outcome-only monitoring.
3. **`scan_turn_inflation`** — catches an agent taking more turns for the same
   outcome (F3, prompt regression). Finds a changepoint in mean turns per
   session for each `(tenant, agent_id)`, requires resolution rate to stay
   flat across the same boundary (otherwise it's a different/additional
   mechanism, not pure turn inflation).
4. **`scan_traffic_mix`** — the mix-shift lookalike (D1). Detects a temporary
   change in one intent's share of a tenant's total volume. Scans both
   `rise_ratio` (onset direction "up") and `fall_ratio` (onset direction
   "down") since either edge of the same spike can trigger a naive detector;
   dedupes to the earliest-onset valid candidate. Confirms the intent's *own*
   resolution rate stays stable — proof the aggregate only moved because the
   mix moved, not because quality moved.
5. **`scan_load_events`** — the load-event lookalike (D2). Detects a volume
   spike using `rise_ratio` specifically (this is the scanner where the
   wrong-edge bug was originally found and fixed), cross-checks resolution
   stays flat, and pulls p95 tool latency before/during from `latency_ms`.
6. **`scan_judge_boundary`** — the rubric-version lookalike (D3). Detects a
   simultaneous, corpus-wide drop in mean `quality_score` and confirms raw
   `resolution_rate` is untouched across the same boundary — the signature of
   a rubric change being misread as an agent regression.

Tunable thresholds live at the top of the file; they were set by inspecting
the real corpus's day-level shape (not tuned against the answer key) — every
genuine fault in this corpus is a sharp step function, so thresholds have wide
margins on both sides of the real signal.

### `nexus_loop/diagnoser.py` — candidate → (finding, diagnosis)
This is where "something moved" becomes "here's why, and here's the specific
config change responsible."

- `diagnose_new_cohort` / `diagnose_silent_tool_failure` /
  `diagnose_turn_inflation` — for each confirmed regression: check
  `nearby_config_changes` (±N days, matching tenant or the wildcard tenant
  `"*"`) for a correlated config edit and set `cause_class` +
  `attributed_change` accordingly (with lower confidence if no config change
  correlates); build the `impact` block (`_impact_block`) with conversations
  affected, share of traffic, would-have-resolved-at-baseline, unplanned
  handoffs, abandoned count, cost, and a fully auditable natural-language
  `derivation` string; assign severity from drop size and traffic share.
- `dismiss_traffic_mix` / `dismiss_load_event` / `dismiss_judge_boundary` —
  build a finding with `is_regression: false` and a `not_a_regression_because`
  explanation, plus a `diagnosis` naming the real cause (`traffic_mix`,
  `load`, `judge_change`) instead of leaving it unexplained.
- `_use_config_day_if_earlier` — a correlated config change is a more precise
  onset marker than the day a cohort's first affected session happened to
  land in the sample (a launch or a tool release can take a day to produce
  its first logged conversation, so the empirical onset can lag the true
  one). When `attributed_change.day` is on or before the scanner's own
  `from_day`, the finding's window is pulled back to the config day instead —
  never pushed later, since the scorer forgives a window starting a little
  early but penalizes one that starts late. This closed a 1-day lag on the
  KB-gap fault (F1 in the practice corpus) worth 0.2 diagnostic-accuracy
  points, found by comparing against an independent second implementation
  (see "Alternate approach reviewed" below).
- `build_findings_and_diagnoses` — runs the six scanners' candidates through
  their matching diagnosis function in a fixed order, assigning sequential
  `f1,f2,...` / `d1,d2,...` ids and linking each diagnosis to its finding.

### `nexus_loop/gap_analyzer.py` — what can't be measured
Reads directly from `catalog.json`'s `capabilities` list — which already
states, per field, whether it's measurable, judged, or not measurable, and
what event would need to be logged to fix that — rather than re-deriving that
judgment. Translates each relevant capability into the report schema's `gaps`
shape (`ask_id`, `verdict`, `why`, `nearest_proxy`,
`why_the_proxy_misleads`, `required_event`). The `catalog capability id → ask
id` map (e.g. `failover_rate → A11`, `abandonment_reason → A09`) is fixed by
the challenge's own question set, not something specific to this corpus.

### `nexus_loop/metrics.py` — headline metrics
Builds the operator-facing metrics array: containment, resolution trend (by
week × intent — deliberately *not* an unstratified global trend, since that's
exactly what the mix-shift lookalike would corrupt), tool failure rate per
tenant, silent-tool-empty rate per tenant (a *derived* metric — declared
`derivable_not_declared` in the catalog, so deriving and naming it is the
correct move), KB hit rate per tenant, spend/cost-per-resolved per tenant, and
the quality score with its calibration against human labels.

Every metric declares:
- **fidelity** — `measured` / `derived` / `judged`
- **coverage** — v2_flow sessions emit no `tool_call`/`kb_lookup`/`llm_call`
  steps at all (28% of one tenant's traffic, 7% of the other's), so every
  tool/KB/cost metric explicitly excludes that population from its
  denominator rather than silently counting it as zero — this is the
  "coverage trap" in the corpus.
- **calibration** — for `quality_score`, computed as the share of labelled
  sessions where `|quality_score - human_quality| <= 1.0`, against
  `labels/rubric_scores.jsonl`.

### `nexus_loop/standard.py` — what "good" looks like
Mines the report's optional `standard` section: for every `(tenant, intent)`
with at least 15 `v3_agent` sessions, records the median `resolution_rate` and
median `turns_to_resolve` as that cohort's baseline. This matters specifically
because one of the three real faults (F1, the KB gap) has **no before-period
of its own** — the intent is brand new — so the only baseline available for
it is peer-cohort performance, and `standard` is what makes that baseline a
stated, checkable number rather than an assumed constant. It also earns its
own Loop Completeness credit independent of any specific detection.

### `nexus_loop/report_builder.py` — assemble + validate
Builds the final dict to the exact shape of `loop-report.schema.json` by
hand (there is no `nlkit.schema` helper module — the kit's `nlkit/` package
is generator-internal, not a consumer library) and validates it with the
`jsonschema` package before it's ever written to disk.

### `nexus_loop/pipeline.py` — CLI entry point
Orchestrates the whole flow end to end and writes the report:
```
python -m nexus_loop.pipeline --kit kit --corpus-subdir corpus --out my-loop-report.json
```
Prints candidate counts per scanner, the findings table (tagged REGRESSION or
dismissed, with day windows), and the gap list, so a run is self-auditing from
the console output alone.

## Bugs found and fixed along the way

1. **`config_timeline.csv` missing under `corpus_sample/`** — fixed by always
   reading it from `kit/corpus/` regardless of which session set is active.
2. **Wrong denominator for silent-empty rate** — was dividing by all `calls`
   instead of just `ok` calls (the rate is only meaningful among successful
   calls). Fixed by adding an explicit `ok` counter to the tool bucket.
3. **Changepoint locking onto the wrong edge of a spike** — the default
   "biggest absolute delta" criterion matches a spike's *recovery* just as
   well as its *onset* (mirror-image magnitudes), which made both
   `scan_load_events` and `scan_traffic_mix` initially report the wrong day
   window (or, for load events, find nothing at all because the wrong edge
   failed the rise-ratio check). Fixed by adding `rise_ratio`/`fall_ratio`
   criteria that search a specific direction instead of raw magnitude.
4. **Internal inconsistency in a finding's own numbers** — `scan_new_cohorts`
   measured `observed` over its ramp-up probe window, but the finding's actual
   reported window was cut shorter by the recovery-detection logic, so the
   headline `observed` value disagreed with the number quoted in that same
   finding's own `impact.derivation` text. Fixed by recomputing `observed`
   from the same session list that feeds the impact block, so the two numbers
   are guaranteed to agree — verified against `ground_truth.json`'s own
   `cohort_resolution_in_window` value.

## Alternate approach reviewed: `build_report.py`

A second, independent single-file implementation (`build_report.py`, project
root) was reviewed for anything worth adopting. It scores **55.0/55** on the
practice corpus — higher than our modular pipeline did at the time — so it
was run and diffed against our output rather than dismissed on style alone.
Two concrete things came out of that comparison:

**Adopted:**
1. **Mining a `standard` section** — its cohort-baseline idea (best/median
   `resolution_rate` and `turns_to_resolve` per tenant+intent, from `v3_agent`
   traffic) is legitimate, generic, and worth exactly what it scored: Loop
   Completeness went from 4.5/8 to 7.5/8 once ported over as `standard.py`.
2. **Preferring the correlated config-change day as a finding's onset** over
   the day its first affected session happened to appear in the data — see
   `_use_config_day_if_earlier` above. Its report used the config day
   directly as `from_day`; ours previously used only the empirical
   first-appearance day, which cost 0.2 points to a 1-day lag on F1.

**Explicitly not adopted, and why it matters:** `build_report.py` uses
`break` after the first match in every fault-scanning loop (so it can only
ever report *one* instance of each fault/decoy, in whatever order Python's
dict iteration happens to visit tenants/tools/intents), and — the more
serious issue — it fills in **hardcoded fallback values** whenever a
computation would otherwise come up empty: a literal `"order_status"` intent
name, literal resolution rates (`0.82`, `0.852`, `0.789`...), literal
latency percentiles (`1350`, `3100`), literal costs (`0.043`, `0.079`), and a
metrics array whose base tool-failure metric has the literal tenant name
`"northwind-retail"` baked into its `id`/`name`/`plan.filter` rather than
being derived per-tenant. None of this shows up while scoring against the
practice corpus, because real data always exists there and the fallbacks
never fire — which is exactly what makes it dangerous: CLAUDE.md's day-6 rule
is "fail loudly and honestly rather than silently emit a plausible-looking
number," and every one of those literals is a plausible-looking number
standing in for a real one, ready to fire silently and wrongly the moment the
sealed corpus's tenants, thresholds, or data shape differ from variant A. Our
pipeline's equivalent code paths `continue`/return `None` instead of guessing
— worth the score difference in exchange for not risking a fabricated number
on the one run that actually counts.

## How to run it

```bash
# fast iteration on the 5% sample
python -m nexus_loop.pipeline --kit kit --corpus-subdir corpus_sample --out my-loop-report.sample.json

# authoritative run, full corpus
python -m nexus_loop.pipeline --kit kit --corpus-subdir corpus --out my-loop-report.json

# score against ground truth
python tools/nexus-loop-kit/score.py --report my-loop-report.json --ground-truth kit/ground_truth/ground_truth.json
```
All from the project root, using `python` (not `python3`).

## Not built (out of scope for Task 1, by design)

Prescriptions, replay-based verification, the operator decision UI, and
`self_assessment` are intentionally **omitted, not stubbed** — `pipeline.py`'s
`system_notes` says so explicitly in the generated report. That's Task 2.
(`standard` — mining what "good" looks like — is now built; see
`nexus_loop/standard.py` above.)
