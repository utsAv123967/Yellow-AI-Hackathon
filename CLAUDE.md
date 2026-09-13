# CLAUDE.md — Nexus Loop (Yellow.ai TechQuest)

**This file is the guardian of the project.** Read it fully at the start of every session before touching code, data, or the report. It encodes the problem statement, the rules that are *disqualifying* if broken, the exact scoring, and the deliverables.

If anything here conflicts with an idea that "seems better", **this file wins**. `YellowAI_PS_TechQuest.pdf` is the source of truth; this is its faithful digest.

---

**Format:** Detect · Diagnose · Prescribe · Verify · **Team:** 7 people, min. one 2nd-year and one 1st-year · **Scoring:** 100 points (55 machine, 45 human) · **Duration:** 6 days, sealed run at 12:00 on day 6.

---

## 0. The one-line mission

> An agent deployment records almost everything it does and learns nothing from it, because the improvement loop is closed by a human moving between six tools. **Build the loop instead.**

From 8 weeks of logs from an AI-agent deployment we have never seen, our system must produce:

1. **`loop-report.json`** — machine-generated, schema-valid: what broke, why, who must act, what it costs, and what could not be measured at all.
2. **One screen** — a person can read a finding, see the evidence, and **approve/reject** the proposed fix; the decision is written back into the report.

Four stages, in order: **Detect → Diagnose → Prescribe → Verify** (plus the return arrow: record the human decision and the loop's own hit rate).

> A report nobody can read is a script. A screen with nothing behind it is a mockup.
> **Both are required.**

---

## 1. 🚨 THE TWO RULES YOU CANNOT BREAK 🚨

Breaking either is **disqualifying** — not a lost point, a lost hackathon. Every code review, every PR, every generated artefact is checked against these two first.

### ❌ RULE 1 — Never ask a language model something the logs already record.

Facts in the data are **queried, never inferred**: whether an API returned an error, which model served a turn, whether a handoff happened, status codes, tool names, costs, token counts, durations, milestones, session end codes.

An LLM may **only** be used for questions of *meaning*:
- Was the user satisfied?
- Was the answer correct?
- What was this conversation about?
- *Why* did the user abandon? (the fact of abandoning is measured; the reason is judged)

Any judge we build must be **versioned** and must **publish its agreement with the human labels** in `kit/labels/`. Concretely:
- `A04` ("how often did a tool call fail?") answered with a model = **Rule 1 violation**.
- `A09`'s abandonment *reason* inferred from `session_end='abandoned'` = **a Rule 1 violation dressed up as an aggregate** (the catalog's own words).

### ❌ RULE 2 — The system may propose changes to the agent, never to the yardstick.

Fixed and untouchable: **metric definitions, judge versions, and the reviewed set of good conversations (golden set)**.

A "fix" whose effect is that a number improved *because its definition changed* is **disqualifying**. Never trend `quality_score` across a `judge_version` boundary and call the movement a regression — that measures the rubric, not the agent.

### The consequence: three honesty fields on every number

| Field | Meaning | Rule |
|---|---|---|
| **Fidelity** | `measured` (from logs) / `judged` (by a model) / `derived` (from other numbers) | Required on every metric |
| **Coverage** | what share of traffic can even produce this number | Required on every metric; a metric that ignores coverage is wrong even when its arithmetic is right |
| **Calibration** | for judged numbers: agreement with the human labels | Human labellers disagree ~**7.5%** of the time — **a calibration of 1.00 is a bug, not a good judge** (the scorer actively penalises ≥ 0.995) |

---

## 2. Allowed / not allowed — the quick table

