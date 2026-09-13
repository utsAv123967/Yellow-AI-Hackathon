#!/usr/bin/env python3
"""Pre-freeze check: does the pipeline still work on layouts it has never seen?

    python scripts/sealed_suite.py                       # 3 fresh sealed-style corpora + a renamed-tenant copy
    python scripts/sealed_suite.py --count 5 --no-rename
    python scripts/sealed_suite.py --kits kit path/to/other-kit --count 0

For each corpus it runs exactly the day-6 command (python -m nexus_loop.pipeline --kit <dir>), then
scores the output with the kit's score.py. The answer key is read by score.py only — never by the
pipeline. Corpora are built with the organisers' own generator (generate.py --sealed <phrase>), which
moves every fault and decoy to new days and lengths. The renamed copy replaces tenant names, which the
public generator never changes but the PS says day 6 does.

Exit code 1 if any corpus scores below --min-score or the pipeline exits non-zero.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIT_TOOLS = os.path.join(ROOT, "tools", "nexus-loop-kit")


def generate(phrase: str, out: str) -> None:
    if os.path.exists(os.path.join(out, "manifest.json")):
        return
    subprocess.run([sys.executable, os.path.join(KIT_TOOLS, "generate.py"), "--sealed", phrase, "--out", out,
                    "--dev-sample", "0"], check=True, stdout=subprocess.DEVNULL)


def rename_tenants(src: str, dst: str) -> None:
    if os.path.exists(os.path.join(dst, "manifest.json")):
        return
    with open(os.path.join(src, "manifest.json"), encoding="utf-8") as f:
        tenants = json.load(f)["tenants"]
    mapping = [(t, "renamed-tenant-%d" % i) for i, t in enumerate(tenants, 1)]

    def sub(text: str) -> str:
        for a, b in mapping:
            text = text.replace(a, b)
        return text

    for root, _, files in os.walk(src):
        rel = os.path.relpath(root, src)
        os.makedirs(os.path.join(dst, rel), exist_ok=True)
        for fn in files:
            s, d = os.path.join(root, fn), os.path.join(dst, rel, fn)
            if fn.endswith(".gz"):
                with gzip.open(s, "rt", encoding="utf-8") as fi, gzip.open(d, "wt", encoding="utf-8", compresslevel=1) as fo:
                    for line in fi:
                        fo.write(sub(line))
            elif fn.endswith((".json", ".jsonl", ".csv", ".md")):
                with open(s, encoding="latin-1") as fi, open(d, "w", encoding="latin-1", newline="") as fo:
                    fo.write(sub(fi.read()))
            else:
                shutil.copy(s, d)


def ground_truth(kit: str) -> str:
    for sub in ("ground_truth_SEALED", "ground_truth"):
        p = os.path.join(kit, sub, "ground_truth.json")
        if os.path.exists(p):
            return p
    return ""


def run_one(kit: str, label: str) -> dict:
    out = os.path.join(kit, "suite-report.json")
    p = subprocess.run([sys.executable, "-m", "nexus_loop.pipeline", "--kit", kit, "--out", out],
                       cwd=ROOT, capture_output=True, text=True)
    row = {"corpus": label, "exit": p.returncode}
    gt = ground_truth(kit)
    if p.returncode not in (0, 2) or not os.path.exists(out) or not gt:
        row["error"] = (p.stderr or "no ground truth to score against")[-400:]
        return row
    sc = subprocess.run([sys.executable, os.path.join(KIT_TOOLS, "score.py"), "--report", out, "--ground-truth", gt, "--json"],
                        capture_output=True, text=True)
    d = json.loads(sc.stdout)
    s = d["sections"]
    row.update(machine=d["machine_score"], accuracy=s["Diagnostic accuracy"]["score"], specificity=s["Specificity"]["score"],
               loop=s["Loop completeness"]["score"], honesty=s["Honesty"]["score"],
               notes=[n for k in ("Diagnostic accuracy", "Specificity") for n in s[k]["notes"]
                      if not n.startswith(("HIT", "CLEAN"))])
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-dir", default=os.path.join(tempfile.gettempdir(), "nexus_sealed_suite"))
    ap.add_argument("--count", type=int, default=3)
    ap.add_argument("--prefix", default="nexus-suite-layout")
    ap.add_argument("--no-rename", action="store_true")
    ap.add_argument("--kits", nargs="*", default=[], help="existing kit directories to include")
    ap.add_argument("--min-score", type=float, default=54.0)
    a = ap.parse_args()

    os.makedirs(a.work_dir, exist_ok=True)
    corpora = [(os.path.abspath(k), os.path.basename(os.path.abspath(k))) for k in a.kits]
    for i in range(1, a.count + 1):
        phrase = "%s-%d" % (a.prefix, i)
        out = os.path.join(a.work_dir, phrase)
        print("generating %s ..." % phrase, flush=True)
        generate(phrase, out)
        corpora.append((out, phrase))
    if not a.no_rename and a.count:
        src = corpora[len(a.kits)][0]
        dst = src + "-renamed"
        print("renaming tenants -> %s ..." % os.path.basename(dst), flush=True)
        rename_tenants(src, dst)
        corpora.append((dst, os.path.basename(dst)))

    rows = []
    for kit, label in corpora:
        print("running %s ..." % label, flush=True)
        rows.append(run_one(kit, label))

    print("\n%-36s %4s %8s %6s %6s %6s %6s" % ("corpus", "exit", "machine", "acc", "spec", "loop", "hon"))
    failed = False
    for r in rows:
        if "error" in r:
            failed = True
            print("%-36s %4s  ERROR %s" % (r["corpus"], r["exit"], r["error"]))
            continue
        bad = r["machine"] < a.min_score or r["exit"] != 0
        failed |= bad
        print("%-36s %4s %8.2f %6.2f %6.2f %6.2f %6.2f %s" % (r["corpus"], r["exit"], r["machine"], r["accuracy"],
                                                            r["specificity"], r["loop"], r["honesty"], "  <-- CHECK" if bad else ""))
        for n in r["notes"]:
            print("      " + n)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
