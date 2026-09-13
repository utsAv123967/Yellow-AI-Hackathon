#!/usr/bin/env python3
"""Replay requests for the report's prescriptions — printed by default, sent only on request.

    # dry run: print the exact request body for every prescription (nothing is sent)
    python -m nexus_loop.replay_client --report my-loop-report.json --team <team>

    # send ONE prescription's request, record the result, keep a ledger
    python -m nexus_loop.replay_client --report my-loop-report.json --team <team> \
           --prescription p1 --send --url http://127.0.0.1:8719

This module is the only place in the package that can open a network connection, and it does so
only with --send. The pipeline never imports it.

Guards, because the team has 40 replay runs and every call is logged:
  * one prescription per --send; there is no "send all";
  * the service answers deterministically, so an identical request is refused locally (it would
    only spend a run for the same answer) unless --allow-repeat is given;
  * the local ledger counts runs and refuses once the 40-run budget is reached;
  * the response is saved raw in the ledger, and a verification entry is written to
    verifications.json in exactly the shape `pipeline --verifications` accepts.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

BUDGET = 40


def request_body(prescription: dict, team: str) -> dict:
    rr = prescription["replay_request"]
    return {"team": team, "tenant": rr["tenant"], "change": rr["change"], "cohort": rr["cohort"],
            "golden_set": rr.get("golden_set") or []}


def body_hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


def _load(path: str, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def _save(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _number(value, metric: str):
    """before/after may come back as a number or as {metric: number}."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, dict):
        if isinstance(value.get(metric), (int, float)):
            return value[metric]
        nums = [v for v in value.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if len(nums) == 1:
            return nums[0]
    return None


def _golden_pass(g):
    if isinstance(g, bool):
        return g
    if isinstance(g, dict):
        for k in ("pass", "passed", "ok", "clean"):
            if isinstance(g.get(k), bool):
                return g[k]
        for k in ("regressed", "broken", "failures"):
            if isinstance(g.get(k), (int, float)) and not isinstance(g.get(k), bool):
                return g[k] == 0
            if isinstance(g.get(k), list):
                return len(g[k]) == 0
    return None


def verification_from_response(prescription: dict, resp: dict) -> dict:
    metric = resp.get("metric") or prescription["predicted_delta"]["metric"]
    v = {"prescription_id": prescription["id"], "replay_run_id": str(resp.get("run_id", "")),
         "verdict": resp.get("verdict"), "metric": metric, "signature": prescription.get("signature")}
    before, after = _number(resp.get("before"), metric), _number(resp.get("after"), metric)
    if before is not None:
        v["before"] = before
    if after is not None:
        v["after"] = after
    gp = _golden_pass(resp.get("golden_set"))
    if gp is not None:
        v["golden_set_pass"] = gp
    return v


def main() -> int:
    ap = argparse.ArgumentParser(description="Print (default) or send one replay request per prescription.")
    ap.add_argument("--report", required=True)
    ap.add_argument("--team", default="<team>")
    ap.add_argument("--prescription", help="prescription id (required with --send)")
    ap.add_argument("--send", action="store_true", help="actually POST the request (spends one of the 40 runs)")
    ap.add_argument("--url", default="http://127.0.0.1:8719")
    ap.add_argument("--verifications", default=None, help="default: verifications.json beside the report")
    ap.add_argument("--ledger", default=None, help="default: replay_ledger.json beside the report")
    ap.add_argument("--allow-repeat", action="store_true", help="send even if this exact request was sent before")
    a = ap.parse_args()

    with open(a.report, encoding="utf-8") as f:
        report = json.load(f)
    base = os.path.dirname(os.path.abspath(a.report))
    ledger_path = a.ledger or os.path.join(base, "replay_ledger.json")
    ver_path = a.verifications or os.path.join(base, "verifications.json")
    ledger = _load(ledger_path, {"runs": []})
    prescriptions = [p for p in report.get("prescriptions", []) if p.get("replay_request")]
    if a.prescription:
        prescriptions = [p for p in prescriptions if p["id"] == a.prescription]
        if not prescriptions:
            print("ERROR: no prescription %s with a replay_request" % a.prescription, file=sys.stderr)
            return 1
    sent_hashes = {r["hash"]: r for r in ledger["runs"]}

    if not a.send:
        print("DRY RUN — nothing is sent. %d of %d replay run(s) recorded in %s.\n" % (len(ledger["runs"]), BUDGET, ledger_path))
        for p in prescriptions:
            body = request_body(p, a.team)
            prior = sent_hashes.get(body_hash(body))
            print("== %s  %s -> %s  (%s)" % (p["id"], p["change_type"], p["target"],
                                           "ALREADY SENT as %s" % prior.get("run_id") if prior else "not sent yet"))
            print("POST %s/replay" % a.url.rstrip("/"))
            print(json.dumps(body, indent=2))
            print()
        return 0

    if not a.prescription:
        print("ERROR: --send needs --prescription (one run at a time)", file=sys.stderr)
        return 1
    if a.team == "<team>":
        print("ERROR: --send needs --team", file=sys.stderr)
        return 1
    if len(ledger["runs"]) >= BUDGET:
        print("ERROR: ledger shows %d runs — the %d-run budget is spent" % (len(ledger["runs"]), BUDGET), file=sys.stderr)
        return 1
    p = prescriptions[0]
    body = request_body(p, a.team)
    h = body_hash(body)
    if h in sent_hashes and not a.allow_repeat:
        print("REFUSED: this exact request was already sent as %s; the service is deterministic, so a repeat returns "
              "the same answer and spends a run. Use --allow-repeat to override." % sent_hashes[h].get("run_id"), file=sys.stderr)
        return 1

    req = urllib.request.Request(a.url.rstrip("/") + "/replay", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print("ERROR: replay service returned HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:500]), file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print("ERROR: could not reach %s: %s" % (a.url, e), file=sys.stderr)
        return 1

    ledger["runs"].append({"hash": h, "prescription_id": p["id"], "signature": p.get("signature"),
                           "run_id": resp.get("run_id"), "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "request": body, "response": resp})
    _save(ledger_path, ledger)

    v = verification_from_response(p, resp)
    verifications = _load(ver_path, [])
    verifications = [x for x in verifications if not (x.get("prescription_id") == v["prescription_id"] and x.get("replay_run_id") == v["replay_run_id"])]
    verifications.append(v)
    _save(ver_path, verifications)

    print("run %s: verdict=%s before=%s after=%s golden_set_pass=%s" % (v["replay_run_id"], v.get("verdict"), v.get("before"), v.get("after"), v.get("golden_set_pass")))
    print("recorded in %s (ledger: %d/%d runs). Re-run the pipeline with --verifications %s" % (ver_path, len(ledger["runs"]), BUDGET, ver_path))
    missing = [k for k in ("before", "after", "golden_set_pass") if k not in v]
    if missing or not str(v["replay_run_id"]).startswith("rp_") or v.get("verdict") not in ("improved", "no_effect", "regressed"):
        print("NOTE: could not read %s from the response automatically — check the raw response in %s and fill "
              "verifications.json by hand." % (", ".join(missing + ([] if str(v["replay_run_id"]).startswith("rp_") else ["run_id"])), ledger_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