| Topic | ✅ Allowed | ❌ Not allowed |
|---|---|---|
| **LLM usage** | Judging meaning: satisfaction, correctness, topic, abandonment *reason* — versioned and calibrated. Also fine for writing our own code and UI. | Deriving any fact the logs already hold. Unversioned or uncalibrated judges. Calibration reported as 1.00. |
| **Metric definitions** | Deriving a new, *declared* capability — e.g. `silent_tool_success` from `response_bytes` / `result_field_count` on a 200; the catalog says this is legitimate, not a gap | Redefining or relaxing an existing metric, judge, or golden set so results look better |
| **Joins — sound** | `step.session_id → session.session_id` (many-to-one); `turn.(session_id,turn_index) → step.(session_id,turn_index)` (one-to-many) | Joining without respecting grain |
| **Joins — with care** | `session.(tenant,day) → config_change.(tenant,day)`: **`config_change.tenant` may be `'*'`** (applies to all tenants) — an equality-only join *silently drops the judge-version change*. `step.tool_name → config_change.target`: **only** with `kind='tool'`, otherwise it fans out. | Equality-only tenant join; unfiltered tool join |
| **Joins — forbidden** | — | Comparing `session.quality_score` **across judge versions** — the catalog marks this `UNSOUND_ACROSS_JUDGE_VERSIONS` |
| **Grain** | Re-aggregate step measures to session grain *before* averaging over sessions | Averaging a step measure over sessions directly — *"the single most common error against this corpus"* (catalog) |
| **Breakdowns** | Group by intent, tenant, week, `agent_kind`, `segment` (budget 50) | Group by `custom_dims.customer_ref` (budget **200**, near-unique) or `session_id` / `step_seq` (budget 0). **Refuse and cite the budget** — never silently truncate. This is trap **C2**. |
| **Transcripts** | Read for meaning and judgement | Using transcript text to establish a fact: *a 500, a timeout, a guardrail block and "didn't know" all render as the same apology string* |
| **Denominators** | Exclude `v2_flow` sessions from tool / cost / KB metrics and say so | Counting `v2_flow` as zero-error — trap **C1** |
| **Unanswerable asks** | Emit a gap spec naming the exact event that would need to exist | Answering A11 with a proxy |
| **Day 6** | The system runs untouched on a sealed corpus | Hand-holding, manual edits to the sealed output, hard-coded practice answers |

---

## 3. Scoring — where the 100 points live

**55 machine points** (exact grader: `tools/nexus-loop-kit/score.py` — not a black box, run it) plus **45 human points**.

### Machine, 55 (script)

| Pts | Criterion | What actually earns it |
|---|---|---|
| **20** | **Diagnostic accuracy** | Split evenly across the faults (**6.67 each** when there are 3). Per fault: **45%** detection, decayed by lag — full at lag 0, and the decay **bottoms out at 35% of the slice around lag 10 and never falls further**, so a very late detection still scores · **20%** naming the right **cohort key**, not just the tenant · **35%** the right `cause_class`; a right *prefix* (any `tool.*`) gets 0.18 instead. A finding whose window starts up to **3 days before** onset still matches. **The change-marker bonus (+0.05, right `kind` within ±2 days) is capped at 0.35** — so it adds nothing if you already named the exact `cause_class`; it only rescues a prefix-only match (0.18 → 0.23). |
| **15** | **Specificity** | Starts full and is *deducted*. Each decoy flagged as a regression: **−4.2**. Each decoy never examined at all: **−1.47** (partial) or **−4.2** if nothing was flagged anywhere. Stray findings matching neither fault nor decoy: **−0.6 each, capped at −3.0**. Reporting nothing scores **0** — this is earned by *looking* and being right. |
| **12** | **Honesty** | 4.0 × (share of metrics carrying both fidelity and coverage) · **+3.0** if at least one **tool-derived metric** claims coverage within ±0.03 of a tenant's true v2 hole — **declare no tool metric at all and you forfeit this** · **+2.0** calibration published, **−1.0** if any is ≥ 0.995 · **+3.0** correct `NOT_MEASURABLE` on A11, **+1.5** for naming the required event's fields, **+0.5** for `nearest_proxy` *and* `why_the_proxy_misleads` · **+0.75** per correct fidelity trap, **−1.0** per wrong one. Raw total is capped at 12, and a complete report reaches ~15.5 raw — so there is roughly **3.5 points of slack** here, but do not spend it deliberately. |
| **8** | **Loop completeness** | metrics 2.0 · standard 3.0 · diagnoses linked to real findings 2.5 · prescriptions linked to real diagnoses 2.5 · predicted delta 2.0 · real replay run (`run_id` starting `rp_`) 3.5, +1.0 if improved · self-assessment 1.5, +1.5 for ≥ 3 cycles, +0.5 for `downweighted` — capped at 8 |

