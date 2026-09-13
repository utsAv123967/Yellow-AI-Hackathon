# Nexus Loop — Full Approach Explained Step by Step

---

## Table of Contents

1. [What Are We Actually Building?](#1-what-are-we-actually-building)
2. [The Data We're Given](#2-the-data-were-given)
3. [Step 1 — Read and Index the Data](#3-step-1--read-and-index-the-data)
4. [Step 2 — Compute Metrics (With Honest Coverage)](#4-step-2--compute-metrics-with-honest-coverage)
5. [Step 3 — Detect Anomalies (Find Candidate Problems)](#5-step-3--detect-anomalies-find-candidate-problems)
6. [Step 4 — Filter Out False Alarms (The Lookalikes)](#6-step-4--filter-out-false-alarms-the-lookalikes)
7. [Step 5 — Diagnose Root Causes](#7-step-5--diagnose-root-causes)
8. [Step 6 — Assess Business Impact](#8-step-6--assess-business-impact)
9. [Step 7 — Propose Fixes](#9-step-7--propose-fixes)
10. [Step 8 — Verify Fixes Using the Replay Service](#10-step-8--verify-fixes-using-the-replay-service)
11. [Step 9 — Handle Unanswerable Questions (Gaps)](#11-step-9--handle-unanswerable-questions-gaps)
12. [Step 10 — Mine What "Good" Looks Like (Standard)](#12-step-10--mine-what-good-looks-like-standard)
13. [Step 11 — Build the Operator UI](#13-step-11--build-the-operator-ui)
14. [Step 12 — Assemble the Report](#14-step-12--assemble-the-report)
15. [Step 13 — Self-Assessment](#15-step-13--self-assessment)
16. [Step 14 — Score and Iterate](#16-step-14--score-and-iterate)
17. [How the Scoring Works](#17-how-the-scoring-works)
18. [The Sealed Run — Why Nothing Can Be Hardcoded](#18-the-sealed-run--why-nothing-can-be-hardcoded)
19. [End-to-End Flow in One Diagram](#19-end-to-end-flow-in-one-diagram)

---

## 1. What Are We Actually Building?

Imagine you're a company running an AI chatbot that handles customer support on WhatsApp. The bot talks to thousands of customers every day. Sometimes it works great — it answers questions, looks up orders, solves problems. Sometimes it fails — it gives wrong answers, can't find information, or sends the customer to a human agent unnecessarily.

Right now, the only way problems get discovered is:

```
Customer has a bad experience
     ↓
Customer complains
     ↓
A human notices the complaint
     ↓
The human manually reads chat transcripts
     ↓
The human guesses what went wrong
     ↓
The human changes something
     ↓
Everyone hopes it worked
```

**We're building the system that replaces this manual loop.** Our system automatically:

1. **Reads** the bot's logs (80,000 conversations over 8 weeks)
2. **Detects** when something went wrong
3. **Distinguishes** real problems from false alarms
4. **Diagnoses** why it went wrong
5. **Measures** how bad it is (business impact)
6. **Proposes** a specific fix
7. **Tests** whether the fix actually works (using a replay service)
8. **Shows** everything to a human operator on a screen
9. **Records** the human's approve/reject decision

The output is two things:
- A **`loop-report.json`** file that captures everything above in a specific schema
- An **operator UI** where a human can see findings and approve/reject fixes

---

## 2. The Data We're Given

We have 5 data files representing 8 weeks of a real (simulated) AI bot deployment:

### `sessions.jsonl.gz` — 80,000 conversations

Every conversation the bot had. Each session looks like:

```json
{
  "session_id": "s_acme_004231",
  "tenant": "acme-bank",
  "intent": "product_info",
  "session_end": "resolved",
  "handoff_by_design": false,
  "agent_kind": "v3_agent",
  "day": 15,
  "num_turns": 4,
  "quality_score": 4.2,
  "judge_version": "v1",
  "csat": 4,
  "cost_usd": 0.032
}
```

Key fields:
- **`session_end`**: Did the bot solve it (`resolved`), pass to a human (`handoff`), or did the customer give up (`abandoned`)?
- **`tenant`**: Which company — `acme-bank` (financial services) or `northwind-retail` (e-commerce)
- **`intent`**: What the customer was asking about — `order_status`, `product_info`, `premium_card_info`, etc.
- **`agent_kind`**: `v3_agent` (modern, uses tools/KB) or `v2_flow` (legacy, uses NO tools/KB)
- **`day`**: Day 0–55 (8 weeks)

### `agent_steps.jsonl.gz` — 880,000 execution steps

Every action the bot took inside each conversation. Each step looks like:

```json
{
  "session_id": "s_nw_012345",
  "step_type": "tool_call",
  "tool_name": "get_order_status",
  "outcome": "ok",
  "status_code": 200,
  "response_bytes": 2,
  "result_field_count": 0,
  "duration_ms": 145
}
```

Step types: `turn`, `llm_call`, `tool_call`, `kb_lookup`, `routing`, `guardrail`, `handoff`

Key insight: **`outcome: "ok"` does NOT mean the API returned useful data.** You also have to check `response_bytes` and `result_field_count`. An API can return HTTP 200 with an empty body — that's a silent failure.

### `config_timeline.csv` — 16 configuration changes

```csv
day,tenant,kind,target,description
28,*,judge,quality_rubric,"quality rubric v1 → v2"
34,acme-bank,kb,kb_2026_06_c,"premium card launch content pack"
40,northwind-retail,tool,get_order_status,"order service migration 3.2 → 3.3"
46,acme-bank,prompt,p_v4,"safety and confirmation wording"
```

These are the deployment changes made over 8 weeks. Some cause problems, some don't.

### `feedback.csv` — Sparse customer ratings

Only ~8.6% of sessions have feedback. Contains CSAT scores (1–5) and `rubric_version` (v1 or v2 — changes partway through).

### `labels/` — Human evaluation labels

- ~400 human outcome labels (`human_resolved: true/false`)
- ~96 human quality scores (1–5 scale)

These have ~7.5% disagreement rate built in (realistic human annotator noise).

---

## 3. Step 1 — Read and Index the Data

**What we do:** Stream through all the compressed JSON files and build in-memory indices.

```python
# Read sessions
sessions = []
for line in gzip.open("corpus/sessions.jsonl.gz"):
    session = json.loads(line)
    sessions.append(session)

# Build lookup indices
sessions_by_tenant = group_by(sessions, key=lambda s: s["tenant"])
sessions_by_intent = group_by(sessions, key=lambda s: s["intent"])
sessions_by_week   = group_by(sessions, key=lambda s: s["day"] // 7)
```

We also parse the config timeline and sort it by day, so later we can ask "what config change happened near day X?"

**Why this matters:** All later analysis works by slicing and filtering these indices. We never hardcode which tenants or intents exist — we discover them from the data.

> **Coverage Trap Alert:** 28% of acme-bank sessions are `agent_kind: "v2_flow"`. These legacy sessions emit **zero** tool_call, kb_lookup, or llm_call steps. If we count "tool failures" using all sessions as the denominator, we'd be saying "this tenant has very few tool failures" when really 28% of their traffic never uses tools at all. We must separate v3 and v2 traffic and only include v3 sessions when computing tool/KB metrics.

---

## 4. Step 2 — Compute Metrics (With Honest Coverage)

We compute every metric deterministically from the logs. Every metric has three properties:

| Property | Meaning | Example |
|----------|---------|---------|
| **Fidelity** | How was this number obtained? | `measured` (from logs), `judged` (by a model), `derived` (calculated from other metrics) |
| **Coverage** | What % of traffic does this metric actually describe? | Tool failure rate has coverage 0.72 for acme-bank (28% v2_flow excluded) |
| **Calibration** | If judged, how well does the judge agree with humans? | Agreement 0.87 against 96 human labels |

### Metrics we compute:

**1. Containment Rate** (answers Ask A01)
```
containment = (sessions where session_end == "resolved"
            OR (session_end == "handoff" AND handoff_by_design == true))
            / total sessions
```
- Fidelity: `measured` (directly from log fields)
- Coverage: `1.0` (every session has `session_end`)
- This is the primary "is the bot working?" metric

**2. Tool Failure Rate** (answers Ask A04)
```
tool_failures = steps where step_type == "tool_call" AND outcome IN ("error", "timeout")
             / all steps where step_type == "tool_call"
```
- Fidelity: `measured`
- Coverage: `0.72` for acme-bank (excludes v2_flow sessions that have zero tool calls)
- **Critical rule:** This must be measured, not judged. Using an LLM to determine "did the tool fail?" when the log already says `outcome: "error"` is a fidelity violation that costs points.

**3. Silent Tool Empty Rate** (key for detecting Problem F2)
```
silent_empty = steps where step_type == "tool_call"
               AND outcome == "ok"
               AND response_bytes <= 2
               AND result_field_count == 0
             / all tool_call steps where outcome == "ok"
```
- This catches APIs that return HTTP 200 (success!) but with an empty body
- Standard error monitoring completely misses this

**4. KB Miss Rate** (key for detecting Problem F1)
```
kb_miss = steps where step_type == "kb_lookup" AND kb_hit == false
        / all steps where step_type == "kb_lookup"
```
- Per intent: if one intent has 100% kb miss, the knowledge base doesn't have articles for that topic

**5. Turns to Resolve** (answers Ask A02, key for detecting Problem F3)
```
median_turns = median(num_turns) for sessions where session_end == "resolved"
```

**6. Quality Score** (answers Ask A06)
```
avg_quality = mean(quality_score)
```
- Fidelity: `judged` (not measured — it's the output of an automated judge)
- **Must be segmented by `judge_version`** — never compare v1 scores to v2 scores
- Calibration: compare against 96 human rubric scores, expect ~0.87 agreement

**7. Other metrics:** handoff rate, CSAT (sparse, ~8.6% coverage), cost per session, etc.

### Segmentation

We compute every metric across multiple dimensions:
- **Global** (all traffic)
- **By tenant** (acme-bank vs northwind-retail)
- **By intent** (order_status, product_info, premium_card_info, etc.)
- **By week** (week 0 through week 7)
- **By tenant × week** (time-series per tenant)
- **By intent × week** (time-series per intent)

This gives us a "metric cube" — a multi-dimensional view where we can spot which specific slice of traffic changed and when.

---

## 5. Step 3 — Detect Anomalies (Find Candidate Problems)

Now we scan the metric cube for significant changes. We're looking for any segment where a metric shifted substantially.

### Detection approach: Simple Probes

With only 8 weekly data points per time-series, sophisticated statistical methods like CUSUM are fragile. Instead, we use straightforward heuristic probes:

**Probe 1: Resolution drop**
```
For each (tenant, intent) combination:
    Compare resolution rate in weeks 0-3 (first half) vs weeks 4-7 (second half)
    If drop > 10 percentage points → flag as candidate
```

**Probe 2: Tool payload change**
```
For each (tenant, tool_name) combination:
    Check if silent_empty_rate appeared or increased substantially
    If > 5% of successful calls return empty → flag as candidate
```

**Probe 3: Turn inflation**
```
For each (tenant, agent_id) combination:
    Compare median turns before vs after each config change
    If turns increased > 30% with no resolution improvement → flag as candidate
```

**Probe 4: KB miss spike**
```
For each (tenant, intent) combination:
    If kb_miss_rate > 80% for any intent → flag as candidate
```

**Probe 5: Volume/latency spike**
```
For any period where session count > 3× normal AND latency p95 > 1.8× normal:
    Flag as candidate (but likely a false alarm — probe further)
```

**Probe 6: Traffic composition shift**
```
For each tenant:
    If any single intent's share of traffic changed > 2× AND per-intent resolution rates are stable:
    Flag as candidate (but likely a false alarm)
```

**Probe 7: Quality score drop**
```
If quality_score drops > 0.4 AND it coincides with a judge_version change:
    Flag as candidate (but likely a false alarm)
```

Each probe produces **candidate findings**. The next step separates real problems from noise.

---

## 6. Step 4 — Filter Out False Alarms (The Lookalikes)

The dataset deliberately contains 3 situations that **look** like regressions but aren't. The scoring heavily penalizes false alarms (4.2 points lost per false alarm on a lookalike), but **rewards explicit dismissals** (+3 points for correctly explaining why something is NOT a problem).

### How we filter each candidate:

For every candidate finding, we run it through a series of causal tests:

```
Candidate: "Resolution rate dropped in week W for tenant T"
    │
    ├─ Test 1: Did per-intent resolution rates change, or just the mix?
    │   └─ If per-intent rates are all stable but aggregate moved:
    │       → DISMISS as "traffic_mix" (Lookalike D1)
    │
    ├─ Test 2: Was there a volume/latency spike that self-corrected?
    │   └─ If volume > 3× AND latency > 1.8× AND resolution unchanged:
    │       → DISMISS as "load" (Lookalike D2)
    │
    ├─ Test 3: Did the judge rubric version change?
    │   └─ If quality_score dropped but session_end distribution is unchanged:
    │       → DISMISS as "judge_change" (Lookalike D3)
    │
    └─ If none of these explain it:
        → CONFIRM as a real regression
```

### The Three Lookalikes in Detail:

**D1 — Traffic Mix Shift (Days 22–32)**

What happened: The `branch_locator` intent at acme-bank suddenly gets 5× more traffic (a marketing campaign). Since `branch_locator` is an easy intent with high resolution, the aggregate resolution rate actually **goes up**. But no individual intent's resolution changed.

How we detect it's fake: Check every intent's resolution rate individually. All are flat. Only the **mix** changed.

Our dismissal:
```json
{
  "id": "f_d1",
  "tenant": "acme-bank",
  "cohort": {"intent": "branch_locator"},
  "is_regression": false,
  "not_a_regression_because": "branch_locator share went from 13% to 44%. Every per-intent resolution rate is flat. This is a traffic composition change, not a quality change."
}
```
With linked diagnosis: `cause_class: "traffic_mix"`

**D2 — Flash Sale / Load Spike (Days 12–16)**

What happened: A flash sale causes 4× traffic volume. Tool latency p95 doubles. But resolution rates are completely unchanged — the bot still works, just slower.

How we detect it's fake: Resolution is unaffected. Latency spike is temporary and self-corrects.

Our dismissal: `cause_class: "load"`

**D3 — Judge Rubric Change (Day 28)**

What happened: The quality scoring rubric changes from v1 to v2. Quality scores drop ~0.6 points overnight across BOTH tenants, ALL intents, ALL channels. But `session_end` outcomes (resolved/handoff/abandoned) are completely unchanged.

How we detect it's fake: The drop is identical across every dimension simultaneously, and aligns exactly with a `judge` config change. Raw outcomes didn't change — only the measuring stick did.

Our dismissal: `cause_class: "judge_change"`

### How dismissals are recorded in the report:

Dismissed lookalikes are NOT in a separate section. They're regular `findings` entries with `is_regression: false` plus a `not_a_regression_because` explanation, AND a linked `diagnoses` entry with the correct `cause_class`. This is how `score.py` identifies them for specificity credit.

---

## 7. Step 5 — Diagnose Root Causes

For each **confirmed** real problem, we trace the causal chain back to a specific configuration change.

### The Three Real Problems:

**F1 — Knowledge Base Gap: `premium_card_info` (Days 34–45, acme-bank)**

```
Day 34: Config change — "premium card launch content pack" for acme-bank
    ↓
New intent "premium_card_info" starts appearing in traffic
    ↓
Bot does kb_lookup for this intent → kb_hit = false EVERY time
    ↓
kb_top_score is 0.11–0.38 (way below the 0.79 tenant average)
    ↓
Bot has no knowledge to answer these questions
    ↓
Resolution rate for this intent: ~31% (vs 82% for peer intents)
    ↓
Most sessions end in unplanned handoff or abandonment
```

**Diagnosis:**
- `cause_class`: `"kb.gap"` — a knowledge gap
- `attributed_change`: `{kind: "kb", day: 34}` — the config change that launched the product without adding KB articles
- `confidence`: 0.91

**Baseline choice: Peer cohort**, not before/after. This intent is NEW — it didn't exist before day 34. So we compare against other question-answering intents on the same agent in the same time period.

---

**F2 — Silent Tool Failure: `get_order_status` (Days 40–52, northwind-retail)**

```
Day 40: Config change — "order service migration 3.2.0 → 3.3.0" for northwind-retail
    ↓
get_order_status API starts returning HTTP 200 (success!) but with EMPTY body
    ↓
response_bytes = 2 (just "{}"), result_field_count = 0
    ↓
outcome = "ok" — standard error monitoring sees NOTHING wrong
    ↓
tool_failure_rate stays FLAT (this is the trap!)
    ↓
But the bot gets no data back → can't tell customer their order status
    ↓
Bot retries the same tool call → still empty → eventually fails
    ↓
Resolution rate drops from 86% to 77%
```

**Diagnosis:**
- `cause_class`: `"tool.contract_break"` — the API contract changed silently
- `attributed_change`: `{kind: "tool", day: 40}`
- `confidence`: 0.84

**Baseline choice: Before/after** — same tool's behavior before day 40.

**Why this is the hardest problem to detect:** Standard monitoring only looks at `outcome == "error"`. This API returns `outcome == "ok"` with `status_code == 200`. The ONLY way to catch it is by inspecting `response_bytes` and `result_field_count`. This is our **content-aware metric**.

---

**F3 — Prompt Verbosity: Over-Confirmation (Days 46–56, acme-bank)**

```
Day 46: Config change — "safety and confirmation wording" prompt rewrite
    ↓
The bot starts over-confirming everything — asking "just to confirm, you want X?"
    ↓
Median turns per conversation nearly doubles (4 → 7+)
    ↓
Cost per session increases ~58%
    ↓
But resolution rate is FLAT — the bot still eventually solves the problem
    ↓
No outcome improvement, just wasted effort and cost
```

**Diagnosis:**
- `cause_class`: `"prompt.regression"` — the prompt change made things worse
- `attributed_change`: `{kind: "prompt", day: 46}`
- `confidence`: 0.88

**Baseline choice: Before/after** — same agent's turns before day 46.

---

## 8. Step 6 — Assess Business Impact

Every real finding needs a quantified impact block. No vague statements — concrete numbers derived from the data.

Example for F1 (KB gap):
```json
{
  "impact": {
    "conversations_affected": 239,
    "share_of_traffic": 0.042,
    "downstream": {
      "would_have_resolved_at_baseline": 120,
      "unplanned_handoffs": 149,
      "abandoned": 15
    },
    "cost_usd": 13.58,
    "days_running": 11,
    "derivation": "239 sessions matched the cohort over 11 days (4.2% of acme-bank traffic in the window). Observed resolution 0.314 against a baseline of 0.816 (peer intents on same agent in same window). 120 conversations that would have resolved did not. 149 ended in unplanned handoff; 15 were abandoned."
  }
}
```

The `derivation` field is crucial — it explains exactly how every number was calculated so a human can audit it.

We also specify:
- **`audience`**: Who needs to act? (`agent_builder`, `business_owner`, `platform_owner`)
- **`if_nothing_changes`**: "At the observed rate, ~11 unresolved conversations per day. These are first impressions of a new product line."

---

## 9. Step 7 — Propose Fixes

For each diagnosed problem, we propose a specific, actionable fix:

| Problem | Fix Type | What We Propose |
|---------|----------|-----------------|
| F1 (KB gap) | `kb.add` | Add the missing premium card product documentation — fees, eligibility, benefits |
| F2 (Silent API) | `tool.validate` | Add validation that checks for non-empty payload before treating a 200 as success |
| F3 (Prompt verbosity) | `prompt.edit` | Revert the over-confirmation prompt change back to the previous version |

Each prescription includes:

```json
{
  "id": "p1",
  "diagnosis_id": "d1",
  "change_type": "kb.add",
  "target": "premium_card_info",
  "description": "Index the premium card product pack: fees, eligibility, benefits. 11 documents.",
  "predicted_delta": {
    "metric": "resolution_rate",
    "from": 0.31,
    "to": 0.84
  },
  "decision": {
    "asking_approval_for": "Index the premium card product pack into the live knowledge base.",
    "risk_if_diagnosis_wrong": "Low. Adding correct content cannot degrade existing behavior.",
    "would_not_ship_if": "The replay shows any regression on the golden set."
  }
}
```

The `decision` block is what the human operator sees in the UI — it frames the approval request honestly, including what could go wrong.

---

## 10. Step 8 — Verify Fixes Using the Replay Service

The kit provides a **replay service** (`replay/serve.py`) that simulates what would happen if we applied a fix. We have **40 attempts total** — so we can't just brute-force it.

### How replay works:

1. Start the server: `python replay/serve.py --kit ../../kit --port 8719`
2. Send a POST request:

```python
import urllib.request, json

payload = {
    "team": "our-team",
    "tenant": "acme-bank",
    "change": {
        "type": "kb.add",
        "target": "premium_card_info",
        "description": "Add missing product documentation"
    },
    "cohort": {
        "intent": "premium_card_info",
        "from_day": 34,
        "to_day": 45
    }
}

req = urllib.request.Request(
    "http://localhost:8719/replay",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"}
)
response = json.loads(urllib.request.urlopen(req).read())
```

3. Get back the result:

```json
{
  "run_id": "rp_5c9a1d3e",
  "before": 0.3412,
  "after": 0.8391,
  "delta": 0.4979,
  "verdict": "improved",
  "golden_set_pass": true,
  "sessions_replayed": 412
}
```

The `run_id` starting with `rp_` proves we actually used the replay service (worth +3.5 points in loop completeness).

### What the verdicts mean:

- **`improved`**: The fix worked! Resolution went up.
- **`no_effect`**: The fix didn't change anything. Either the fix was wrong, or the cohort had no problem. The server deliberately makes these indistinguishable to prevent probing.
- **`regressed`**: The fix made things worse (golden set regression).

A `no_effect` result is **valid and honest**. We record it as-is. We never fabricate a successful verification.

---

## 11. Step 9 — Handle Unanswerable Questions (Gaps)

The operator asks 11 questions (in `asks.md`). Most can be answered. Some cannot.

### A11: "What is our failover rate?"

**This cannot be answered.** The runtime has no failover mechanism — no step_type, outcome, or error_class in the logs records a failover event. There's literally nothing in 880,000 steps that says "primary failed, switched to alternate."

We MUST refuse to answer and explain why:

```json
{
  "ask_id": "A11",
  "verdict": "NOT_MEASURABLE",
  "why": "No failover mechanism exists in the runtime. No step in the corpus records a primary path failing and an alternate being chosen.",
  "nearest_proxy": "llm_call rows with retry_count > 0",
  "why_the_proxy_misleads": "Those are QUALITY retries — the same target re-issued after a malformed response — not a switch to an alternate target. Reporting them as failover would show a non-zero rate that reads as 'failover is working' when failover does not exist.",
  "required_event": {
    "name": "failover",
    "grain": "step",
    "fields": ["from_target", "to_target", "reason", "recovered"],
    "owner": "conversation-runtime"
  }
}
```

This refusal is **worth points**. The scoring rewards honest refusal with a usable specification of what event would need to be logged. Inventing a fake failover metric would cost points.

### A09: "How many users gave up out of frustration?"

This is a **fidelity trap**. We can count abandonments (`session_end == "abandoned"` — that's measured). But the **reason** for abandonment (frustration vs. "I got what I needed and left") requires a judge — there's no log field for it. We declare the reason as `fidelity: "judged"` or emit a gap with `verdict: "REQUIRES_NEW_JUDGE"`.

### Other answerable asks:

Each answerable ask maps to a metric with an `ask_id` field:
- A01 → `containment_rate` metric
- A02 → `turns_to_resolve` metric
- A04 → `tool_failure_rate` metric
- A06 → `quality_score` metric (judged, with calibration)
- etc.

---

## 12. Step 10 — Mine What "Good" Looks Like (Standard)

We profile the deployment's own best conversations to establish a baseline of excellence:

1. Filter sessions: `session_end == "resolved"` AND `quality_score >= 4.0` (within same `judge_version`)
2. Group by tenant and intent
3. For each group, calculate: top-decile turns, median turns, resolution rate

Output:
```json
{
  "tenant": "acme-bank",
  "cohort": {"intent": "product_info"},
  "metric": "turns_to_resolve",
  "exemplar_n": 412,
  "golden_set_version": "gs_v1",
  "best": 3.0,
  "median": 5.0,
  "deficit": 2.0,
  "derivation": "top decile of resolved sessions by turns, no tool error, no kb miss, quality >= 4"
}
```

This "standard" is then used as the benchmark — deviations from it are candidates for investigation. The standard is part of the **fixed yardstick** — once established, we never change it to make results look better.

---

## 13. Step 11 — Build the Operator UI

We build a Flask web server with a single-page HTML interface. **This is NOT a dashboard — it's a decision-making tool.**

### Backend (`ui/app.py`):

```python
from flask import Flask, jsonify, request
import json

app = Flask(__name__)

# Load the report
with open("my-loop-report.json") as f:
    report = json.load(f)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/report")
def get_report():
    return jsonify(report)

@app.route("/api/decisions/<prescription_id>", methods=["POST"])
def record_decision(prescription_id):
    decision = request.json  # {verdict, decided_by, reason}
    # Find the prescription and write the approval
    for p in report["prescriptions"]:
        if p["id"] == prescription_id:
            p["approval"] = {
                "verdict": decision["verdict"],       # "accepted", "rejected", "deferred"
                "decided_by": decision["decided_by"],
                "reason": decision["reason"],
                "at": datetime.now().isoformat()
            }
    # Write back to the file
    with open("my-loop-report.json", "w") as f:
        json.dump(report, f, indent=2)
    return jsonify({"ok": True})
```

### Frontend (`templates/index.html`):

A sidebar + detail panel layout:

- **Left sidebar**: Lists findings (real problems in red, dismissed lookalikes in green), gaps, and metrics
- **Detail panel**: When you click a finding, shows:
  - Problem description
  - Impact numbers (conversations affected, traffic share, cost)
  - Evidence (baseline vs comparison, specific metrics)
  - Diagnosis (what config change caused this, confidence level)
  - Proposed fix (what to change, who owns it)
  - Verification result (before/after from replay)
  - **APPROVE / REJECT / DEFER buttons** with a reason text field

When the operator clicks a button, JavaScript sends a POST to `/api/decisions/<id>`, which writes the decision back into `loop-report.json`.

---

## 14. Step 12 — Assemble the Report

The final `loop-report.json` has this structure (all field names from the actual schema):

```json
{
  "team": "our-team-name",
  "corpus": "A",
  "generated_at": "2026-09-12T12:00:00Z",
  "system_notes": "What is real, what is stubbed...",

  "metrics": [
    {
      "id": "m_containment",
      "ask_id": "A01",
      "fidelity": "measured",
      "coverage": {"value": 1.0, "basis": "..."},
      "calibration": null,
      "plan": {"source": "...", "filter": "...", "denominator": "..."}
    }
  ],

  "standard": [
    {"tenant": "...", "cohort": {}, "metric": "...", "best": 0.94, "median": 0.79, "deficit": 0.15}
  ],

  "findings": [
    {
      "id": "f1",
      "tenant": "acme-bank",
      "cohort": {"intent": "premium_card_info"},
      "metric": "resolution_rate",
      "window": {"from_day": 35, "to_day": 45},
      "observed": 0.31,
      "expected": 0.86,
      "is_regression": true,
      "impact": {"conversations_affected": 239, "share_of_traffic": 0.042, "derivation": "..."},
      "audience": ["agent_builder"],
      "if_nothing_changes": "..."
    },
    {
      "id": "f_d1",
      "is_regression": false,
      "not_a_regression_because": "traffic composition change, not quality change"
    }
  ],

  "diagnoses": [
    {"id": "d1", "finding_id": "f1", "cause_class": "kb.gap", "confidence": 0.91, "attributed_change": {"kind": "kb", "day": 34}}
  ],

  "prescriptions": [
    {
      "id": "p1",
      "diagnosis_id": "d1",
      "change_type": "kb.add",
      "target": "premium_card_info",
      "predicted_delta": {"metric": "resolution_rate", "from": 0.31, "to": 0.84},
      "decision": {"asking_approval_for": "...", "risk_if_diagnosis_wrong": "...", "would_not_ship_if": "..."},
      "approval": {"verdict": "accepted", "decided_by": "operator", "reason": "...", "at": "..."}
    }
  ],

  "verifications": [
    {"prescription_id": "p1", "replay_run_id": "rp_5c9a1d3e", "before": 0.34, "after": 0.84, "verdict": "improved"}
  ],

  "gaps": [
    {"ask_id": "A11", "verdict": "NOT_MEASURABLE", "why": "...", "required_event": {"name": "failover", "fields": ["from_target", "to_target"]}}
  ],

  "self_assessment": {
    "cycles": 1,
    "prescription_accuracy": {"kb.add": {"n": 1, "hit_rate": 1.0}},
    "downweighted": [],
    "notes": "..."
  }
}
```

We validate this against `schema/loop-report.schema.json` using `jsonschema.validate()` before writing. An invalid report scores **0**.

---

## 15. Step 13 — Self-Assessment

The system evaluates its own performance:

```json
{
  "cycles": 1,
  "prescription_accuracy": {
    "kb.add": {
      "n": 1,
      "hit_rate": 1.0,
      "mean_prediction_error": -0.0079
    },
    "tool.validate": {
      "n": 1,
      "hit_rate": 1.0,
      "mean_prediction_error": 0.0026
    }
  },
  "downweighted": [],
  "notes": "One cycle only. Hit rates from n=1 carry no statistical weight."
}
```

- **`cycles`**: How many detect→fix→verify iterations we ran
- **`prescription_accuracy`**: For each fix type, how often did it actually improve things?
- **`prediction_error`**: How close was our predicted improvement to the actual?
- **`downweighted`**: Fix types we learned to trust less (none yet with 1 cycle)

Honesty matters: "n=1 is not evidence" is a better note than claiming high confidence from a single observation.

---

## 16. Step 14 — Score and Iterate

After generating the report, we score it:

```bash
python tools/nexus-loop-kit/score.py my-loop-report.json
```

This prints a breakdown of the 55 machine-scored points. We then iterate:

```
Fix gaps in the report
     ↓
Regenerate
     ↓
Re-score
     ↓
Repeat until score plateaus
```

Development flow:
1. **Always develop on `corpus_sample` first** (5% = ~4,000 sessions, runs in seconds)
2. **Confirm on full `corpus`** before final submission (80,000 sessions)

---

## 17. How the Scoring Works

**55 machine-scored points:**

| Category | Points | What It Rewards |
|----------|--------|-----------------|
| **Diagnostic Accuracy** | 20 | Finding real problems, correct tenant/intent slice, correct cause, early detection |
| **Specificity** | 15 | NOT reporting false alarms. Explicit dismissals with correct cause_class earn +3 each |
| **Honesty** | 12 | Fidelity/coverage on metrics, coverage trap handled, A11 refused correctly, calibration not 1.0 |
| **Loop Completeness** | 8 | Full chain: metrics → finding → diagnosis → prescription → replay verification |

**45 human-scored points:**

| Category | Points | What It Rewards |
|----------|--------|-----------------|
| **Decision Quality** | 18 | Can an operator act on each finding? Is impact traceable? Is baseline named? |
| **Screen** | 15 | Can a stranger understand the finding? Can they see evidence? Can they approve/reject? |
| **Framing** | 12 | Demo opens with problem, shows lookalike dismissal, shows refusal, admits what's stubbed |

---

## 18. The Sealed Run — Why Nothing Can Be Hardcoded

On Day 6, we receive a **new dataset** with:
- Same kinds of problems (KB gap, silent API failure, prompt regression)
- Same kinds of lookalikes (traffic mix, load spike, rubric change)
- But **different tenants, different intents, different days, different slices**

Our system must discover the problems from scratch with no manual intervention.

This means:
- ❌ No `if tenant == "acme-bank"` 
- ❌ No `if intent == "premium_card_info"`
- ❌ No `if day >= 34`
- ✅ All tenants discovered from `set(s["tenant"] for s in sessions)`
- ✅ All intents discovered from the data
- ✅ All changepoints found by comparing time-series, not by knowing which day to look at
- ✅ All causes inferred from patterns (KB miss → kb.gap, empty payload → tool.contract_break, turns up → prompt.regression)

---

## 19. End-to-End Flow in One Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                    RAW LOG FILES                                    │
│  sessions.jsonl.gz  │  agent_steps.jsonl.gz  │  config_timeline.csv │
└──────────┬──────────┴──────────┬──────────────┴──────────┬──────────┘
           │                     │                         │
           ▼                     ▼                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STEP 1: INGEST — Stream, parse, index by tenant/intent/week       │
│  Separate v3_agent from v2_flow for coverage accuracy              │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STEP 2: METRICS — Compute containment, tool_failure, kb_miss,     │
│  silent_empty_rate, turns, quality, CSAT for every segment          │
│  Every metric has: fidelity + coverage + calibration (if judged)   │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STEP 3: DETECT — Probe for resolution drops, tool payload changes,│
│  turn inflation, KB misses, volume spikes, quality drops            │
│  Output: candidate findings                                         │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                    ┌──────────┴──────────┐
                    ▼                     ▼
┌───────────────────────┐  ┌──────────────────────────────────────────┐
│  STEP 4a: CONFIRM     │  │  STEP 4b: DISMISS                       │
│  F1: KB gap           │  │  D1: traffic_mix — per-intent rates flat │
│  F2: Silent API fail  │  │  D2: load — resolution unaffected        │
│  F3: Prompt verbose   │  │  D3: judge_change — raw outcomes stable  │
└──────────┬────────────┘  └──────────┬───────────────────────────────┘
           │                          │
           ▼                          ▼
┌───────────────────────┐  ┌──────────────────────────────────────────┐
│  STEP 5: DIAGNOSE     │  │  findings[is_regression:false]           │
│  Link to config change│  │  + diagnoses with cause_class            │
│  cause_class + conf.  │  │  = specificity credit                    │
└──────────┬────────────┘  └──────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STEP 6: IMPACT — Count affected conversations, traffic share,      │
│  unplanned handoffs, cost, days running. Full derivation string.    │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STEP 7: PRESCRIBE — Map cause to fix type (kb.add, tool.validate, │
│  prompt.edit). Include predicted_delta, risk, who should approve.   │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STEP 8: VERIFY — POST /replay to the replay service.              │
│  Record run_id (rp_...), before, after, verdict.                   │
│  40-run budget. Failed fix = honest result.                        │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STEP 9-10: GAPS + STANDARD                                        │
│  A11 → NOT_MEASURABLE (failover doesn't exist)                     │
│  Standard: mine "good" from deployment's own best conversations    │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STEP 12: ASSEMBLE loop-report.json                                 │
│  Validate against schema. Write file.                               │
│                                                                     │
│  STEP 13: Self-assessment — cycles, prediction accuracy, notes      │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STEP 14: SCORE — python score.py my-loop-report.json               │
│  Iterate until score plateaus.                                      │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STEP 11: OPERATOR UI (Flask)                                       │
│  Human reads finding → sees evidence → sees verification →          │
│  clicks APPROVE / REJECT / DEFER → decision written to report      │
└─────────────────────────────────────────────────────────────────────┘
```

