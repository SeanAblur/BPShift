# Limitations & Status

`BPShift` is `v0.1.0` beta. This document is the honest accounting
of what works, what is unverified, what you must supply yourself, and
how far you can take this without an LLM.

---

## Required external dependencies (NOT bundled)

This repository does not contain everything needed to run end-to-end.
You must supply:

| Component | Why | Source |
|---|---|---|
| **Python 3.10+** | The CLI uses PEP 585 typing syntax (`dict[str, list]`). | python.org |
| **Unreal Engine 5.2** | The editor commandlets compile against UE 5.2 APIs. Other engine versions are not validated. | Epic Games Launcher |
| **A target UE project** | The plugin installs into your project; the recipe migrates Blueprints from your Content/. | Your own. |
| **A C++ compiler for UE** | To build the `BPMigration` plugin (MSVC on Windows, Xcode on macOS, clang on Linux per the standard UE toolchain). | Per UE setup docs. |

What IS bundled:

- `tools/UAssetGUI/UAssetGUI.exe` (atenfyr's MIT binary) — no separate
  download needed.
- All Python source.
- All UE plugin C++ source.
- Both skill markdown files.

---

## Can I use this without Claude?

**Read-only inspection: yes**, no LLM required. Run any CLI subcommand
directly:

```
bpmigrate inspect MyBlueprint
bpmigrate summarize <out>/MyBP.json --bytecode
bpmigrate detect-gaps <out>/MyBP.json --summary-text <out>/MyBP.txt
bpmigrate scenario <out>/MyBP.json --graph <out>/MyBP_graph.json

# Deterministic codegen primitives (no LLM):
bpmigrate emit-class-flags         <out>/MyBP_graph.json    # UCLASS(...)
bpmigrate emit-dispatcher-delegates <out>/MyBP.json         # delegate macros
bpmigrate emit-variable-defaults   <out>/MyBP.json          # UPROPERTY initializers
bpmigrate emit-component-overrides <out>/MyBP_graph.json    # constructor body
```

(`<out>` is `BPMIGRATION_TMPDIR` or whatever directory you pass to `-o`; the
CLI defaults to `<system temp>/BPShift` which is `%TEMP%` on Windows
and `/tmp` on macOS / Linux.)

That covers BP analysis, gap detection, and scenario generation with no
LLM in the loop.

**Full migration recipe: needs an agent OR disciplined manual
execution.** `skill/migrate-bp.md` is a multi-step recipe with decision
gates, deterministic rules, and conditional branches. It was authored
for Claude Code, but the markdown body is plain English instructions —
you can:

- Feed it to a different LLM (GPT, Gemini, Llama, etc.). The
  `description` / `argument-hint` / `allowed-tools` frontmatter is
  Claude-specific metadata; the agent can ignore it.
- Read it as a human and execute the steps yourself. Time-consuming but
  fully possible — every gate and rule is spelled out deterministically.

What the LLM (or human) does that the CLI doesn't:

- Parse the BP analysis and write the C++ header / cpp.
- Evaluate the sanity-check gates (R1-R6 dead-mapping rules) and prompt
  for resolution.
- Walk the user through the manual editor steps (delete `CustomEvent`
  nodes, reparent BPs, etc.).
- Iterate the regression test (`bpmigrate verify`) until it passes.

---

## Verified scope

`v0.1.0` is validated end-to-end on a real UE 5.2 project. The capability
matrix lists what is covered.

| Capability | Status | What's covered |
|---|---|---|
| **Read-only inspection** (`inspect` / `dump-graph` / `dump-class-reflection` / `summarize` / `find-bp` / `uasset-tojson` / `scenario`) | ✅ | Every read-only path runs cleanly on real Actor / Pawn / Widget / AnimBP assets. Bundled UAssetGUI handles serialization on Windows. Asset-path parser handles short / long / inner-quoted forms. `BodyInstance` deep sub-field walk (22-field real override). `MaxLen=8192` truncation guard with explicit warning. |
| **Detect / map / instructions** (`detect-gaps` / `map-broken-refs` / `apply-fix-mapping`) | ✅ | All 8 gap categories surfaced: orphan pins, dead inputs, refs/body mismatch, FunctionExports, components, parent CDO overrides, class flags, broken references. Broken-ref coverage: `function` / `variable` / `castTarget` / `eventOverride` / `macro` / `delegate` / `createDelegate` / `asyncTask`. `unauditedK2Nodes` self-check catches K2Node coverage gaps. Layer 2 signature-compare (`--old-parent`) rejects name-matched candidates whose signatures differ. End-to-end Layer 1+2 verified on reparent (downcast / sibling Pawn→AInfo / upcast→Actor). |
| **Deterministic codegen** (4 `emit-*` primitives) | ✅ | `emit-class-flags` (Rule 14, 6/6 synthetic + real Actor BP), `emit-dispatcher-delegates` (Rule 3, real one-param dispatcher + 6/6 edge cases incl. 10-param overflow with explicit TODO), `emit-variable-defaults` (Rule 10/11 — bool/int32/double/FString/FName + name sanitization + Object/Struct/Enum TODO), `emit-component-overrides` (Rule 12, real BP_DummyAxes 4 components / 24 SCS overrides / 22-field BodyInstance / 0 silent drops + AudioComponent / WidgetComponent / NiagaraComponent / ParticleSystemComponent / DecalComponent). Setter names cross-checked against `dump-class-reflection`. Output compiles against UE 5.2. |
| **Snapshot + verify (parity check)** | ✅ | Real BP_DummyAxes → 102-step scenario → snapshot trace → verify against partial `ABPDummyAxesMin` C++. Schema `regression_report_v3` distinguishes structural vs valueMismatch so default-value bugs are not masked by BP-only-field noise. Intentional bug injection (variable mutation, wrong CDO default) → `result: FAIL` with precise diffs; revert → PASS. `BlueprintCreatedComponents` filtered. Static-state scope; complement with manual PIE per Recipe Step 5-C. |
| **Caller-graph surgery cycle** (`analyze-candidate` / `plan-rewrite-callers` / `rewrite-callers` / `verify-callers`) | ✅ | `analyze-candidate` 6-criteria suitability matrix (3 real BPs verified row-for-row incl. SELECTIVE-MIGRATION on BP-defined Interface). `plan-rewrite-callers` is editor-walk authoritative — backed by `DumpCallSites` (UE's own `K2Node_CallFunction` graph walk over Ubergraph + Function + Macro + DelegateSignature + ImplementedInterfaces[].Graphs, with `SubGraphs` recursed). Validated against a 5-BP corpus spanning components / managers / pawns / panels / game-instance: all `(caller, function, count)` triples PASS exact-match. Caller-load failure surfaces as exit 3 (no silent PASS over a shrunken universe). `rewrite-callers` end-to-end on a production BP (18 callers × 38 K2Node sites, 0 `LogBlueprint Error` after force-unload + fresh-load). `verify-callers` reads `FCompilerResultsLog::NumErrors` directly (`compile_blueprint` returns false-positive PASS in some cases — see Known Limitations). Same-name inheritance-only fast path verified (3-caller, 9-site real migration, no surgery). `FBPGraphWalk::ForEachExecGraph` is the single source of truth for the graph walk shared by dumper and rewriter. `tests/test_caller_plan_accuracy.py` exercises drop / count-bump / synthetic-inject perturbations and asserts each is detected. |
| **Plumbing / build / install** | ✅ | `BPMigration` plugin builds in a fresh project (UE 5.2 `TP_Blank`, ~8s clean). Single-source plugin: `ue-plugin/` is canonical, downstream projects drop it into `Plugins/`. Bundled UAssetGUI produces correct UE5_2 output. Read-only paths handle source-control read-only files. Windows shell auto-fix (MSYS argv unmangle + Cygwin path normalize) with conservative match guards (zero false positives across 20-case risk matrix). UTF-8 stdout/stderr reconfigure at import time so non-ASCII help / report output survives Korean cp949 consoles. `_run_commandlet` quote-wraps switch values containing `,` / space / tab so UE `FParse::Value` accepts the full value. TOML config (`.bpmigrate.toml`) auto-discovery + `--config` (CLI flags > env vars > config file). Determinism contract test (`tests/test_determinism.py`) under three `PYTHONHASHSEED` values. |

## Not yet verified

| Item | Status | Why I couldn't test it |
|---|---|---|
| `BPMigration` UE plugin builds in a fresh project (not the source project) | ✅ | Verified against UE 5.2's `TP_Blank` C++ template (~8s clean). |
| `cookRefCandidates` against a real positive case in the wild | ⚠ partial | Detection logic verified with a synthetic positive case. No live BP in the active validation set holds the pattern. Real-world false-positive rate unknown until users report. |
| macOS / Linux behavior | ❌ untested | Verified only on Windows (Git Bash + Python 3.14). The CLI uses `pathlib`/`subprocess`; portability options for the bundled Windows-only `UAssetGUI.exe` (Wine / .NET runtime / native UAssetAPI build / commandlet-only) are documented in `docs/CROSS_PLATFORM.md` but unverified. |
| The `/migrate-bp` end-to-end flow against an LLM agent | ❌ untested | The recipe was extracted from a working internal workflow and translated; no fresh end-to-end run was performed against the OSS package. Non-Claude harness guidance in `docs/LLM_AGNOSTIC.md` is also unverified. |
| UE 5.3+ compatibility | ❌ untested | Plugin's `EngineVersion` is set to `5.2.0`. UAssetGUI serialization output and editor commandlet APIs may differ on 5.3+. Migration check-list in `docs/UE_VERSIONS.md`. |
| Active call paths exercising external object dependencies | ❌ untested | Substantive verification in this project's run was constrained to early-return paths, primitive-only state setup, and component initialization. Cases that need a constructable dependent object (e.g. cast-success calling into a BP-only widget) require engine-level setup and are not exercised here. |
| Delegate broadcast migration end-to-end | ❌ untested | Rule 3's deterministic signature extraction is documented; no live BP with a dispatcher + receiver pair was run through the full cycle. |

If something breaks for you in any of these rows, please open an issue
with the failing command, OS, UE version, and stderr.

---

## Known limitations

- **Per-Blueprint scope.** The recipe migrates one BP at a time and
  surfaces dependent BPs to the user; it does not transitively rewrite
  consumers automatically.
- **Cook-reference detection: positive cases unverified in the wild.**
  The CLI's `cookRefCandidates` implements the recipe Step 2-C-1
  pattern (TSubclassOf / TSoftClassPtr / TSoftObjectPtr / object ref
  with non-empty path default, never used in graph). Logic verified
  against a synthetic case; no live BP in the validation set hit the
  pattern. False-positive rate unknown.
- **Bytecode-less functions are flagged but not auto-corrected.** Step
  1-B2 detection produces a list of functions without bytecode; the
  agent must convert them from the graph dump alone, with reduced
  accuracy. The recipe spells out the procedure.
- **Some `K2Node` types are not in the C++ mapping table** (Step 4
  rule 7). Uncommon nodes need ad-hoc handling by the agent.
- **Windows shell quirks (Git Bash / MSYS / Cygwin)**: argv path
  mangling and Cygwin-style `/c/...` paths are auto-normalized at
  CLI startup with a one-line `bpmigrate:` warning so you can audit.
  The conservative match guards (only `/Game|Script|Engine|Plugins/`
  prefixes for MSYS, only lowercase single-letter drives for Cygwin)
  produce zero false positives across the 20-case risk-test matrix
  in the verified scope.
- **SCS-overlap BPs (`❌ in-place reparent blocked`)**: a BP whose
  SCS components share names with the new C++ parent's UPROPERTY
  components cannot be reparented in place. `delete_subobjects` on
  a mixed BP-side / inherited handle list raises an
  `Ensure ParentNode` ensure (`SimpleConstructionScript.cpp:942`)
  and the SKEL compile errors with `Tried to create a property X in
  scope SKEL_BP_C, but another object already exists`. **Workaround**:
  Recipe Step 5-A-3 (clean-slate replacement) — create a fresh BP
  child of the C++ class with `BlueprintFactory` and migrate callers
  manually via `Replace References`.
- **BP-defined Interface implementation (`❌ native override blocked`)**:
  if the BP implements a `BI_*_C` BP-defined Interface, the `UFUNCTION`
  on the C++ port collides with the interface override at SKEL compile
  with `Cannot override 'BI_X::Func' ... different signature`.
  BP-defined interfaces don't generate the `IInterface` C++
  scaffolding required for native override. **Workaround**: selective
  migration — leave the interface method bodies BP-side, native-port
  only the non-interface functions. Recipe Step 0.5's
  `analyze-candidate` flags this automatically. Validated on three
  separate BPs in the staging project that each exposed a BP-defined
  interface — all three migrated successfully under the selective rule.
- **Cross-class BP redirect via `consolidate_assets`**: returns `False`
  because the call requires source and target to share the same
  `generated_class`. The clean-slate replacement workflow (Step 5-A-3)
  cannot use this API; the caller-redirect step there relies on the
  user's editor-side `Replace References`. The graph-surgery path
  (`rewrite-callers`) covers the typical migration scenario instead.
- **`compile_blueprint` is a false-positive signal**: it can return PASS
  on a BP whose disk reload fails. Always pair surgery with
  `bpmigrate verify-callers`, which force-unloads each package,
  fresh-loads from disk, and reads `FCompilerResultsLog::NumErrors`.
  The user still opens the editor and runs PIE for end-to-end behavior —
  the toolchain covers everything up to but not including PIE.