### Human, 45 (judges)

| Pts | Criterion | What it asks |
|---|---|---|
| **18** | **Decision quality** | Could an operator act on each finding? Is every impact figure traceable to the data, **with the baseline named**? |
| **15** | **The screen** | Can a stranger read a finding, see the evidence, and approve or reject the fix? Does every number carry its definition? |
| **12** | **Framing** | Did the demo open on the problem, show the refusal, state coverage out loud, and admit what was faked? |

### Levels (measured, not estimated)

| Level | What you have done | Machine /55 |
|---|---|---|
| Start | Ran `starter.py`: two metrics, honest coverage, a valid file | 9.8 |
| **Level 1 — the target, by day 3** | One real problem found, quantified, explained. **Three lookalikes dismissed. The unanswerable question refused.** | **38.0** |
| Level 2 | Mined what "good" looks like from the deployment's own best conversations. One fix proposed and proved on the replay service. | 41.5 |
| Level 3 | Found the other problems too. A human decision recorded on each fix. The system keeps a record of its own hit rate. | 46.3 |

> **Level 1 with an excellent screen and decision layer ≈ 70/100. Level 3 with neither ≈ 58.**
> **Depth on one problem beats breadth across all eight report sections.** Bank Level 1, then optimise for the human 45.

### Where the points actually are (verified by running `score.py` on the reference reports)

The three example reports score **37.97 / 41.47 / 46.28** — the PDF's figures, confirmed. The per-section
breakdown is more useful than the totals:

| Section | L1 ref | L2 ref | L3 ref |
|---|---|---|---|
| Diagnostic accuracy /20 | 6.47 | 6.47 | **12.75** |
| Specificity /15 | **15.00** | **15.00** | 13.53 |
| Loop completeness /8 | 4.50 | **8.00** | **8.00** |
| Honesty /12 | **12.00** | **12.00** | **12.00** |

Four consequences worth planning around:

1. **Specificity + honesty = 27 of the 55 machine points, and the *Level 1* reference already scores
   both perfectly.** They need no fault detection at all — only correctly dismissed decoys and
   disciplined fidelity/coverage/calibration/gap fields. **This is the cheapest, most certain block of
   points in the event. Bank it first.**
2. **Loop completeness (8) is pure plumbing** — standard, linked IDs, a predicted delta, one real
   replay run, a self-assessment. The L2 reference gets 8/8 with the *same* single detection as L1.
3. **Diagnostic accuracy is where everyone is weak.** Even the **Level 3 reference misses `F3`
   (`prompt.regression`) entirely** and tops out at 12.75/20. Nothing in the kit demonstrates all three
   faults being found. **Detecting all three is worth ~7 points over the best reference report** and is
   the single biggest differentiator available — F3 is the fault that "never moves the outcome metric",
   so it needs a turns/latency-shaped detector, not a resolution-rate one.
4. Note the L3 reference *lost* 1.47 specificity by leaving `D2` (the load event) unexamined. Detecting
   more faults never excuses skipping a decoy dismissal.

---

## 4. What is planted in the data

| Planted | What it looks like | What we must do |
|---|---|---|
| **3 real problems** | (a) a new product launches with nothing about it in the knowledge base; (b) an API starts returning empty results **while reporting success** (HTTP 200); (c) a prompt change makes conversations twice as long without changing outcomes | Find it, name the slice of traffic it lives in, name the cause, point at the config change behind it |
| **3 lookalikes (decoys)** | a marketing campaign shifts the mix of questions; a flash sale spikes traffic and latency for four days; the quality-scoring rubric changes version halfway through | **Examine each and dismiss it in writing.** Flagging one as a regression costs more than missing a real problem. |
| **1 unanswerable question** | "What is our failover rate?" — the runtime has no failover mechanism, so no such event is ever logged | Refuse it, and specify the exact event that would need to exist to answer it |
| **2 denominator traps** | legacy v2 traffic logs no tool calls at all; one field is nearly unique per customer | State the true coverage of any tool metric; refuse the breakdown and cite the budget |

