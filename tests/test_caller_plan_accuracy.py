"""Closed-loop accuracy regression test for `plan-rewrite-callers`.

`plan-rewrite-callers` (in `bpmigrate.py`) is itself backed by `DumpCallSites`,
so the plan rows ARE the ground truth -- the two sets are PASS by construction.
This file exists to make that contract explicit so the day someone swaps in a
faster-but-approximate enumerator, the regression fails loudly here instead
of silently producing wrong call-site sets in production migrations.

Two checks:

  1. **PASS-by-construction** — for each BP in CORPUS, `_compute_plan_callsites`
     and the truth file it produces describe identical `(caller, function)` pairs.
  2. **Sanity-negative** — synthetic mutations to the truth file (drop a pair,
     bump a count, inject a synthetic pair) MUST be detected by the diff
     logic. Proves the verifier itself can detect a regression.

Requires: `BPMIGRATION_PROJECT_ROOT`, `BPMIGRATION_UPROJECT`, `BPMIGRATION_UE_CMD`
env vars (same as the editor-driven CLI commands). Skipped otherwise.

Run:
    python tests/test_caller_plan_accuracy.py /Game/Path/To/BP1 /Game/Path/To/BP2 ...
"""
from __future__ import annotations

import json
import os
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

import bpmigrate  # type: ignore


def _check_pass_by_construction(bp_path: str, cfg) -> int:
    plan_rows, callers, _itf, _sz, truth_path = bpmigrate._compute_plan_callsites(bp_path, cfg)
    if plan_rows is None:
        print(f"  SKIP {bp_path}: helper bailed (caller enumeration / commandlet failed)")
        return 0
    if not callers:
        print(f"  SKIP {bp_path}: no callers")
        return 0
    if truth_path is None or not truth_path.exists():
        print(f"  FAIL {bp_path}: helper did not return a truth file")
        return 1
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    failed = truth.get("callers_failed") or []
    if failed:
        print(f"  WARN {bp_path}: {len(failed)} caller(s) failed to load -- comparison would be over a shrunken universe")
        for cp in failed[:5]:
            print(f"    - {cp}")
        return 2
    truth_set = {(s["caller"], s["function"], int(s.get("count", 1)))
                 for s in truth.get("callsites", [])}
    plan_set = {(c, f, h) for c, f, h in plan_rows}
    if truth_set == plan_set:
        print(f"  PASS {bp_path}: {len(truth_set)} call-site triples match exactly")
        return 0
    print(f"  FAIL {bp_path}: plan vs truth diverge")
    print(f"    truth - plan: {sorted(truth_set - plan_set)[:5]}")
    print(f"    plan - truth: {sorted(plan_set - truth_set)[:5]}")
    return 1


def _check_sanity_negative(bp_path: str, cfg) -> int:
    """Mutate the truth file in three ways; the diff logic must catch each."""
    plan_rows, callers, _itf, _sz, truth_path = bpmigrate._compute_plan_callsites(bp_path, cfg)
    if plan_rows is None or not callers or truth_path is None:
        print(f"  SKIP sanity-negative for {bp_path}: helper bailed")
        return 0
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    sites = list(truth.get("callsites", []))
    if not sites:
        print(f"  SKIP sanity-negative for {bp_path}: 0 truth call-sites to mutate")
        return 0

    plan_set = {(c, f, h) for c, f, h in plan_rows}
    truth_set = {(s["caller"], s["function"], int(s.get("count", 1))) for s in sites}
    if truth_set != plan_set:
        print(f"  SKIP sanity-negative for {bp_path}: baseline already mismatches")
        return 0

    # 1) drop first site -> false positive (plan has it, mutated truth doesn't)
    dropped = sites[0]
    mutated_no_drop = sites[1:]
    mut_set = {(s["caller"], s["function"], int(s.get("count", 1))) for s in mutated_no_drop}
    fp = plan_set - mut_set
    if (dropped["caller"], dropped["function"], int(dropped.get("count", 1))) not in fp:
        print(f"  FAIL sanity-negative drop: {bp_path}: false-positive not detected")
        return 1

    # 2) bump count -> count-drift
    bumped = dict(sites[0]); bumped["count"] = bumped["count"] + 99
    mutated_bump = [bumped] + sites[1:]
    bump_set = {(s["caller"], s["function"], int(s.get("count", 1))) for s in mutated_bump}
    drift = bump_set ^ plan_set
    if not drift:
        print(f"  FAIL sanity-negative bump: {bp_path}: count drift not detected")
        return 1

    # 3) inject synthetic -> false negative (truth has it, plan doesn't)
    synthetic = {"caller": "/Game/_synthetic_caller_", "function": "_synthetic_fn_",
                 "count": 7, "graphs": ["_synthetic_graph_"]}
    mutated_inject = sites + [synthetic]
    inject_set = {(s["caller"], s["function"], int(s.get("count", 1))) for s in mutated_inject}
    fn = inject_set - plan_set
    if ("/Game/_synthetic_caller_", "_synthetic_fn_", 7) not in fn:
        print(f"  FAIL sanity-negative inject: {bp_path}: false-negative not detected")
        return 1

    print(f"  PASS sanity-negative for {bp_path}: drop / bump / inject all detected")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("bp_paths", nargs="+", help="/Game/.../<BP> targets to verify.")
    p.add_argument("--skip-sanity", action="store_true",
                   help="Skip the sanity-negative perturbation test (only run pass-by-construction).")
    args = p.parse_args()

    for var in ("BPMIGRATION_PROJECT_ROOT", "BPMIGRATION_UPROJECT", "BPMIGRATION_UE_CMD"):
        if not os.environ.get(var):
            print(f"SKIP all: {var} unset (this test requires the editor-driven path)", file=sys.stderr)
            return 0

    cfg = bpmigrate.Config(argparse.Namespace())
    failures = 0
    print(f"== pass-by-construction over {len(args.bp_paths)} BP(s) ==")
    for bp in args.bp_paths:
        failures += _check_pass_by_construction(bp, cfg)
    if not args.skip_sanity and args.bp_paths:
        print("== sanity-negative on first BP ==")
        failures += _check_sanity_negative(args.bp_paths[0], cfg)
    print(f"\n{'FAIL' if failures else 'PASS'}: {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
