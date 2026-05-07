#!/usr/bin/env python3
"""Determinism contract test for bpmigrate's pure-Python commands.

Each command in `CASES` is run twice with identical inputs. The two
outputs MUST be byte-identical -- if they differ, the toolchain has
introduced a nondeterministic source (Python set iteration, random,
hash seed dependence, dict ordering on <3.7, etc.) and the deterministic
guarantee is broken.

UE-side commandlets (`dump-graph`, `dump-class-reflection`, `snapshot`,
`verify`) are not tested here -- they require a project and the editor.
Their determinism is checked indirectly: their JSON output is consumed
by Python commands, and any nondeterminism would propagate.

Usage:
    # Run against a real BP from your project. Set whichever subset of
    # env vars you can produce — each enables a different command:
    #   TEST_BP_JSON      UAssetGUI tojson output (-> summarize / emit-dispatcher / emit-variable / scenario / detect-gaps)
    #   TEST_BP_GRAPH     DumpBPGraph output      (-> emit-class-flags / emit-component-overrides / detect-gaps / scenario)
    #   TEST_NEW_PARENT   DumpClassReflection output (-> map-broken-refs)
    #   TEST_GAPS         detect-gaps output         (-> map-broken-refs)
    # With no env vars, the script exits 0 with WARN and zero cases.
    python tests/test_determinism.py

Exit code 0 on PASS (or no cases configured), 1 on FAIL.
"""

from __future__ import annotations

import filecmp
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLI = [sys.executable, str(REPO / "python" / "bpmigrate.py")]


def run_twice(name: str, args_with_output_placeholder: list, output_flag: str = "-o") -> tuple[bool, str]:
    """Run `bpmigrate <args> -o <tmp>` twice; return (passed, msg).

    `args_with_output_placeholder` must NOT include `-o`. The runner
    appends `-o <unique tmp path>` for each run so the two outputs land
    in different files for filecmp.
    """
    with tempfile.TemporaryDirectory() as td:
        out_a = Path(td) / "a.out"
        out_b = Path(td) / "b.out"
        cmd_a = CLI + args_with_output_placeholder + [output_flag, str(out_a)]
        cmd_b = CLI + args_with_output_placeholder + [output_flag, str(out_b)]
        ra = subprocess.run(cmd_a, capture_output=True, text=True)
        rb = subprocess.run(cmd_b, capture_output=True, text=True)
        if ra.returncode != 0:
            return (False, f"first run failed (exit {ra.returncode}): {ra.stderr.strip()[:200]}")
        if rb.returncode != 0:
            return (False, f"second run failed (exit {rb.returncode}): {rb.stderr.strip()[:200]}")
        if not (out_a.exists() and out_b.exists()):
            return (False, "expected output files not produced")
        if filecmp.cmp(out_a, out_b, shallow=False):
            sz = out_a.stat().st_size
            return (True, f"identical ({sz} bytes)")
        # Show first differing chunk for debugging.
        a_bytes = out_a.read_bytes()
        b_bytes = out_b.read_bytes()
        for i, (ca, cb) in enumerate(zip(a_bytes, b_bytes)):
            if ca != cb:
                ctx_lo = max(0, i - 30)
                ctx_hi = min(len(a_bytes), i + 30)
                return (False, f"diverged at byte {i}; A={a_bytes[ctx_lo:ctx_hi]!r} B={b_bytes[ctx_lo:ctx_hi]!r}")
        return (False, f"length mismatch: a={len(a_bytes)} b={len(b_bytes)}")


def env(name: str) -> str | None:
    v = os.environ.get(name)
    return v if v and Path(v).exists() else None


def main() -> int:
    bp_json = env("TEST_BP_JSON")
    bp_graph = env("TEST_BP_GRAPH")
    new_parent = env("TEST_NEW_PARENT")
    gaps = env("TEST_GAPS")

    cases: list[tuple[str, list, bool]] = []

    # Each tuple: (label, [bpmigrate args], requires_input_present)
    if bp_json:
        cases.append(("summarize", ["summarize", bp_json], True))
        cases.append(("emit-dispatcher-delegates", ["emit-dispatcher-delegates", bp_json], True))
        cases.append(("emit-variable-defaults", ["emit-variable-defaults", bp_json], True))
    if bp_json and bp_graph:
        cases.append(("detect-gaps (with --graph)",
                      ["detect-gaps", bp_json, "--graph", bp_graph], True))
        cases.append(("scenario (with --graph)",
                      ["scenario", bp_json, "--graph", bp_graph], True))
    if bp_graph:
        cases.append(("emit-class-flags", ["emit-class-flags", bp_graph], True))
        cases.append(("emit-component-overrides",
                      ["emit-component-overrides", bp_graph], True))
    if gaps and new_parent:
        cases.append(("map-broken-refs",
                      ["map-broken-refs", "--gaps", gaps,
                       "--new-parent", new_parent], True))

    if not cases:
        print("WARN: no fixture env vars set; nothing to test.")
        print("  set TEST_BP_JSON / TEST_BP_GRAPH / TEST_NEW_PARENT / TEST_GAPS to enable.")
        return 0

    print(f"Running {len(cases)} determinism contract test(s)...\n")
    failed = 0
    for label, args, _ in cases:
        passed, msg = run_twice(label, args)
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {label}: {msg}")
        if not passed:
            failed += 1

    print()
    if failed:
        print(f"FAIL: {failed}/{len(cases)} commands are nondeterministic")
        return 1
    print(f"PASS: all {len(cases)} commands are deterministic")
    return 0


if __name__ == "__main__":
    sys.exit(main())