Each real problem is built to beat a lazy approach: **one has no "before" period** — only a comparison against the deployment's own best conversations catches it, which is why the `standard` section matters; **one is invisible in the obvious error-rate metric**; **one never moves the outcome metric at all**.

Ground-truth shape of the practice corpus (variant A): faults `F1 kb.gap`, `F2 tool.contract_break`, `F3 prompt.regression`; decoys `D1 traffic_mix_shift`, `D2 load_event`, `D3 judge_version_boundary`; coverage traps `C1`, `C2`; unanswerable `A11 → NOT_MEASURABLE`; fidelity traps `A09 → judged`, `A04 → measured`.

> ⚠️ **Never hard-code any of that.** The sealed day-6 corpus moves every one of them to different days, tenants and slices. `kit/ground_truth/` is for self-scoring only, never an input to the system.

---

## 5. A finding is a decision, not a number

*"Resolution in cohort X fell from 0.86 to 0.31"* is true and useless. Every reported regression must carry:

- **How many conversations** it affected, and what **share of traffic** that is.
- **What happened to those users** — how many would have resolved and did not; how many reached a human who was never meant to be involved.
- **How long** it has been running, and **what it has cost**.
- **Who has to act** — `agent_builder`, `business_owner`, or `platform_owner`.
- **What happens if nobody acts** — one sentence, in plain words.
- **How you computed it** — a derivation someone can check against the same data, **naming the baseline you chose and why**. One problem has no before-period, so its baseline must be **peer traffic**. Another looks identical to its peers and is only visible **against its own past**.

Every figure is **computed from the data. Nothing is asserted.**

A proposed fix is framed as a decision: what a human is being asked to approve, the risk if the diagnosis is wrong, and what would make us not ship it. Then an **approve/reject** control on the screen, and the decision recorded back into the report. **A rejection with a good reason scores as well as an approval.**

---

## 6. The report contract — `loop-report.json`

Validate against `tools/nexus-loop-kit/schema/loop-report.schema.json`. Top-level required keys: `team`, `corpus`, `metrics`, `findings`, `diagnoses`, `gaps`.

**Required sections (4):**

*Required by the schema* is in **bold**; the rest is optional but scored or judged.

1. **`metrics`** (**minItems 1**) — every metric the system authored. Each requires **`id`, `ask_id`,
   `fidelity`, `coverage`, `plan`**; `coverage` requires **`value` + `basis`** (`excluded` optional);
   `plan` requires **`source` + `filter` + `denominator`** (`breakdowns`, `alternatives_offered`
   optional). `calibration {agreement, n, judge_version}` is optional to the schema but **required in
   practice on every judged metric** — it is worth 2.0, and omitting it reads as a Rule 1 dodge.
   Optional `name`, `grain`.
2. **`findings`** — what moved. Each requires **`id`, `tenant`, `cohort`, `metric`, `window`
   (`from_day` + `to_day`), `is_regression`**. **Must include what we decided was *not* a problem**
   (`is_regression: false` plus `not_a_regression_because`) — that is how specificity is earned.
   When `is_regression: true`, the schema *conditionally requires* **`impact`, `audience`
   (minItems 1), `if_nothing_changes`**; inside `impact`, **`conversations_affected`,
   `share_of_traffic`, `derivation`** are required and `downstream`, `cost_usd` (null is an honest
   answer), `days_running` are optional.
3. **`diagnoses`** — why it happened. Each requires **`id`, `finding_id`, `cause_class`** from the
   closed set (`kb.gap`, `tool.contract_break`, `tool.outage`, `prompt.regression`, `model.change`,
   `routing.error`, `traffic_mix`, `load`, `judge_change`, `unknown`). `confidence`, `evidence` and
   `attributed_change {kind, day}` are optional — but `attributed_change` is what the accuracy
   bonus reads, and the config change we blame is an explicit demo item.
4. **`gaps`** — what could not be measured. Each requires **`ask_id`, `verdict`, `why`**; verdicts are
   `NOT_MEASURABLE`, `REQUIRES_NEW_JUDGE`, `COVERAGE_TOO_LOW`, `CARDINALITY_REFUSED`.
   `nearest_proxy`, `why_the_proxy_misleads` and `required_event {name, grain, fields, owner}` are
   schema-optional but carry **2.0 of the honesty points**, so treat them as required.
   *This is a deliverable, not an apology.*

