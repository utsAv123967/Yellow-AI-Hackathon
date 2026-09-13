# How Nexus Loop Detects, Explains and Refuses

A complete walk-through of the logic behind `nexus_loop/`: every kind of issue the PS plants, the exact
algorithm that finds or dismisses it, the numbers it produces on the practice corpus, and how each part
of the report turns into points. Written so the team can explain the system in the demo and answer
"how does it know?" for any number on the screen.

Numbers quoted as *practice* come from running `python -m nexus_loop.pipeline --kit kit` on the practice
corpus (variant A). The system never reads the answer key; `score.py` alone does, after the fact.

---

## Contents

1. [The loop in one picture](#1-the-loop-in-one-picture)
2. [Ground rules the code is built on](#2-ground-rules-the-code-is-built-on)
3. [The data, reduced once](#3-the-data-reduced-once)
4. [The four core algorithms](#4-the-four-core-algorithms)
5. [The three real problems](#5-the-three-real-problems)
6. [The three lookalikes](#6-the-three-lookalikes)
7. [Traps, refusals and honesty fields](#7-traps-refusals-and-honesty-fields)
8. [Every operator ask and how it is answered](#8-every-operator-ask-and-how-it-is-answered)
9. [From finding to decision: impact, audience, prescription](#9-from-finding-to-decision-impact-audience-prescription)
10. [What "good" looks like: the standard](#10-what-good-looks-like-the-standard)
11. [The return arrow: decisions, replay, self-assessment](#11-the-return-arrow-decisions-replay-self-assessment)
12. [How the report becomes a score](#12-how-the-report-becomes-a-score)
13. [Why it works on data it has never seen](#13-why-it-works-on-data-it-has-never-seen)
14. [Every threshold, in one table](#14-every-threshold-in-one-table)
15. [Real, stubbed, simulated, and 100× scale](#15-real-stubbed-simulated-and-100-scale)
16. [Demo script: what to say, in the PS's order](#16-demo-script-what-to-say-in-the-pss-order)

---

## 1. The loop in one picture

```
 kit/ (any corpus)                                                                   loop-report.json
 ┌────────────────┐   ┌──────────┐   ┌───────────┐   ┌───────────┐   ┌────────────┐   ┌──────────────┐
 │ sessions       │──▶│ ingest   │──▶│ detector  │──▶│ diagnoser │──▶│ prescriber │──▶│ report       │
 │ agent_steps    │   │ one pass │   │ 6 scanners│   │ cause +   │   │ decision + │   │ builder      │
 │ config_timeline│   │ -> cubes │   │ candidates│   │ config +  │   │ replay req │   │ write, then  │
 │ catalog, labels│   └──────────┘   └───────────┘   │ impact    │   └────────────┘   │ validate     │
 └────────────────┘                                  └───────────┘                    └──────────────┘
          │                                                │                                  ▲
          ├──────────────▶ metrics + answers (A01–A10) ────┤                                  │
          ├──────────────▶ gap_analyzer (A11, A09, C2) ────┤                                  │
          └──────────────▶ standard (what good looks like) ┘                                  │
                                                                                               │
 humans ──▶ decide.py (approve / reject) ──▶ decisions.json ───────────────┐                  │
 humans ──▶ replay_client.py --send (one run) ──▶ verifications.json ──────┴─▶ loop_state ────┘
```

| PS stage | Module | Output in the report |
|---|---|---|
| **Detect** | `detector.py` | candidates (internal) |
| **Diagnose** | `diagnoser.py` | `findings[]`, `diagnoses[]` |
| **Prescribe** | `prescriber.py` | `prescriptions[]` |
| **Verify** | `replay_client.py` (human-triggered) + `loop_state.py` | `verifications[]`, `self_assessment` |
| Measure | `metrics.py`, `answers.py` | `metrics[]` |
| Refuse | `gap_analyzer.py` | `gaps[]` |
| Standard | `standard.py` | `standard[]` |

One command runs everything: `python -m nexus_loop.pipeline --kit <dir> --out loop-report.json`.
Exit 0 means the report was written and passed schema validation.

---

## 2. Ground rules the code is built on

**Rule 1 — never ask a model what the logs record.** No language model is called anywhere. Every fact
(an error, an empty payload, a handoff, a turn count, a cost, a judge version) is counted from the logs.
The only judged number is the corpus's own `quality_score`, which is calibrated against the human labels,
never re-judged.

**Rule 2 — change the agent, never the yardstick.** Fixes target KB content, a tool integration or a
prompt. Metric definitions, judge versions and the golden set are never edited.

**Separation of observation and judgment.** A scanner only says "this signal moved, here is the
evidence". Only the diagnoser decides "regression or lookalike, and why". Nothing is dismissed and
nothing is blamed by the component that found it.

**Two gates on every detection.**
- *Effect size:* is the move big enough to matter to an operator? Examples: resolution down 0.15; turns ×1.3.
- *Noise:* is the move big compared to its own sampling error (z ≥ 4)?

A size gate alone lets a thin cohort's noise through; a noise gate alone flags tiny but certain wiggles
on huge cohorts. Both must pass.

**Every sentence is computed.** Evidence strings, dismissal reasons and decision text are formatted from
numbers the code just calculated or from a `config_timeline` row. A dismissal whose checks fail is not
written at all.

**Nothing is hard-coded to the practice corpus.** No tenant, intent, agent, tool or day appears in the
code. An automated test (`tests/test_nexus_loop.py`) reads every name out of the catalog and fails if any
of them appears in the source.

---

## 3. The data, reduced once

| File | Grain | What we use it for |
|---|---|---|
| `sessions.jsonl.gz` (~80k) | one conversation | outcomes (`session_end`, `handoff_by_design`), turns, cost, `quality_score` + `judge_version`, intent, agent, model, milestones |
| `agent_steps.jsonl.gz` (~880k) | one execution step | tool calls (outcome, `result_field_count`, `response_bytes`, retries, latency), KB lookups (`kb_hit`, `kb_top_score`) |
| `config_timeline.csv` | one config change | change markers for attribution |
| `catalog.json` | — | capabilities (what is measurable), cardinality budgets, milestone journeys |
| `labels/rubric_scores.jsonl` | human quality score | calibration of the judged score |
| `labels/outcome_labels.jsonl` | human "resolved" label | the golden set for replay |

**Grain discipline.** Steps fan out per session, and averaging a step measure over sessions is the
catalog's "single most common error". So `ingest.py` makes **one streaming pass** over the steps and
reduces them into per-day counters ("step cubes"):

```
tool_by_tool  [(tenant, tool)]   [day] -> calls, ok, err, silent_empty, retry, err_class
tool_by_intent[(tenant, intent)] [day] -> the same, per intent
kb_by_intent  [(tenant, intent)] [day] -> lookups, hits, top_score sum/min/max
latency_ms    [tenant]           [day] -> tool_call durations (for p95)
tool_intents  [(tenant, tool)]         -> which intents actually issue calls to that tool
```

Session-level rates always come from session rows. Step-level rates always come from step counts.
The two are never mixed at the wrong grain.

**The key field semantic.** `outcome='ok'` means the call returned without an error; it does **not** mean
it returned anything useful. A call is counted as *silent-empty* when:

```
outcome = 'ok' AND result_field_count = 0
   (only if result_field_count is absent: response_bytes <= 2, i.e. "{}" or "[]")
```

A missing field is never read as zero.

---

## 4. The four core algorithms

### 4.1 Sliding-window changepoint (`changepoint.find_rate_changepoint`)

Every "did this metric move, and when?" question is the same computation on two daily series: a
numerator (e.g. resolved sessions, empty calls, total turns) and a denominator (sessions, ok calls).

```
for each candidate boundary day d:
    before window = days [d - W, d - 1]
    after  window = days [d, d + W - 1]            (W = 7 by default)
    skip d unless both windows have >= min_den denominator units
    before_rate = sum(num over before) / sum(den over before)
    after_rate  = sum(num over after)  / sum(den over after)
    delta       = after_rate - before_rate
pick the d with the best score:
    "rise"       score = delta              (largest increase)
    "fall"       score = -delta             (largest decrease)
    "rise_ratio" score = after / before     (largest multiple; needs before > 0)
    "fall_ratio" score = before / after
    "abs_delta"  score = |delta|            (either direction)
```

Days near the edges of the log cannot have a full window on both sides. With `allow_partial_window`,
the search falls back to clipped windows **only when no full-window boundary qualifies at all**, so a
thinner, noisier window can never outbid a real full-window signal.

**Why the criterion matters (the bug we fixed).** A fault that ends inside the log has two edges: the
onset (turns 4 → 7) and the recovery (7 → 4). They are nearly mirror images. `abs_delta` picks whichever
is a hair larger. When it picked the recovery, the scanner saw turns *falling*, failed the "turns rose"
test, and threw away a real fault. On unseen corpora that missed F3 every time. Faults that can recover
are now searched with a signed `rise` (or `fall`); `rise_ratio` could not be used for empty payloads
because their baseline is exactly 0.

```
rate
 0.20 |          ┌──────────────┐
      |          │   fault      │
 0.01 |──────────┘              └──────────  (after recovery: 0.00)
      +----------d10------------d25--------> day
         rise picks d10 (onset)   abs_delta picked d25 (recovery, |−0.20| > |+0.19|)
```

### 4.2 Recovery day (`find_recovery_day`)

Once the onset is known, the end of the finding's window is found by walking forward:

```
from the onset day, slide a short block (3–5 days):
    direction "down": first block whose rate <= target + tolerance
    direction "up":   first block whose rate >= target - tolerance
window.to_day = recovery_day - 1   (or the last day of the log if it never recovers)
```

"Never recovers" is a legitimate answer: the fault is still running at the end of the log.

### 4.3 Noise gates (z-tests)

For rates (resolution, empty-payload share), a pooled two-proportion z:

```
p   = (x1 + x2) / (n1 + n2)
se  = sqrt( p (1 - p) (1/n1 + 1/n2) )
z   = (x2/n2 - x1/n1) / se
```

For means (turns per session), a Welch z from running sums and sums of squares, so no per-session list
is ever needed:

```
mean_i = sum_i / n_i
var_i  = (sumsq_i - n_i * mean_i^2) / (n_i - 1)
z      = (mean_2 - mean_1) / sqrt(var_1/n_1 + var_2/n_2)
```

Detection requires **z ≥ 4**. We scan many cohorts × many boundary days, so the gate is deliberately
far above the usual 2. On full-size corpora real faults sit at z = 14.7–18.5; on the 5% sample, a real
fault can fall just under 4. We accept that miss rather than risk a false alarm, which the PS scores as
worse.

### 4.4 Config attribution (`detector.nearby_config_changes`)

A finding is tied to a change marker by a join that follows the catalog's warnings exactly:

```
config row matches when:
    row.tenant in (finding.tenant, '*')     <- '*' rows apply to every tenant (catalog: equality-only
                                               joins silently drop the judge change)
    row.kind  = the kind this cause needs   (kb for kb.gap, tool for tool faults, prompt/model for turns)
    row.target = the tool / agent           (only after filtering kind — catalog: tool_name→target
                                               fans out without kind='tool')
    |row.day - finding.from_day| <= 6
nearest day wins
```

If the matched change is **on or before** the day the fault first shows in the data, the finding's
window is moved back to the change day. A launch or a release can take a day to produce its first
conversation. The window is never moved later.

---

## 5. The three real problems

Each is built to beat a lazy approach. The table shows which one, and how the system avoids it.

| | Lazy approach that fails | Why | What we do instead |
|---|---|---|---|
| **F1 KB gap** | "compare this cohort to last month" | the cohort is new, so it has **no before-period** | compare to **peer traffic** over the same days |
| **F2 silent tool break** | "watch the tool error rate" | calls return HTTP 200, `outcome='ok'`: the error rate is **flat** | measure **empty payloads** among ok calls |
| **F3 prompt regression** | "alert when resolution drops" | resolution **does not move** | measure **turns** against the agent's **own past** |

### 5.1 F1 — a new product launches with nothing in the knowledge base

**Scanner:** `scan_new_cohorts`

1. For each tenant, find each intent's first day in the log. An intent first seen **after the first 10%
   of the log** is *new*; the rest are *mature*. The 10% is a fraction of the corpus length, not a day.
2. Skip cohorts with fewer than 30 sessions.
3. Over a 12-day ramp-up from its first day, compare the new intent's resolution rate to the mature
   intents of the same tenant **over the same days**.
4. Flag if `peer_rate − cohort_rate ≥ 0.15` **and** the two-proportion z ≥ 4.
5. End of window: the first 3-day block where the cohort climbs back to within 0.10 of the peer rate.
6. Collect KB evidence for the cohort: `kb_hit` rate, `kb_top_score` mean and range, number of lookups,
   and number of tool calls. The tool-call count is published as evidence so a reader can see a tool
   was not involved; the cause decision itself rests on the KB hit rate.

**Diagnosis:**
- `kb.gap` when the cohort's kb_hit rate is below 0.35. The nearest **kb** change for the tenant within
  ±6 days is attributed (confidence 0.85, or 0.6 with no change marker).
- Without a low hit rate but with a KB change nearby: `kb.gap` at 0.55. Otherwise `unknown`.
- The finding's window is pulled back to the KB change day if earlier.
- Cohort and peer rates are then **recomputed over that final window**, so every number in the finding
  describes the same days.

**Baseline named:** peer traffic. The cohort has no past.

**Practice corpus:**

| | |
|---|---|
| Cohort | `acme-bank`, intent `premium_card_info`, first appears day 35 |
| Attributed change | day 34 `kb` `kb_2026_06_a → kb_2026_06_c`, "premium card launch content pack" |
| Window | days 34–44 (11 days) |
| Resolution | **0.309** over 230 sessions vs mature intents **0.806** over 5,366; z = 18.0 |
| KB evidence | kb_hit **0.00** on 606 lookups; kb_top_score mean 0.24, range 0.11–0.38; 0 tool calls |
| Impact | 4.1% of the tenant's traffic; 114 conversations that would have resolved at the peer rate did not; 145 unplanned handoffs; 14 abandoned; $9.41 v3 cost |
| Report | `is_regression: true`, `kb.gap`, severity critical, audience agent_builder + business_owner |

**Why a lookalike cannot trigger it:** a mix shift changes shares, not a new cohort's own rate; a load
spike has no new intent; a judge change moves quality, not resolution.

### 5.2 F2 — an API returns empty results while reporting success

**Scanner:** `scan_silent_tool_failures`

1. For every `(tenant, tool)` with at least 150 calls, build daily series: `silent_empty` over `ok`
   calls, and declared errors over all calls.
2. Find the largest **rise** in the empty-payload share (signed criterion; the baseline is often exactly 0).
3. Flag if the share rises by at least 0.05 **and** z ≥ 4.
4. **Silence check:** across the same boundary (±7 days) the declared error rate must stay within ±0.02.
   If it moved, this is an ordinary outage, not a silent break. This flat-while-broken signature is what
   the PS says makes the fault invisible.
5. End of window: the first 3-day block where the empty share is back within 0.02 of its baseline.
6. Cohort intent: the intent that **actually issues the calls** to this tool, counted from the tool_call
   rows. Not the tenant's busiest intent.
7. Evidence: declared error rate before and during; empty share before and after; empty responses in
   the window; tool retries during vs before.

**Diagnosis:** `tool.contract_break`. The attributed change is the nearest `kind='tool'` row whose
**target is this tool** within ±6 days (confidence 0.88, or 0.65 without one).

**Baseline named:** the cohort's **own** resolution over the same number of days immediately before the
change. It looks like its peers today, so only its own past shows the drop.

**Practice corpus:**

| | |
|---|---|
| Cohort | `northwind-retail`, tool `get_order_status`, intent `order_status` (100% of the tool's calls) |
| Attributed change | day 40 `tool get_order_status 3.2.0 → 3.3.0`, "order service migration" |
| Window | days 40–51 (12 days) |
| Declared error rate | **0.0145 → 0.0156**: flat, so the dashboard stays green |
| Empty `ok` share | **0.0% → 13.7%**; z = 14.7; 382 empty responses |
| Retries | 323 during the window vs 0 in the 12 days before |
| Resolution | 0.852 (days 28–39) → **0.776** |
| Impact | 2,686 sessions, 29.0% of tenant traffic; 206 conversations lost vs its own past; 404 unplanned handoffs; 147 abandoned |
| Report | `tool.contract_break`, severity critical, audience platform_owner + agent_builder |

**Why a lookalike cannot trigger it:** a load event raises real errors and timeouts, which fails the
"declared errors flat" check. Neither a mix shift nor a rubric change produces empty payloads.

### 5.3 F3 — a prompt change makes conversations twice as long without changing outcomes

**Scanner:** `scan_turn_inflation`

1. For every `(tenant, agent_id)` with at least 150 sessions, build daily series of total turns and
   session counts (plus sums of squares for the z-test).
2. Find the largest **rise** in mean turns per session (signed criterion; this is the scanner the
   recovery-edge bug broke).
3. Flag if `after_mean / before_mean ≥ 1.3` **and** Welch z ≥ 4.
4. **Outcome-flat check:** resolution across the same boundary must stay within ±0.06. If resolution
   moved, it is a different mechanism, not pure effort inflation.
5. End of window: the first 5-day block where mean turns fall back under 1.15× the baseline.
6. Evidence: median and mean turns before and after; resolution before and during; cost per v3 session
   before and during.

**Diagnosis:** `prompt.regression` when a `kind='prompt'` row targets **this agent** within ±6 days
(confidence 0.85). If no prompt change fits but a model change on the same agent does, `model.change`
(0.6). With neither, `prompt.regression` at 0.5.

**Baseline named:** the agent's **own** previous 7 days.

**Practice corpus:**

| | |
|---|---|
| Cohort | `acme-bank`, agent `acme_main_v3` |
| Attributed change | day 46 `prompt acme_main_v3 p_v3 → p_v4`, "safety and confirmation wording" |
| Window | days 46–55 (still running at the end of the log) |
| Turns | median **4 → 6**; mean 4.60 → 7.04 (×1.53); z = 18.5 |
| Resolution | 0.776 → 0.803: flat, so no outcome alert fires |
| Cost per v3 session | $0.0445 → $0.0799 (**+80%**) |
| Impact | 1,224 sessions, 23.3% of tenant traffic; 2,960 extra turns; +$43.37 over the window |
| Report | `metric: median_turns`, `prompt.regression`, severity high, audience agent_builder |

**Why a lookalike cannot trigger it:** a mix shift toward long intents raises the *tenant* turn count
but not one agent's count at the same moment, and it has no prompt marker. A load spike and a rubric
change do not add turns.

---

## 6. The three lookalikes

A lookalike is examined **and dismissed in writing**. The PS scores flagging one as a regression (−4.2)
worse than missing a real fault. A dismissal is only written when its own checks pass, and it always
carries a `not_a_regression_because` plus a diagnosis with the true cause class.

### 6.1 D1 — a marketing campaign shifts the mix of questions

**Scanner:** `scan_traffic_mix`

1. For each tenant and intent, build the daily **share** of the tenant's traffic.
2. Search both a share **rise** (`rise_ratio`) and a **dip** (`fall_ratio`), because either edge can
   fool a naive detector. Keep the earliest valid onset.
3. Require a share move of at least 0.15.
4. **Stratification check:** the shifted intent's **own** resolution must be stable within ±0.10
   (10 days before vs during).
5. End of window: when the share returns to within 0.05 of its baseline.
6. Also measure every **other** intent's own before/during move (intents with ≥ 30 sessions on each
   side), and list quality-relevant config changes within ±5 days.

**Dismissal:** `traffic_mix`. The aggregate moved because *who is asking* changed, not *how well the
agent answers*. Confidence drops from 0.9 to 0.75 when a config change sits nearby.

**Practice corpus:** `branch_locator` share **13.2% → 43.6%** on `acme-bank` from day 22. Its own
resolution holds at 0.908 → 0.899. No other intent moves by more than 0.042 (median 0.007). The tenant
aggregate *rises* 0.812 → 0.832, purely from the mix. A prompt change on a different agent
(`acme_cards_v3`, day 23) is listed and confidence lowered accordingly.

**The mistake it prevents:** reporting "resolution improved" (or, in reverse, "degraded") when no cohort
changed. This is Simpson's paradox. The A02 answer uses the same idea (§8).

### 6.2 D2 — a flash sale spikes traffic and latency for four days

**Scanner:** `scan_load_events`

1. For each tenant, daily session volume; find the largest **volume ratio** (`rise_ratio`, 4-day windows).
2. Require ×1.9 or more.
3. End of window: when volume falls back under 1.3× the baseline.
4. **Quality check:** resolution 7 days before vs during must stay within ±0.06. **If it moved, the event
   is not dismissed.** A regression that happens to coincide with a spike must never be explained away.
5. Evidence: volume ratio, tool p95 latency, declared tool error rate, quality within one judge version.

**Dismissal:** `load`. A capacity event for the platform team, not an agent-quality regression.

**Practice corpus:** `northwind-retail` volume **856 → 2,696 sessions/day (3.1×)** on days 12–15.
Tool p95 **1,348 → 3,152 ms**; declared errors 0.022 → 0.051 (real timeouts); resolution
0.806 → 0.777, inside the 0.06 band; quality in judge v1 3.88 → 3.84; volume back to baseline from day 16.

**The mistake it prevents:** calling a latency spike a quality regression.

### 6.3 D3 — the quality-scoring rubric changes version halfway through

**Scanner:** `scan_judge_boundary`

1. Daily mean `quality_score` over all sessions; find the largest **fall**.
2. Require a drop of at least 0.35.
3. **Outcome check:** raw resolution across the same boundary within ±0.06.
4. **Every tenant at once:** each tenant must drop by at least half the threshold (0.175) on the same
   day. A drop in one tenant is not a rubric change.
5. Read which `judge_version` is stamped on scored sessions before and after.

**Dismissal:** `judge_change`, attributed to the `tenant='*'` judge row within ±6 days. It is only
written if that row **or** a judge_version change exists. Otherwise the drop is not explained away.
From here on, quality is trended only within one version.

**Practice corpus:** quality **3.89 → 3.29** on day 28. By tenant, acme-bank 3.91 → 3.31 and
northwind-retail 3.88 → 3.27. Resolution 0.809 → 0.805. Versions v1 → v2. Config row day 28 `*` `judge
quality_rubric v1 → v2`, "stricter quality rubric rolled out".

**The mistake it prevents:** "quality regressed 15%". That measures the rubric, not the agent; the
catalog marks the comparison `UNSOUND_ACROSS_JUDGE_VERSIONS`. Every other use of `quality_score` in the
system (A07, A10, D2 evidence) is also restricted to one version.

---

## 7. Traps, refusals and honesty fields

### 7.1 Every metric carries three honesty fields

| Field | Meaning | How it is filled |
|---|---|---|
| `fidelity` | measured / judged / derived | set per metric from what it is computed from |
| `coverage` | share of traffic that can produce this number | **computed from the data** per tenant, with the basis and exclusions written out |
| `calibration` | agreement of a judged number with human labels | computed from `labels/rubric_scores.jsonl` |

### 7.2 C1 — legacy v2 traffic logs no tool calls (the denominator trap)

`v2_flow` sessions emit no `tool_call`, `kb_lookup` or `guardrail` steps. A tool error rate over "all
traffic" would silently count them as zero-error. Every tool, KB and cost metric instead:

- is published **per tenant**;
- sets `coverage.value` = that tenant's measured v3 share (practice: acme-bank **0.7205**,
  northwind-retail **0.9308**);
- lists `excluded: ["agent_kind = v2_flow"]`.

### 7.3 C2 — a field that is nearly unique per customer (the cardinality trap)

`gap_analyzer.cardinality_audit` reads every cardinality budget in `catalog.json` and counts that
field's **actual distinct values** in the corpus. Over budget means refused:

- a `CARDINALITY_REFUSED` gap citing both numbers (practice: `customer_ref` **80,252 distinct vs budget
  200**), with the nearest in-budget proxy (`segment`, 3 values, budget 50) and why the proxy misleads;
- identifier fields with budget 0 (`session_id`, `step_seq`) refused as never valid;
- a planner guard (`apply_breakdown_budgets`) that removes any over-budget breakdown from a metric's plan
  and records it under `refused_breakdowns`. Nothing is silently truncated.

### 7.4 A11 — "What is our failover rate?" (the unanswerable ask)

The catalog declares `failover_rate` as `NOT_MEASURABLE`: the runtime has no failover mechanism, so the
event is never logged. The gap is built from the catalog:

| Field | Value |
|---|---|
| `verdict` | `NOT_MEASURABLE` |
| `why` | no failover mechanism exists; there is no primary/secondary path to observe |
| `nearest_proxy` | `llm_call.retry_count > 0` |
| `why_the_proxy_misleads` | those are **quality retries at a different temperature**, not failovers; reporting them would read as "0% failover", i.e. as good news |
| `required_event` | `failover`, grain `step`, fields `from_target, to_target, reason, recovered`, owner `conversation-runtime` |

The refusal is itself a deliverable: it tells the platform team exactly what to start logging.

### 7.5 A09 — "How many users gave up out of frustration?" (the fidelity trap)

Two halves, handled differently:

- **That** a user abandoned is **measured**: `session_end = 'abandoned'`. Answered in the gap's
  `measured_part` (practice: 4,653 abandoned sessions; acme-bank 5.9%, northwind-retail 5.7%).
- **Why** they abandoned is a judgment about meaning. No calibrated judge for it exists, so it is a
  `REQUIRES_NEW_JUDGE` gap, with the event a judge would need to emit (`session_id, judge_version,
  label, rationale, calibration_agreement`).

Inferring "frustration" from the end code would be a Rule 1 violation dressed up as an aggregate. No
A09 metric is published, so the scorer reads A09's fidelity from the gap: `judged`.

### 7.6 A04 — "How often did a tool call fail?" (the other fidelity trap)

**Measured**, from `outcome` on tool_call steps. The canonical `m_tool_failure_<tenant>` metrics
(`measured`) are listed **before** the derived silent-empty metrics, because the scorer reads only the
first metric for an ask. Practice: acme-bank 1.86% of 17,936 calls; northwind-retail 2.63% of 44,262,
broken down by `error_class`.

The companion `m_silent_tool_empty_<tenant>` is `derived` (catalog: `silent_tool_success` is
*derivable, not declared*). It is exactly the number the plain error rate misses: 0.89% on northwind,
all of it from F2.

### 7.7 Calibration of the judged score

```
agreement = share of human-labelled sessions where |quality_score − human_quality| ≤ 1.0
            (each label compared only with a score from the judge_version it was labelled under)
```

Practice: **0.9479** over 96 labels (v1 0.917 on 48; v2 0.979 on 48). The labellers disagree with each
other ~7.5% of the time, so 1.00 would signal a bug. The metric warns when n < 30 or agreement ≥ 0.995,
which is what happens on the 5% sample, where only 6 labels match.

---

## 8. Every operator ask and how it is answered

| Ask | Operator question | Method | Fidelity | Practice answer |
|---|---|---|---|---|
| **A01** | Share handled end to end without a human? | containment = (resolved + handoffs *by design*) / all sessions | measured | **0.840** (acme 0.850, northwind 0.835) |
| **A02** | Better or worse month over month? | first vs last full 28-day month, **raw and at a fixed intent mix**; per-intent change with z; plus the stratified weekly trend. Quality trend refused across the judge boundary | measured | acme −0.016 raw, −0.007 at fixed mix (about half the fall is mix); northwind −0.005, with `order_status` significantly worse (F2) |
| **A03** | Which intents cost most per resolved conversation? | Σ cost of the intent's v3 sessions / its resolved count, per tenant, coverage = v3 share | measured | acme: txn_dispute $0.131, employment_letter $0.096, premium_card_info $0.095 |
| **A04** | How often did a tool call fail? | §7.6 | measured | 1.86% / 2.63% |
| **A05** | Where do people drop out of the returns journey? | funnel over `catalog.milestones`: sessions reaching each milestone in order; largest drop. The journey is matched from asks.md words ("returns" → `return_initiate`) at runtime; every journey is published | measured | largest drop **item_chosen → return_created**: 24.7% lost; 75.3% complete |
| **A06** | Is the KB actually answering? | kb_hit rate and mean kb_top_score per tenant; the judged reading ("did the document satisfy the question") is offered as an alternative that needs a judge | measured | acme 0.884 hit (mean score 0.74); northwind 0.910 (0.75) |
| **A07** | Did the model upgrade help or hurt? | for every `kind='model'` row: the changed agent's v3 sessions 7 days before vs after, standardized to the before-window intent mix; resolution (with z), turns, cost; quality only inside one judge version; other changes and overlapping findings listed | measured (quality part judged) | day 9 `acme_main_v3`: **no measurable change** (−0.026, z −0.9). Day 26 `nw_care_v3`: **no measurable change**, quality **refused**: the window crosses the day-28 judge change |
| **A08** | How much did we spend, and on what? | Σ cost over v3 sessions per tenant, by week and intent; coverage = v3 share | measured | acme $954.06; northwind $2,064.21 |
| **A09** | How many gave up out of frustration? | §7.5 | judged gap + measured part | refused (reason), 4,653 abandoned (fact) |
| **A10** | Which conversations should a human review this week? | scope = last 7 days ∩ (inside a reported regression ∪ unplanned handoff ∪ abandoned); ranked by judged quality **percentile within its own judge_version**; top 25 listed | derived | 2,954 in scope (1,734 in regressions, 1,007 unplanned handoffs, 549 abandoned) |
| **A11** | Failover rate? | §7.4 | refused | `NOT_MEASURABLE` + event spec |

**Fixed intent mix (direct standardization)**, used by A02 and A07:

```
weights w_i = intent i's share of sessions in the BEFORE period (intents with >= 10 sessions on both sides)
before_fixed = Σ w_i × rate_before_i
after_fixed  = Σ w_i × rate_after_i
change explained by mix = (raw_after − raw_before) − (after_fixed − before_fixed)
```

If the raw number moves but the fixed-mix number does not, the movement is composition, not performance.

---

## 9. From finding to decision: impact, audience, prescription

The PS: *"Resolution in cohort X fell from 0.86 to 0.31" is true and useless.* Every regression carries
a decision-shaped block, computed as follows.

### 9.1 Impact

| Field | Formula |
|---|---|
| `conversations_affected` | sessions in the finding's cohort and window |
| `share_of_traffic` | that count / all of the tenant's sessions in the same days |
| `would_have_resolved_at_baseline` | round(n × (baseline_rate − observed_rate)) |
| `actually_resolved` | cohort sessions with `session_end='resolved'` |
| `unplanned_handoffs` | `session_end='handoff'` with `handoff_by_design` not true |
| `abandoned` | `session_end='abandoned'` |
| `cost_usd` | Σ cost over the cohort's v3 sessions (F3: extra cost = Δ cost per session × v3 sessions) |
| `extra_turns_total` (F3) | round((mean turns during − mean turns before) × n) |
| `days_running` | window length |
| `derivation` | the sentence that shows every one of these calculations, **naming the baseline and why it was chosen** |

**Severity** (resolution faults): critical if the drop ≥ 0.35 or share ≥ 15%; high if the drop ≥ 0.15
or share ≥ 5%; medium if the drop ≥ 0.05. F3 is high when turns rise ≥ 1.5×.

**Audience by cause:**

| Cause | Who acts |
|---|---|
| `kb.gap` | agent_builder + business_owner |
| `tool.contract_break` | platform_owner + agent_builder |
| `prompt.regression` / `model.change` | agent_builder |

**`if_nothing_changes`** is one plain sentence built from the numbers. For example, F2: *"About 17
conversations a day that used to resolve now do not, and 404 reached a human over the window, while
every error-rate dashboard for 'get_order_status' stays green because the calls report success."*

### 9.2 Prescription (`prescriber.py`)

| Cause | Change type | Why this class |
|---|---|---|
| `kb.gap` | `kb.add` | add the missing content; changes no existing answer |
| `tool.contract_break` | `tool.validate` | treat an `ok` call with no fields as a failure and take the fallback |
| `prompt.regression` | `prompt.edit` | remove the added confirmations; an **edit, not a revert**, which would also undo everything else in that prompt version and risks known-good conversations |

Every prescription carries:

- **`predicted_delta`**: the finding's observed value returning to the baseline the finding already
  names (practice: p1 resolution 0.309 → 0.806; p2 0.776 → 0.852; p3 median turns 6 → 4). No new
  number is invented.
- **`decision`**: `asking_approval_for`, `risk_if_diagnosis_wrong`, `would_not_ship_if`, each a sentence
  formatted from the diagnosis signals.
- **`autonomy_rung: L1`**: the system proposes; a person approves and applies.
- **`signature`**: `cause|tenant|cohort|from_day`. Recorded decisions and replay results only re-attach
  to the same diagnosis.
- **`replay_request`**: the exact body for the replay service. The tenant, the change, the cohort
  **keys** and the finding window (the service needs the right key, value and overlapping days), and a
  golden set.

### 9.3 Golden set

Per tenant: up to 24 conversations that **humans labelled as resolved** (`labels/outcome_labels.jsonl`)
and that sit **outside every reported regression**, taken round-robin across intents. The version
string is a hash of the chosen IDs, so the same corpus always yields the same set. It is derived once
and never edited by a fix.

---

## 10. What "good" looks like: the standard

The PS: one problem has no before-period, so only a comparison against the deployment's own best
conversations catches it. `standard.py` mines that bar from the deployment itself:

```
for each (tenant, intent) of v3 traffic:
    drop sessions inside any reported regression's window and cohort   (a broken stretch never lowers the bar)
    resolution_rate: daily rates over days with >= 15 sessions (need >= 7 such days)
        best   = 90th-percentile day      (the schema's "top decile")
        median = median day
        deficit = best − median
    turns_to_resolve: daily median turns of resolved sessions over days with >= 10 resolved
        best   = 10th-percentile day      (fewer turns is better)
        median = median day
        deficit = median − best
```

Practice examples:

| Tenant / intent | Metric | Best | Median | Deficit | Note |
|---|---|---|---|---|---|
| acme-bank / `product_info` | resolution_rate | 0.817 | 0.754 | 0.062 | 185 sessions in regression windows excluded |
| northwind-retail / `order_status` | resolution_rate | 0.901 | 0.856 | 0.045 | 2,489 excluded (F2 window) |
| northwind-retail / `order_status` | turns_to_resolve | 3 | 4 | 1 | |

35 cohort standards are mined in total. Each carries the golden-set version it was mined alongside.

---

## 11. The return arrow: decisions, replay, self-assessment

```
report ──▶ screen / decide.py ──▶ decisions.json ─────────────┐
report ──▶ replay_client.py (dry run: prints requests)          │
           replay_client.py --send --prescription pN (1 run) ──▶ verifications.json + replay_ledger.json
                                                                 │
pipeline --decisions decisions.json --verifications verifications.json
           └─▶ loop_state: validate, attach, compute self-assessment ─▶ report
```

**Decisions** (`decide.py`):
- A verdict of `accepted`, `rejected` or `deferred`, with who decided and a **required reason**.
- Written into `prescriptions[].approval` immediately, and into `decisions.json` with the signature.
- A rejection with a good reason is as valid as an approval (PS).

**Replay** (`replay_client.py`, run only by a person):
- The dry run prints the exact request bodies.
- `--send` posts **one** prescription.
- An exact repeat is refused, since the service is deterministic and a repeat only spends a run.
- A local ledger counts runs against the 40-run budget and stores every raw response.
- The result is written to `verifications.json` in the pipeline's format.

**Validation on merge** (`loop_state.py`): an entry attaches only if its prescription exists in this run
and its signature matches, a verification needs a real `rp_…` run id, and verdicts must come from the
schema's closed sets. Anything else is rejected with a printed reason.

**Self-assessment** (the system's own hit rate):

```
prediction_error          = (predicted to − from) − (observed after − before)
per change type:  n, hit_rate = improved / n, mean_prediction_error
cycles                    = distinct replay run ids
downweighted              = change types with n >= 3 and hit_rate < 50%
```

A single observation is reported but never acted on.

---

## 12. How the report becomes a score

`score.py` awards 55 machine points in four sections. This is how each part of our report meets them.

### 12.1 Diagnostic accuracy (20): split across the 3 faults, 6.67 each

A finding matches a fault when the **tenant** matches, **any cohort key/value** matches, and the window
overlaps `[onset − 3, onset + duration]`. Per fault:

| Part | Weight | What earns it | Ours |
|---|---|---|---|
| Detection | 45% × max(0.35, 1 − lag/10 × 0.65) | `from_day` at or before onset (lag 0) | onset = config day → lag 0 |
| Localisation | 20% | right cohort key, not just the tenant | intent / tool+intent / agent_id |
| Diagnosis | 35% (a right prefix only earns 18%) | exact `cause_class` | kb.gap / tool.contract_break / prompt.regression |
| Attribution bonus | +5%, capped at 35% | right config kind within ±2 days | kb / tool / prompt, same day |

### 12.2 Specificity (15): starts full and is deducted

| Event | Points | Ours |
|---|---|---|
| a decoy flagged as a regression | −4.2 each | none |
| a decoy never examined | −1.47 each | each dismissed with overlap + reason + exact cause class |
| stray regressions matching nothing | −0.6 each (cap −3) | none |
| no findings at all | 0 total | — |

### 12.3 Loop completeness (8, capped)

| Item | Points | Ours |
|---|---|---|
| metrics present | 2.0 | ✓ |
| standard mined | 3.0 | ✓ |
| diagnoses linked to real findings | 2.5 | ✓ |
| prescriptions linked to real diagnoses | 2.5 | ✓ |
| predicted delta | 2.0 | ✓ (cap reached) |
| real replay run (`rp_`) (+1 if improved) | 3.5 | after the team's replay |
| self-assessment (+1.5 at ≥ 3 cycles, +0.5 downweighted) | 1.5 | after the team's replay |

### 12.4 Honesty (12, capped)

| Item | Points | Ours |
|---|---|---|
| share of metrics with fidelity and coverage × 4 | 4.0 | all metrics |
| a tool metric's coverage within ±0.03 of the true v2 hole | 3.0 | 0.7205 / 0.9308, computed |
| calibration published (−1 if ≥ 0.995) | 2.0 | 0.9479 |
| A11 correctly `NOT_MEASURABLE` | 3.0 | ✓ |
| required-event fields named | 1.5 | all 4 |
| nearest proxy and why it misleads | 0.5 | ✓ |
| fidelity traps (A04 measured, A09 judged) | +0.75 each, −1 if wrong | both correct |

**Practice result: 20 + 15 + 8 + 12 = 55 / 55.** The other 45 points are human-judged: decision quality,
the screen, framing.

---

## 13. Why it works on data it has never seen

The sealed corpus moves every fault and decoy to different days and lengths, and the PS says different
tenants too. We tested that directly and offline, without spending replay runs:

- **New layouts:** the kit's own generator builds corpora with new layouts
  (`generate.py --sealed <phrase>`), each with its own answer key.
- **New tenant names:** one copy has every tenant name replaced.
- **Unseen holdouts:** two corpora were generated **after** the fixes were written and never inspected.
- **Scoring:** only `score.py` read the answer keys.

| Corpus | Fault onsets (F1 · F2 · F3) | Score |
|---|---|---|
| practice A | 34 · 40 · 46 | 55.0 |
| sealed-style alpha | 7 · 14 · 22 | 55.0 |
| sealed-style bravo | 23 · 5 · 6 | 55.0 |
| sealed-style charlie | 9 · 31 · 26 | 55.0 |
| sealed-style delta | 8 · 28 · 23 | 55.0 |
| alpha, tenants renamed | 7 · 14 · 22 | 55.0 |
| holdout echo | 26 · 30 · 9 | 55.0 |
| holdout foxtrot | 7 · 11 · 23 | 55.0 |
| fresh suite layout + its renamed copy | new | 55.0 / 55.0 |

What makes this hold:

1. **Cohorts are discovered, not named.** Tenants, intents, agents and tools come from the data; the
   rule-guard test fails the build if a catalog name appears in the code.
2. **Days are found by changepoints, not configured.** The 10% "mature" cut-off is a fraction of the
   corpus length.
3. **Signed criteria** handle faults that end mid-log, and at the very start of the log (bravo: F2 on
   day 5, F3 on day 6).
4. **Noise gates** stop thin cohorts from producing false alarms.
5. **Attribution uses joins, not text.** Config rows are matched by tenant (including `*`), kind and
   target, never by keywords in notes.
6. **Deterministic.** The same corpus gives the same report byte for byte, apart from the timestamp
   (tested).

Run the check any time: `python scripts/sealed_suite.py`, and `python -m unittest discover -s tests`.

---

## 14. Every threshold, in one table

| Constant | Value | Where | Meaning |
|---|---|---|---|
| `CP_WINDOW` | 7 days | detector | before/after window for changepoints (4 days for volume spikes) |
| `MIN_Z` | 4.0 | detector | noise gate on every fault detection |
| `NEW_COHORT_MATURE_FRACTION` | 0.10 | F1 | an intent seen in the first 10% of the log is mature |
| `NEW_COHORT_MIN_SESSIONS` | 30 | F1 | smallest new cohort considered |
| `NEW_COHORT_DROP` | 0.15 | F1 | resolution gap to peers that is material |
| `NEW_COHORT_RAMPUP_DAYS` | 12 | F1 | probe window from first appearance |
| kb_hit for `kb.gap` | < 0.35 | F1 diagnosis | retrieval mostly failing |
| `SILENT_EMPTY_MIN_CALLS` | 150 | F2 | smallest tool series considered |
| `SILENT_EMPTY_JUMP` | +0.05 | F2 | rise in empty share among ok calls |
| `DECLARED_ERR_FLAT_TOL` | ±0.02 | F2 | declared error rate must stay flat |
| `EMPTY_BODY_BYTES` | 2 | ingest | "{}" / "[]", used only when result_field_count is absent |
| `TURNS_MIN_SESSIONS` | 150 | F3 | smallest agent series considered |
| `TURNS_INFLATION_RATIO` | ×1.3 | F3 | material rise in mean turns |
| `RESOLUTION_FLAT_TOL` | ±0.06 | F3, D2, D3 | resolution counts as unchanged |
| `SHARE_SHIFT_MIN` | 0.15 | D1 | material change in an intent's share |
| `SHARE_STABLE_TOL` | ±0.10 | D1 | the shifted intent's own rate counts as stable |
| `VOLUME_SPIKE_RATIO` | ×1.9 | D2 | material volume spike |
| `QUALITY_DROP_MIN` | 0.35 (0.175 per tenant) | D3 | material corpus-wide quality drop |
| `CONFIG_ATTRIBUTION_WINDOW` | ±6 days | diagnoser | change-marker search radius |
| `CALIBRATION_TOLERANCE` | ±1.0 point | metrics | agreement definition on the 1–5 scale |
| `MIN_DAY_SESSIONS` / `MIN_DAY_RESOLVED` | 15 / 10 | standard | a day counts toward the standard |
| `BEST_QUANTILE` | 0.90 | standard | top-decile day |
| `GOLDEN_SET_SIZE` | 24 per tenant | prescriber | replay regression guard |
| `MONTH_DAYS` | 28 | A02 | a month |
| `CHANGE_WINDOW_DAYS` | 7 | A07 | before/after around a model change |
| `REVIEW_WINDOW_DAYS` / queue | 7 / 25 | A10 | "this week", and the list length |
| `DOWNWEIGHT_MIN_N` / hit rate | 3 / < 50% | loop_state | when the loop trusts a change type less |

Effect-size thresholds are in the metric's own units and describe what an operator would call material.
None is tied to a day, tenant or answer-key value, and the tests on unseen layouts are the evidence
that none is overfitted.

---

## 15. Real, stubbed, simulated, and 100× scale

| | |
|---|---|
| **Real** | Every metric, finding, diagnosis, impact figure, standard, gap and prescription is computed deterministically from the corpus with no model and no network call. Schema validation and link checks run on every report. |
| **Judged (not ours)** | `quality_score` is the corpus's pre-computed judge output; we calibrate it but do not re-judge. |
| **Not built** | A judge for abandonment *reason* (reported as a gap). The approval **screen** itself (its back end `decide.py` exists). |
| **Human-triggered** | Replay verification: requests are prepared by the system and sent by a person, one at a time. |
| **Simulated** | The corpora we tested on are the kit's generated data. The sealed-style corpora come from the same generator with new passphrases. |
| **At 100× scale** | Steps are already one streaming pass into per-day cubes. Sessions (~80k) are held in memory and would need the same per-day aggregation; per-day latency lists would need a quantile sketch (e.g. t-digest) instead of raw values. The rest scales with the number of cohorts, not rows. |

---

## 16. Demo script: what to say, in the PS's order

1. **The finding and its impact.** F2: *"For 12 days, 29% of Northwind's traffic lost 206 resolutions
   and sent 404 customers to a human, and every error dashboard stayed green."* Show the flat declared
   error rate (1.45% → 1.56%) next to empty `ok` responses jumping 0% → 13.7%.
2. **The lookalike we ignored.** D1: *"Acme's resolution went up during the campaign, but no intent got
   better. branch_locator went from 13% to 44% of traffic."* Or D3: *"quality fell 0.6 on both tenants
   on the same day the rubric changed."*
3. **The diagnosis.** `tool.contract_break`, confidence 0.88, and why: empty payloads, flat errors,
   retries 0 → 323, 100% of calls from `order_status`.
4. **The config change behind it.** Day 40, `get_order_status` 3.2.0 → 3.3.0, "order service migration",
   matched by tenant, `kind='tool'` and target.
5. **The proposed fix.** `tool.validate`, with the decision block: what we ask approval for, the risk if
   wrong (legitimate empties; the baseline rate was 0%), and when not to ship.
6. **Proof it works.** The replay run on the finding's cohort and window, with the golden set. Say
   `no_effect` out loud if that is what came back; it is a result, not an error.
7. **The approval gate.** Record accept or reject with a reason; show it written back into the report.
8. **The question we refused.** A11, failover rate.
9. **Why we refused.** *"No failover event exists in the runtime. The closest proxy, retries, are
   quality retries at a different temperature. Reporting them would say 0% failover, which reads as good
   news and is false. Here is the event the runtime would have to emit: `failover(from_target,
   to_target, reason, recovered)`."*

Say coverage out loud when showing any tool number: *"measured on 72% of Acme's traffic; the other 28%
is legacy flow that logs no tool calls, so we exclude it rather than count it as error-free."*