**Optional sections — where the remaining machine points live:** `standard`, `prescriptions`, `verifications`, `self_assessment`. Also set `system_notes` (what is real, what is stubbed).

Optional per-finding fields worth filling for the human judges: `observed`, `expected`, `severity`
(`low`/`medium`/`high`/`critical`), `evidence`. `window` requires both `from_day` and `to_day`.

**Dismissal recipe, for full specificity credit** — all four parts, or the credit does not register:
a finding whose `tenant` and `window` overlap the decoy, with `is_regression: false`, a
`not_a_regression_because` string, **and** a linked diagnosis whose `cause_class` is exactly
`traffic_mix` (D1), `load` (D2), or `judge_change` (D3).

⚠️ **Ordering gotcha — verified experimentally.** For the fidelity traps (A04, A09) the scorer reads
**only the first metric in the `metrics` array carrying that `ask_id`**. The L1 reference declares
`m_tool_fail` (`measured`) before `m_useful_tool` (`derived`) for A04; swapping those two turns
`+0.75` into `−1.0`. **For any ask with a fidelity trap, list the canonical `measured` metric first.**

---

## 7. The data and the catalog

`kit/` — practice corpus, variant A, 56 days from `2026-06-01`, seed 42, tenants `acme-bank` and `northwind-retail`.

| Path | What it is |
|---|---|
| `kit/corpus/sessions.jsonl.gz` | 80,252 conversations — one row per conversation |
| `kit/corpus/agent_steps.jsonl.gz` | 883,764 steps — **the fact grain** (turn 365k, llm_call 369k, tool_call 62k, kb_lookup 72k, handoff 12k, guardrail 4k) |
| `kit/corpus/turns.jsonl.gz` | 365,065 transcript turns — text only |
| `kit/corpus/config_timeline.csv` | 16 timestamped config changes |
| `kit/corpus/feedback.csv` | sparse CSAT, ~8.6% of sessions, **not missing at random** |
| `kit/corpus_sample/` | whole-session ~5% slice — iterate here, **confirm on the full corpus before reporting** |
| `kit/catalog.json` | **Read this before writing any query.** Entities, grains, join validity, field semantics, capability + coverage declarations, cardinality budgets, milestones |
| `kit/asks.md` | the eleven operator asks |
| `kit/labels/outcome_labels.jsonl` | 400 human "was this resolved" labels |
| `kit/labels/rubric_scores.jsonl` | 96 human 1–5 quality scores — the calibration source |
| `kit/ground_truth/` | the practice answer key — self-scoring only |

**Field semantics that matter (from the catalog):**

- `step.outcome = 'ok'` means the call *returned without an error*. **It does not mean it returned anything useful.** A 200 with an empty body is a successful call and a failed answer.
- `step.response_bytes` and `step.result_field_count` are the **only** fields that distinguish a useful 200 from an empty one. `result_field_count = 0` on a 200 means the agent got nothing to answer with. This is how the fault that is invisible in error rate gets caught.
- `step.retry_count > 0` is a **quality retry at a different temperature**, *not* a failover. Reporting it as failover under-reports and reads as "0% failover" — i.e. as good news.
- `session.agent_kind ∈ {v3_agent, v2_flow}`. **`v2_flow` emits no `tool_call`, `kb_lookup` or `guardrail` steps** — that is the coverage hole. v2 share: acme-bank 28%, northwind-retail 7%.
- `session.handoff_by_design` — containment is a *filter over this flag*, not a philosophical argument.
- `session.quality_score` is 1–5 and comparable **only within one `judge_version`** (`v1` / `v2`).
- `step.milestone` is the unit journeys, funnels and drop-off are built from; per-intent milestone sequences live in `catalog.milestones`.
- A quality movement **with no change marker near it** is usually a **mix shift, not a regression**.

**The config timeline, verified** — 16 rows: `kb` 4, `prompt` 4, `tool` 3, `model` 2, `routing` 2, `judge` 1.
Exactly **one row has `tenant = '*'`**, and it is the judge one (in variant A: day 28, `quality_rubric`
v1 → v2, *"stricter quality rubric rolled out"*). That single row is both the decoy `D3` boundary and the
row an equality-only tenant join silently drops. The two `model` rows are the two upgrades A07 asks about.
**Handle the wildcard structurally; never hard-code day 28** — the sealed corpus moves it.

**Declared capabilities and their true coverage:**

- `containment_rate` (measured, session, coverage 1.00) — *sessions ending `resolved`, plus handoffs with `handoff_by_design = true`, over all sessions*
- `resolution_rate`, `turns_to_resolve`, `milestone_reached_rate` — measured, coverage 1.00
- `quality_score` — **judged**, calibratable against `labels/rubric_scores.jsonl`
- `tool_failure_rate`, `cost_per_session`, `kb_fallthrough_rate` — measured, **coverage acme-bank 0.72 / northwind-retail 0.93** (the v2 hole)
- `silent_tool_success` — **derivable, not declared**; deriving and declaring it is a legitimate answer, not a gap
- `failover_rate` — **NOT_MEASURABLE**
- `abandonment_reason` — **requires a new judge**

---

## 8. The eleven asks — `kit/asks.md`

**Four are required: A01, A04, A09, A11.** For each ask the system must either answer it or explain exactly why it cannot.

| Ask | Shape / the trap |
|---|---|
| **A01** ✱ | containment_rate — clean canonical; the only trap is by-design handoffs |
| A02 | ambiguous: on which metric, over which cohort? A mix shift answers it wrongly unless you stratify |
| A03 | measured, but the denominator excludes v2 traffic — **state coverage** |
| **A04** ✱ | measured. **Answering with a model is a Rule 1 violation.** |
| A05 | milestone drop-off — needs the journey definition, not a transcript read |
| A06 | measured via `kb_hit` / `kb_top_score`, but "actually answering" also has a judged reading — **offer both** |
| A07 | needs change markers **and** a stable cohort; two model upgrades exist, one near a judge-version boundary |
| A08 | measured, coverage-limited to v3 |
| **A09** ✱ | abandonment is **measured**; the **reason is judged**. Classifying it as measured is wrong (−1.0) |
| A10 | open — a good answer composes a deterministic scope with a judged score |
| **A11** ✱ | **NOT MEASURABLE.** The only correct output is a gap spec |

Required event for the A11 gap spec: name `failover`, grain `step`, fields `from_target`, `to_target`, `reason`, `recovered`, owner `conversation-runtime`.

---

## 9. Tools, commands, and the replay budget

Plain Python 3.9+, no dependencies.

```bash
cd nexus-loop-day1/tools/nexus-loop-kit

make kit                                          # regenerate the practice corpus (~20 s)
python3 starter.py --kit ../../kit --team <team> --out my-loop-report.json
python3 score.py   --report my-loop-report.json \
                   --ground-truth ../../kit/ground_truth/ground_truth.json
python3 score.py   --report ... --ground-truth ... --json      # machine-readable
python3 replay/serve.py --kit ../../kit --port 8719     # or: make serve
```

On this machine the interpreter is `python`, not `python3` (Python 3.12.10).

⚠️ **Two tooling facts that are easy to get wrong:**

- **`verify.py --kit <dir>` verifies the *corpus*, not our report.** It is an organiser tool that checks
  a generated (especially sealed) corpus is still teachable. It will never tell us our deliverable is valid.
- **Nothing in the kit validates `loop-report.json` against the schema.** `starter.py` only *claims* its
  output is schema-valid. **We must write our own validator** (stdlib only — do not assume `jsonschema`
  is installed) and run it in the pipeline, or the sealed run can emit a file that fails the contract.
  `score.py` will happily score a structurally invalid report and silently give zeros.

**Replay service — `POST /replay`:**

```json
{"team": "...", "tenant": "acme-bank",
 "change": {"type": "kb.add", "target": "...", "description": "..."},
 "cohort": {"intent": "order_status", "from_day": 12, "to_day": 20},
 "golden_set": ["s_...", "s_..."]}
```

Returns `run_id` (`rp_...`), `before`, `after`, `delta`, `verdict ∈ {improved, no_effect, regressed}`, and a `golden_set` regression guard.

- **40 runs per team. A hard budget, and every call is logged (`GET /log`).**
- **A wrong fix returning `no_effect` is a result, not an error.** "Nothing happened" is reported identically whether the fix was wrong or the cohort was healthy — the endpoint verifies a hypothesis you already have; **it will not hand you a map**.
- **Do not brute-force it. Diagnose first.** `revert` and `routing.change` are blunt instruments and carry a much higher chance of breaking the golden set.
- Always send a `golden_set` — a fix that helps the cohort while breaking known-good conversations must be caught. Blunt changes carry ~**6%** per-session break risk against ~0.2–0.4% for a targeted one.
- A `verifications` entry only scores if its `replay_run_id` starts with `rp_` and its `prescription_id` links to a real prescription.

**How the endpoint actually decides — read this before spending a single run:**

1. **The result is a deterministic hash of `(tenant, change, cohort)`.** Repeating an identical request
   returns an identical answer and still costs a run. **Never re-send the same call.**
2. **Three things must all line up to get `improved`:** the `tenant` must match; the `cohort` must
   **name the fault's own key and value** (e.g. the right `intent`) — any one matching key is enough,
   but a wrong or missing one silently yields `no_effect`; and `from_day`/`to_day` must **overlap the
   fault window**.
3. **`change.type` must be in that fault's accepted fix classes.** The most direct class gives a 0.92
   lift, an accepted-but-partial one 0.70, anything else **zero**. A correct diagnosis paired with the
   wrong `change_type` is indistinguishable from a wrong diagnosis.
4. **We do not choose the verification metric — the fault does.** Prompt-class faults are verified on
   `median_turns`; everything else on `resolution_rate`. A movement smaller than 0.15 turns / 0.02 rate
   is reported as `no_effect` regardless.

The practical consequence: **one well-diagnosed call beats ten probes**, and a `no_effect` tells us
nothing about *which* of the four conditions we got wrong.

---

## 10. Day 6 — the sealed run

- **At 12:00 on day 6 everything freezes.** The system runs **untouched** on a second dataset with the same kinds of problems planted on **different days, different tenants, different slices**. Its answer key is held by the organisers. **That output is what gets scored.**
- **A system that needs hand-holding to run scores as if it failed.**

Standing engineering constraints from day 1, because of the above:

- **No hard-coded days, tenants, intents, thresholds, or fault IDs.** Everything derived at runtime.
- **One command, end to end**, taking the corpus path as an argument and emitting a valid `loop-report.json`.
- Read `manifest.json` and `catalog.json` at runtime; never assume variant A's numbers.
- Fail loudly and honestly rather than silently emitting a plausible-looking number.
- Deterministic, re-runnable, no interactive prompts anywhere in the pipeline.

**Submission — three things:**

1. `loop-report.json` from the sealed run, **produced by the system without edits**.
2. **The system plus the screen**, with a working **APPROVE / REJECT** control whose decision is written back into the report.
3. **A one-page note**: what is **real**, what is **stubbed**, what is **simulated**, and what **breaks at 100× the scale**.

> **Faking is fine in six days. Hiding it is not.**

---

## 11. The demo — 10 minutes, then 5 of questions

Open on the problem **from the operator's side**, not on our architecture. Show, in order: the finding and its impact → the lookalike we correctly ignored → the diagnosis → the config change behind it → the proposed fix → proof that it works → the approval gate → the one question the system refused to answer → **why** it refused.

> **The refusal is the most memorable thirty seconds we have.**

---

## 12. The six days

| Day | Focus | The bar |
|---|---|---|
| 1 | Read the catalog. Get one metric computing. | One number on screen, with fidelity and coverage attached |
| 2 | Emit a valid report, however thin. Start mining the standard. | `score.py` returns a number. Any number. |
| 3 | Detection and diagnosis. Meet the lookalikes. | A scorecard on the board: detections and false alarms, no narrative. **Level 1 by end of day 3.** |
| 4 | One fix, proposed and replayed. | Before and after on the affected cohort |
| 5 | The screen and the approval gate. | The full loop runs on one problem with no human except the approval |
| 6 | **12:00 freeze. Sealed run.** | 15:00 demos — ten minutes to present, five of questions |

**The first four hours, from the PS:** run `make kit`, then `starter.py`, then `score.py` — you are on the board before writing a line. Then read `catalog.json` end to end, `asks.md`, and `starter.py`. **Do not code yet.** Then take **A11 first**: work out from the catalog that it cannot be answered and write the gap spec — it is the cheapest real scoring in the event, and it teaches Rule 1 better than any explanation. Then, on the 5% sample, group resolution rate by intent, per tenant, per week. *Something will stand out. Do not trust it yet — three of the things that stand out are not problems.*

---

## 13. Definition of done for any change

Check every box before saying a piece of work is finished.

- [ ] **Rule 1**: no LLM used for anything the logs record. Every judge versioned and calibrated.
- [ ] **Rule 2**: no metric definition, judge version, or golden set was altered.
- [ ] Every metric carries `fidelity` **and** `coverage {value, basis}`; judged ones carry a `calibration` that is **not** ≥ 0.995.
- [ ] Tool, cost and KB metrics **exclude `v2_flow`** from the denominator and say so (≈ 0.72 / 0.93).
- [ ] No join violates the catalog: `config_change.tenant = '*'` handled, `kind='tool'` filter applied, step measures re-aggregated to session grain before averaging.
- [ ] Any breakdown over its cardinality budget is **refused, with the budget cited**.
- [ ] Every `is_regression: true` finding has `impact` (with a `derivation` naming the baseline), `audience`, and `if_nothing_changes`.
- [ ] Decoys appear as `is_regression: false` + `not_a_regression_because` + a diagnosis with the matching `cause_class`.
- [ ] A11 is present in `gaps` as `NOT_MEASURABLE`, with the `required_event` fields, `nearest_proxy` and `why_the_proxy_misleads`.
- [ ] For A04 and A09, the **canonical `measured`/`judged` metric is listed first** among that ask's metrics.
- [ ] At least one **tool-derived** metric is declared (otherwise the 3.0 coverage-trap credit is forfeited).
- [ ] The report passes **our own schema validator** (the kit ships none) and `score.py` runs clean.
- [ ] Nothing hard-coded to variant A; the pipeline runs unattended against an unseen corpus path.
- [ ] `system_notes` honestly states what is real and what is stubbed.

---

## 14. Anti-patterns that lose this hackathon

1. Asking an LLM what the logs already say. *(disqualifying)*
2. Moving the yardstick to make a fix look good. *(disqualifying)*
3. Flagging a decoy as a regression. *(−4.2 each — costs more than a miss)*
4. Staying silent to be safe — an **empty `findings` array scores 0** on specificity. (Dismissals alone can still earn the full 15; what is never rewarded is not looking.)
5. Reporting a number with no fidelity or coverage, or a calibration of 1.00.
6. Averaging step measures over sessions without re-aggregating to session grain.
7. Trending `quality_score` across a judge-version boundary.
8. Answering A11 with `retry_count` as a proxy.
9. Reading transcripts to decide *what happened* rather than *what it meant*.
10. Brute-forcing the replay endpoint instead of diagnosing — 40 runs, all logged.
11. Breadth across all eight sections instead of **depth on one problem**.
12. A pipeline that needs a human to nurse it through the sealed run.

---

## 15. Current state

- `nexus-loop-day1/` holds the day-1 bundle: `kit/` (practice corpus, variant A) and `tools/nexus-loop-kit/`.
- `tools/nexus-loop-kit/my-loop-report.json` is **starter output only** — team `your-team`, 3 metrics, no findings, no gaps, no prescriptions. Everything from section 6 onward is still to be built.
- Reference reports to score ourselves against: `tools/nexus-loop-kit/examples/loop-report.level{1,2,3}.json`. Verified on this machine: they score **37.97 / 41.47 / 46.28** of 55.
- Interpreter is `python` (3.12.10). `make` targets assume `python3`; call the scripts directly if `make` is unavailable.
- Not yet built: our own schema validator, the detection/diagnosis pipeline, the screen with the approve/reject gate, and the one-page note.
