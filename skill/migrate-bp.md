---
description: "Blueprint -> C++ migration (extract BP logic, generate C++ code, verify behavior parity)."
argument-hint: "<BP-name> | <abs uasset path> [--dry-run] [--target-module <module>] [--accept-defaults] [--skip-sanity]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Write", "Edit", "Agent"]
---

# Blueprint -> C++ Migration

Migrate an Unreal Engine Blueprint to a C++ class while preserving runtime
behavior. Drives the `bpmigrate` CLI for analysis, code generation,
sanity checking, and regression verification.

**CRITICAL — completeness rule:**
- Every node, pin, connection, variable, event, and macro of the source
  Blueprint MUST be extracted and translated. Missing logic causes
  divergent behavior in production.
- Do not summarize or omit for token economy; enumerate exhaustively even
  when the result is verbose.
- "Empty"-looking events frequently jump into the UberGraph. Always
  cross-check the bytecode for `ExecuteUbergraph_*` entry points.
- Cross-reference the editor commandlet graph dump (Step 1-A) against the
  bytecode decompile (Step 1-B) to catch gaps.
- Surface unconnected pins, orphan pins, and node comments — distinguish
  intentional disconnection from accidental.

**Arguments:** "$ARGUMENTS"
- First arg: Blueprint name (resolved via configured Content/) or
  absolute `.uasset` path.
- `--dry-run`: stop after Step 3 (the migration plan).
- `--target-module <module>`: target C++ module for the generated class
  (default: the parent class's module).
- `--accept-defaults`: skip the gate-b prompt and use type-natural defaults
  for variables that lack an explicit default. Gate-c sanity checks still
  run as a catch-all.
- `--skip-sanity`: skip gate-c (dead-mapping detection). Use only when you
  have manually verified the call sites.

---

## Setup

This skill drives the `bpmigrate` CLI. Configure once via env vars (or pass
`--flag` overrides on every CLI call):

- `BPMIGRATION_PROJECT_ROOT` — project root (parent of `Content/`).
- `BPMIGRATION_UPROJECT` — full path to your `.uproject` file.
- `BPMIGRATION_UE_CMD` — full path to `UnrealEditor-Cmd` executable.
- `BPMIGRATION_UASSETGUI` — path to `UAssetGUI.exe` (defaults to bundled).
- `BPMIGRATION_UE_VERSION` — `UE5_2` (default), `UE5_3`, etc.

The UE plugin under `ue-plugin/BPMigration/` must be installed in the
target project for Steps 1-A, 2.5-B, and 6 (which invoke editor
commandlets). See the project README for plugin install steps.

---

## Step 0: argument validation

If no Blueprint argument is provided, ask the user for one and stop. Use
`bpmigrate find-bp <name>` to locate the asset; if multiple matches are
returned, ask the user to pick or supply an absolute path.

---

## Step 0.5: candidate suitability analysis (REPORT BEFORE PROCEEDING)

Before generating any C++, dump the BP and report a suitability matrix to
the user. The toolchain has verified production blockers in two patterns
(see `LIMITATIONS.md`); the user picks **proceed / pick a different BP /
abort** with full context, not after a half-done migration is already on disk.

The 6-criteria evaluation is automated:

```bash
bpmigrate analyze-candidate /Game/Path/<TargetBP>
# emits a markdown table + recommendation (proceed / selective / abort)

bpmigrate plan-rewrite-callers /Game/Path/<TargetBP>
# discovers every caller, counts NameMap hits per BP function,
# ranks non-interface logic functions for native migration
```

Run those first; the tables below explain what each row means in detail
when you need to interpret a borderline case manually.

| Check | Where to look | If hit |
|---|---|---|
| **SCS overlap** with intended C++ parent | `componentsRequired` ∩ parent reflection's UPROPERTY components | ❌ Block: SCS cleanup is not deterministic via the public `SubobjectDataSubsystem` API. Recommend clean-slate replacement (Step 5-A-3) or skip. |
| **BP-defined Interface implementation** | `Interfaces` array entries that resolve to a `BI_*_C` BP class (no native source) | ❌ Block: `UFUNCTION` on the C++ parent triggers `Cannot override 'BI_X::Func' ... different signature` at editor reload. Recommend leaving the interface methods BP-side, or migrate the interface itself first. |
| **Caller surface size** | `find_package_referencers_for_asset` count, classified by reference kind (placement / class-type / member-access) | ⚠ caller count alone is not the disqualifier. What matters is how many callers do **member-access** — graph-surgery cost scales with that, not raw referencer count. |
| **Migration value vs cost** | Function bodies non-trivial? Frequently invoked? C++ reuse needed? | ⚠ A BP whose functions are all empty interface stubs gets the migration cost without the value. Stop unless the toolchain itself is what's being verified. |
| **Variable name collisions** | BP `Variables` whose names match `UPROPERTY`s already on the planned C++ parent | ⚠ Causes `ValidateVariableNames ... already taken` warnings and may chain into compile errors. Plan to delete the BP-side variables or rename. |
| **Caller compile != reload-PASS** | n/a | ⚠ Always finish with `bpmigrate verify-callers` (force-unload + fresh-load + `FCompilerResultsLog::NumErrors`). An in-memory `compile_blueprint` PASS can hide a reload-time failure; the user-driven editor open + PIE smoke is the final verification. |

**Output**: a Markdown table to the user listing each check's verdict
(✅ / ⚠ / ❌), the underlying numbers, and a one-line recommendation —
one of:

- `PROCEED — straightforward case.`
- `PROCEED WITH CARE — review warnings; rewrite-callers + verify-callers required.`
- `PROCEED WITH SELECTIVE MIGRATION — ...` (BP-defined Interface present)
- `PROCEED PER-FUNCTION — surgery one function at a time, verify each.`
- `ABORT in-place reparent — ...` (structural blocker, e.g. SCS overlap)

Do not start Step 1 until the user accepts the recommendation.

---

## Step 1: extract Blueprint logic

Notify the user: `"Step 1/7: extracting Blueprint logic..."`

### 1-A. Editor commandlet graph dump (precise nodes / pins / connections)

```bash
bpmigrate dump-graph "/Game/Path/To/<BP>" -o "<tmp>/<BP>_graph.json"
```

Editor cold-start takes 1-2 minutes. If the editor is currently running
and holds an exclusive lock on the `.uasset`, the commandlet may fail to
load it; close the editor or work on a P4 / git temp copy.

### 1-B. UAssetGUI bytecode decompile (execution logic and literal values)

```bash
bpmigrate uasset-tojson "<abs path>/<BP>.uasset" -o "<tmp>/<BP>.json"
bpmigrate summarize "<tmp>/<BP>.json" --bytecode -o "<tmp>/<BP>.txt"
```

### 1-B2. Detect functions missing from bytecode

**CRITICAL — UAssetGUI limitation:** UAssetGUI does not always emit
`FunctionExport` entries for every Blueprint function. Functions without
`ScriptBytecode` cannot be decompiled.

Common patterns affected:
- `UserConstructionScript` (most Actor BPs).
- Helper functions called only from macros.
- Auto-generated functions like `NotifyOnActorChanged`.
- Some user-defined functions (BP-specific; not predictable from name).

Run the gap detector:

```bash
bpmigrate detect-gaps "<tmp>/<BP>.json" --summary-text "<tmp>/<BP>.txt"
```

The CLI emits a JSON report with `missingFunctionExports` and
`referencesBodyGap` lists. If `missingFunctionExports` is non-empty,
notify the user:

```
WARNING: N functions have no bytecode (UAssetGUI limitation):
  - <list>
These must be migrated using the Step 1-A graph dump only. Pay extra
attention to branch conditions and literal values when reading those
functions; flag them in the migration plan (Step 3).
```

**Translating bytecode-less functions:**
1. Use the commandlet graph dump for node types, pin connections, and pin
   default values.
2. Follow `K2Node_IfThenElse.Condition` pin links to reconstruct branches.
3. Use `K2Node_CallFunction.FunctionReference` for call targets.
4. Read each pin's `DefaultValue` for literal values.
5. Use Exec-pin connection order for the execution flow.
6. **Show the converted body to the user and request a verification pass
   equivalent to the bytecode-backed functions.**

### 1-C. UberGraph empty-event verification

In the bytecode, `129(offset)` or `24(offset)` operations are
`EX_ComputedJump` entries into the UberGraph. Events that look empty in
the editor often have logic in the UberGraph.

Verification:
1. Look for `ExecuteUbergraph_*` functions in the bytecode summary.
2. Map each post-`SWITCH` offset back to the originating event.
3. For each "empty" event with logic in the UberGraph, decompile that
   logic and merge it into the per-event view in Step 1-D.

### 1-D. Unified summary

Combine all sources for the user:
- **Graph dump** (1-A): node placement, connections, broken/orphan pins,
  node comments.
- **Bytecode** (1-B): execution order, branch conditions, literal values,
  parameter passing.
- **UberGraph mapping** (1-C): event -> offset -> actual logic.
- **Bytecode-less functions** (1-B2): show in a separate section so the
  user knows accuracy is lower.
- **Required state surfaces** (1-E, below): components, class flags,
  parent CDO overrides, dead inputs, orphan pins.

```
=== Functions WITHOUT bytecode (graph dump only) ===
WARNING: graph-only conversion; verify branch conditions and literals.

  - <Function>: <node summary from graph dump>
```

### 1-E. Required state surfaces (completeness gate)

Run `bpmigrate detect-gaps --graph <graph>.json` and surface every
non-empty field below to the user. Each is something the bytecode
summary does not cover but the migration MUST address — otherwise the
runtime state silently diverges from the BP.

| Field | What it means | Migration obligation |
|---|---|---|
| `componentsRequired` | BP-added components (SimpleConstructionScript) with attach hierarchy. | Recreate every component in the C++ constructor with `CreateDefaultSubobject<T>` and `SetupAttachment`. Order matters; leaf nodes attach to their `parent` field's component. See Step 4 rule 12. |
| `parentCDOOverrides` | Inherited fields the BP set in Class Defaults that differ from the parent class default (e.g. `PrimaryActorTick.bCanEverTick=true`, `RootComponent`, replication flags). | Initialize each in the C++ constructor (or, when applicable, via UPROPERTY default). See Step 4 rule 13. |
| `classFlags` | UCLASS-level flags set on the BP (`Abstract`, `NotPlaceable`, `Hidden`, etc.). | Reflect in the migrated `UCLASS()` specifier. See Step 4 rule 14. |
| `orphanPins` | Pins UE marks `bOrphanedPin=true` (target was renamed/removed). | Stop and request the user clean these up in the editor before continuing. The migration cannot infer intent. |
| `unconnectedDataInputs` | Data input pins with no link AND no DefaultValue (silent zero-valued inputs). | For each, ask the user whether the pin's zero/empty default is intentional or a bug. Do not silently pass the natural default. |
| `brokenReferences` | K2Nodes whose member reference fails to resolve against the BP's current parent class. The editor would draw these red. Covered refKinds: `function` (CallFunction), `variable` (VariableGet/Set), `castTarget` (DynamicCast), `eventOverride` (Event), `macro` (MacroInstance), `delegate` (Add/Remove/Clear/Call/Assign delegate ops), `createDelegate` (CreateDelegate), `asyncTask` (latent action's ProxyClass/ProxyFunction). | Most common after a reparent: the BP still references members on the old base. Stop and run Layer 2 (`bpmigrate map-broken-refs`, see Step 5-A-2) to classify auto-fixable vs needs-user-mapping. Generating C++ on top of a graph with broken refs produces logic that is silently missing those calls/reads. |
| `unauditedK2Nodes` | K2Node kinds present in the graph but NOT covered by DumpBPGraph's `Resolved` emit -- the toolchain cannot tell if these nodes have broken references. Per-class summary with `count`, `exampleGraph`, `exampleTitle`, and `action`. Empty in the validation set. | If non-empty, `brokenReferences=[]` may be a false-clean. Treat the listed K2Node classes as "unknown coverage" and either (a) extend DumpBPGraphCommandlet.cpp per the `action` text and re-dump, or (b) manually inspect those nodes in the editor before treating the BP as migration-ready. |

Stop and present this report **before** Step 2. The user must
acknowledge or address each non-empty list — the migration plan in
Step 3 will repeat them as gated rows. **Do not generate a plan that
omits any of these.**

---

## Step 2: C++ backing class analysis + impact survey

Notify the user: `"Step 2/7: analyzing existing C++ + impact..."`

### 2-A. Locate the parent class and classify

Take the `Parent Class` from the Step 1-D summary. Find the C++ header:

```bash
grep -rn "class.*U<ParentClass>\b\|class.*A<ParentClass>\b" \
  "$BPMIGRATION_PROJECT_ROOT/Source/" --include="*.h" -l
```

**Type classification** (drives C++ class shape):

| Parent | Category | Notes |
|---|---|---|
| ActorComponent / SceneComponent | Component | Must attach to an owner Actor; cannot SpawnActor. |
| Actor / Character / Pawn | Actor | SpawnActor possible. Verify root component. |
| UserWidget | Widget | Keep widget tree in BP; migrate logic only. |
| BlueprintFunctionLibrary | FunctionLibrary | static methods only, no instance state. |
| Object | Plain UObject | Construct via NewObject. |

### 2-B. What is already in C++

From the parent class header:
- Existing `UFUNCTION` declarations.
- Existing `UPROPERTY` declarations.
- Delegate declarations.
- Include layout, owning module.

### 2-C. Migration targets

Diff the BP summary against the parent C++:

- **BP-only function** -> migration target.
- **BP-only variable** -> add `UPROPERTY`.
- **BP-only event handler** -> override or delegate-binding target.
- **BP function calling existing C++** -> no migration needed; preserve
  the call.
- **Calls into other BP classes** (e.g. `BP_Other_C::DoSomething`) ->
  record as a dependency.
- **`UserConstructionScript`** -> migrate to C++ `OnConstruction()`. Do
  not confuse this with constructor / CDO defaults; BP `Default Value` is
  CDO, while the dynamic setup in `ConstructionScript` belongs in
  `OnConstruction()`.

#### 2-C-1. Cook-only reference variables (CRITICAL — packaged-build hazard)

**Background**: some BP variables are never read or written by graph
logic. They exist purely to keep a `TSoftClassPtr` / `TSubclassOf`
default value in the BP's import table so the cook walker tracks the
dependency. This pattern is needed when the native CDO holds the
soft-pointer default but UE 5.2's cook walker fails to follow it from
native — e.g. a `UGameInstanceSubsystem`'s `TSoftClassPtr` default. The
BP variable's import table entry is what keeps the package alive in the
cook.

**Detection criteria** (all three must hold):
1. The variable is never read or written in any function body, macro, or
   `ExecuteUbergraph` (search the `summarizer` text).
2. Variable type is one of: `TSubclassOf<>`, `TSoftClassPtr<>`,
   `TSoftObjectPtr<>`, or a UObject reference.
3. The default value is a non-empty class / object path (not `False`,
   `True`, `0`, `None`, or `nullptr`).

**Handling** (do NOT simply delete as "unused"):

```
For each detected cook-ref variable:

1. Add a matching UPROPERTY to the C++ base class (the parent of this BP):
     UPROPERTY(EditDefaultsOnly, Category="<sensible category>")
     TSubclassOf<U<X>> <VariableName>;

2. Remove the variable from the BP's NewVariables list (it is now inherited).

3. Override the inherited UPROPERTY default on the BP's ClassDefaultObject
   to the same path. The BP's import table will continue to register the
   package, so the cook walker keeps tracking it (verified mechanism).

4. Add an entry to the Step 5 "manual editor work" list: "verify the BP's
   ClassDefaults override of the inherited property is preserved".
```

**Failure mode if mishandled**: the modal/widget is missing in packaged
builds only. Editor and PIE behave correctly, so the regression slips past
review and is found in stage / QA — expensive to bisect.

`bpmigrate detect-gaps --summary-text` emits a `cookRefCandidates` list;
supplement with the manual check above when the matcher reports empty
(false-positive rate unknown — see LIMITATIONS).

### 2-D. Event dispatcher binding survey

**CRITICAL**: when the BP exposes an event dispatcher, other BPs / C++ may
already bind to it. **Survey before generating any code**:

```bash
# Search other BP JSONs (if you've previously dumped them)
grep -rl "<DispatcherName>" "<tmp>"/*.json

# Search C++ source
grep -rn "<DispatcherName>\|AddDynamic.*<DispatcherName>" \
  "$BPMIGRATION_PROJECT_ROOT/Source/" --include="*.cpp" --include="*.h"
```

If any binding is found, notify the user:

```
WARNING: <N> BP/C++ files bind to '<DispatcherName>'. Replacing with a
C++ delegate requires re-binding in those files:
  - <list>
Deleting the BP dispatcher and exposing a C++ UPROPERTY with the same
variable name usually re-binds automatically, but some nodes may break.
```

### 2-E. External reference check

Find files that reference this Blueprint:

```bash
grep -rn "<BP name>" \
  "$BPMIGRATION_PROJECT_ROOT/Source/" "$BPMIGRATION_PROJECT_ROOT/Plugins/" \
  --include="*.cpp" --include="*.h"

grep -l "<BP name>" "<tmp>"/*_graph.json
```

If another BP uses this BP as a component, notify the user:

```
WARNING: BPs using <BP name> as a component: <list>
Deleting the BP removes that component reference; the C++ component must
be re-added in the editor on each consumer. The pin type also changes
(BP_X -> UX), so existing pin connections may break.
```

---

## Step 2.5: Blueprint behavior capture (regression-test expected values)

Notify the user: `"Step 2.5/7: capturing BP behavior trace..."`

**CRITICAL**: do this BEFORE deleting / modifying the BP. After deletion
the trace cannot be captured.

### 2.5-A. Auto-generate the test scenario

```bash
bpmigrate scenario "<tmp>/<BP>.json" --graph "<tmp>/<BP>_graph.json" \
  -o "<tmp>/<BP>_scenario.json"
```

The bytecode analysis classifies functions by importance:
- **thorough**: state-mutating + branching + delegate `Broadcast` -> call
  multiple times with edge values.
- **smoke**: pure getter -> call once.

Execution order is auto-resolved: producers (e.g. `RegisterActions`) ->
getters -> consumers (e.g. `Undo` / `Redo`) -> cleaners (e.g.
`ClearStack`).

Show the generated scenario to the user. They can edit and pass `-o` to
the next step.

### 2.5-B. Capture the trace

```bash
bpmigrate snapshot "/Game/Path/To/<BP>" \
  --scenario "<tmp>/<BP>_scenario.json" \
  -o "<tmp>/<BP>_behavior.json"
```

Editor cold-start: 1-2 minutes. The output `<BP>_behavior.json` records
the `UPROPERTY` snapshot and per-step function outputs.

---

## Step 3: migration plan

Notify the user: `"Step 3/7: drafting migration plan..."`

If `--dry-run`, stop after this step.

Render the plan and **wait for user confirmation** before proceeding:

```markdown
# <BP> -> C++ migration plan

## Affected files
- modify: <header path> (UPROPERTY/UFUNCTION additions)
- modify: <cpp path> (function bodies)
- (when needed) new: <new file path>
- (when needed) modify: <Build.cs path> (module dependencies)

## Migration items
Include EVERY entry from Step 1-E (`detect-gaps` report) as a row here.
Variables / functions / dispatchers are listed below; components,
class flags, and parent CDO overrides MUST also appear with their own
rows so nothing is silently dropped.

| # | BP item | C++ target (with initializer) | Conversion | Source | Default origin | Notes |
|---|---|---|---|---|---|---|
| 1 | Var: Foo | `UPROPERTY(BlueprintReadWrite) TArray<FBar> Foo` | direct | - | type natural (TArray empty) | |
| 2 | Var: useMinCap | `UPROPERTY(EditAnywhere) bool useMinCap = true` | direct | - | from CDO override | |
| 3 | Var: MaxUIValue | `UPROPERTY(EditAnywhere) double MaxUIValue = 100.0` | direct | - | inferred from tooltip via P1 | |
| 4 | Var: TempValue | `UPROPERTY(EditAnywhere) double TempValue = 0.0` ⚠ | direct | - | **⚠ NONE — needs user input** | type-natural placeholder |
| 5 | Event: BeginPlay | `void BeginPlay() override` | override | bytecode | (n/a) | preserve `Super` call |
| 6 | Func: FindRoute | `UFUNCTION(BlueprintCallable) FRoute FindRoute()` | new | bytecode | (n/a) | |
| 7 | Func: Trace | `UFUNCTION() void Trace()` | new | **graph-only** | (n/a) | needs manual verification |
| 8 | Dispatcher: OnFoo | `UPROPERTY(BlueprintAssignable) FOnFoo OnFoo` | delegate | - | (delegate) | bound by N other BPs |

Source legend:
- **bytecode**: bytecode + graph dump (high accuracy)
- **graph-only**: graph dump only (no bytecode; manual verification needed)

Default-origin legend (see Step 4 rule 10):
- **from CDO override**: extracted from `Default__<BP>_C` Data
- **from BP NewVariable.DefaultValue**: extracted from the variable's DefaultValue field
- **inferred from tooltip via P<n>**: tooltip regex match
- **type natural (...)**: type-natural default; only label this when the value is unambiguously meaningful for the variable's role
- **⚠ NONE — needs user input**: gate-b trigger

## ⚠ Default-origin gate (gate b)

The N variables marked ⚠ have no extractable default. Without input from
you, they will be initialized with type-natural defaults (0.0 / false /
empty) which may diverge from BP intent. Decide each before Step 4:

```
Variable: <name> (<type>)
  - graph usage: <1-3 use sites from summarizer>
  - tooltip text (if any): "<MetaDataArray.tooltip>"
  - options:
    (1) accept type-natural default (e.g. double=0.0)
    (2) supply an explicit value (you provide)
    (3) add a BP CDO override (most robust; also keeps cook tracking stable)
selection: _____
```

Step 4 cannot proceed until each ⚠ has a response. The selected value
becomes the C++ initializer (option 1 = natural, option 2 = literal,
option 3 = natural in code, with a "set CDO override" task added to
Step 5).

**Bypass**: passing `--accept-defaults` resolves all ⚠ variables to
option 1. Gate c (Step 4.5) still runs as a catch-all sanity check.

## Components (from Step 1-E `componentsRequired`)
| Name | Class | Attach parent | Attach native parent |
|---|---|---|---|
| <Name> | <Class> | <Parent component name> | <true/false> |

(See Step 4 rule 12 for constructor generation.)

## Parent CDO overrides (from Step 1-E `parentCDOOverrides`)
| Property | Type | BP value | Parent default |
|---|---|---|---|
| <Name> | <Type> | <BP value> | <Parent value> |

(See Step 4 rule 13 for constructor propagation.)

## Class flags (from Step 1-E `classFlags`)
- <Flag> -- reflected as `UCLASS(<spec>)` (rule 14)

## Event dispatcher -> C++ delegate map
| BP dispatcher | C++ variable name (same) | Signature | Existing binders |
|---|---|---|---|
| <Name> | <Name> | F<...>(<params>) | <bound BPs / C++> |

## BP dependencies
- BPs / C++ that reference this BP: <list>
- BPs that this BP references: <list>

## Manual editor work (after C++ generation)
- [ ] Delete CustomEvent nodes in the BP (the C++ functions take over).
- [ ] Delete event dispatchers in the BP (replaced by C++ delegates).
- [ ] Reconnect broken nodes in consumer BPs.
- [ ] (When deleting the BP entirely) re-add the C++ component on each
      consumer.

## Notes
- <any caveats specific to this migration>

Proceed?
```

---

## Step 4: generate C++

After user confirmation. Notify: `"Step 4/7: generating C++ code..."`

### Code-generation rules

1. **Follow project conventions.**
   - Match existing delegate prefixes (e.g. `FGI_` for GameInstance,
     `FOn` for events) — discover these from the parent class's module.
   - Match `UFUNCTION Category` choices.
   - Match `UPROPERTY` access-specifier patterns.
   - Prefer forward declarations over `#include` for cross-class
     references; minimize includes to avoid circular dependencies.

2. **Preserve `BlueprintCallable`** for functions other BPs can invoke;
   drop it only for BP-internal helpers (and confirm with user).

3. **Event dispatcher -> C++ delegate (DETERMINISTIC signature extraction)**.
   - Use `DECLARE_DYNAMIC_MULTICAST_DELEGATE_*`.
   - **The C++ variable name MUST match the BP dispatcher name exactly**
     so existing bindings auto-reconnect.
   - Parameter names, types, and order must match the BP's signature.

   **Signature extraction procedure (do not infer with the LLM):**
   1. Identify the BP `NewVariable` whose `VarType.PinCategory` is
      `mcdelegate`.
   2. Find the `<DispatcherName>__DelegateSignature` (or
      `<DispatcherName>_<Hash>__DelegateSignature`) `FunctionExport`.
      Fallback: the function reference of any `K2Node_CreateDelegate`
      that points at this dispatcher.
   3. Walk that `FunctionExport`'s `ChildProperties` (or
      `LoadedProperties`) for parameters:
      - `PropertyFlags` with `Parm` (0x80) -> parameter
      - `OutParm` (0x100) or `ReturnParm` (0x400) -> output parameter
        (multicast delegates usually have inputs only)
      - Extract each parameter's `PinCategory` and name.
   4. Pick the macro by parameter count:
      - 0: `DECLARE_DYNAMIC_MULTICAST_DELEGATE`
      - 1: `..._OneParam`
      - 2: `..._TwoParams`
      - up to 9 (UE macro limit).
   5. Extraction failure (no `FunctionExport`, or parameter parsing
      failed) -> mark ⚠ and trigger gate b: the user must supply the
      signature before proceeding. Never guess.

4. **`CustomEvent` -> `UFUNCTION` switch.**
   - The BP `CustomEvent` node MUST be deleted from the graph after
     migration; otherwise the C++ function is never invoked. Step 5
     covers the manual deletion.

5. **Migration provenance comment.**
   - On every migrated function:
     ```cpp
     // Migrated from BP: <BP name>::<FunctionName>
     ```

6. **Null access in BP returns defaults.**
   - In BP, calling a method on a null object does NOT crash — it returns
     the type-natural default (0, `FVector::ZeroVector`, false, ...) and
     the surrounding computation continues.
   - Adding a C++ `nullptr` guard with `early return` changes behavior.
   - **Match BP semantics**: substitute the type-natural default and
     continue.

   **Code template (DETERMINISTIC — DO NOT early-return):**

   For every BP node whose target pin can be null (cast result, getter,
   spawn result), translate as a ternary that yields the type-natural
   default when the target is null, then continue the computation:

   ```cpp
   // BAD — diverges from BP (skips downstream computation entirely):
   if (!CamMgr) return;
   const FVector CamLoc = CamMgr->GetCameraLocation();
   const float Dist = FVector::Dist(CamLoc, MyLoc);
   // ... rest of function ...

   // GOOD — matches BP null-cascade (downstream still runs):
   APlayerCameraManager* CamMgr = UGameplayStatics::GetPlayerCameraManager(this, 0);
   const FVector CamLoc = CamMgr ? CamMgr->GetCameraLocation() : FVector::ZeroVector;
   const float Dist = FVector::Dist(CamLoc, MyLoc);
   // ... rest of function continues even when CamMgr was null ...
   ```

   **Type-natural defaults table (use exactly these):**

   | Return type | Default expression |
   |---|---|
   | `bool` | `false` |
   | `int32` / `int64` | `0` |
   | `float` / `double` | `0.0f` / `0.0` |
   | `FVector` / `FVector2D` / `FVector4` | `FVector::ZeroVector` / `FVector2D::ZeroVector` / `FVector4::Zero()` |
   | `FRotator` | `FRotator::ZeroRotator` |
   | `FQuat` | `FQuat::Identity` |
   | `FTransform` | `FTransform::Identity` |
   | `FLinearColor` / `FColor` | `FLinearColor::Black` / `FColor::Black` |
   | `FString` / `FName` / `FText` | `FString()` / `NAME_None` / `FText::GetEmpty()` |
   | `T*` (any pointer) | `nullptr` |
   | `TArray<T>` | `TArray<T>()` |
   | `TMap<K,V>` / `TSet<T>` | `TMap<K,V>()` / `TSet<T>()` |
   | `TSubclassOf<U>` | `nullptr` |
   | enum class | `static_cast<E>(0)` (BP uses 0-init for enums) |

   **When the cascading value is reused in multiple statements**, hoist
   to a local with the ternary; do NOT repeat the null-check inline:

   ```cpp
   // GOOD — single null-check, reused:
   const FVector CamLoc = CamMgr ? CamMgr->GetCameraLocation() : FVector::ZeroVector;
   DoA(CamLoc);
   DoB(CamLoc, OtherArg);
   ```

   **Snapshot verification will catch divergence.** If the BP function
   was supposed to early-bail (e.g. only on a `IsValid` Branch node),
   that Branch is in the graph dump — translate the explicit branch.
   Implicit early-return based on null is what diverges.

7. **BP node -> C++ mapping**:

   | BP node | C++ |
   |---|---|
   | CallFunction | direct call |
   | Branch | `if / else` |
   | ForEachLoop | `for (auto& Elem : Array)` |
   | SwitchString | `if / else` chain or `TMap` lookup |
   | DynamicCast | `Cast<UClass>(Object)` + `nullptr` check |
   | Map_Find | `TMap::Find()` |
   | Map_Add | `TMap::Add()` |
   | Set_Contains | `TSet::Contains()` |
   | GetActorOfClass | `UGameplayStatics::GetActorOfClass()` |
   | MakeLiteralGameplayTag | `FGameplayTag::RequestGameplayTag()` |
   | Sequence | sequential statements (default in C++) |
   | WhileLoop | `while () {}` |
   | IncrementInt / DecrementInt | `Var++ / Var--` |
   | Delay | **Latent action** -- `GetWorld()->GetTimerManager().SetTimer()` + callback split |
   | DelayUntilNextTick | `GetWorld()->GetTimerManager().SetTimerForNextTick()` |
   | K2_SetTimer | `SetTimer(Handle, this, &Class::Func, Time, bLoop)` |

8. **Graph-only function translation** (from Step 1-B2).
   - Use only the commandlet graph dump.
   - Extract Exec-pin order, `IfThenElse.Condition` links,
     `CallFunction.FunctionReference`, `VariableGet/Set` names, and pin
     `DefaultValue`s.
   - Emit a comment so the reviewer knows accuracy is lower:
     ```cpp
     // WARNING: migrated from graph dump only (no bytecode).
     // Verify branch conditions and literal values manually.
     // Migrated from BP: <BP>::<FunctionName>
     ```

9. **Build.cs module dependencies.**
   - Audit included types for module ownership:
     - `FJsonObjectWrapper` -> `Json`, `JsonUtilities`
     - `FGameplayTag` -> `GameplayTags`
     - `UUserWidget` -> `UMG`
   - Add to `PrivateDependencyModuleNames` when not already present.

10. **Variable default extraction (DETERMINISTIC priority — no LLM inference).**

    Resolve every BP `NewVariable`'s C++ initializer using these
    priorities. Use only the explicit data lookups and regexes below; no
    natural-language reasoning at any step.

    **Priority 1 — CDO override**

    If `Default__<BP>_C.Data` has an entry whose name matches the
    variable, use its `Value`.
    Origin label: `from CDO override`.

    **Priority 2 — `NewVariable.DefaultValue`**

    If `BPVariableDescription.DefaultValue` (StrPropertyData) has a
    non-null/non-`None` value, parse and use it.
    Origin label: `from BP NewVariable.DefaultValue`.

    **Priority 3 — tooltip regex**

    Apply the patterns below to the variable's `MetaDataArray` tooltip
    in order. Use the first match's capture group 1.

    ```python
    PATTERNS = [
        (1, r"(?:percentage|percent)\s*(?:case|context)?\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)"),
        (2, r"(?:default|default\s*value|initial\s*value)\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)"),
        # only when the variable name contains Max:
        (3, r"(?:max\s*value|maximum)\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)"),
        # only when the variable name contains Min:
        (4, r"(?:min\s*value|minimum)\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)"),
    ]
    ```

    Origin label: `inferred from tooltip via pattern <n>: "<excerpt>"`.
    Place a one-line excerpt as a comment above the generated UPROPERTY.

    **Priority 4 — type-natural default + ⚠ marking (gate-b trigger)**

    If priorities 1-3 all miss, fall back to the type-natural default
    (`bool=false`, `int/double=0`, `FText=FText::GetEmpty()`, `enum=0`,
    `FString=""`) and mark the variable ⚠ for the gate-b prompt.
    Origin label: `⚠ type natural default — REQUIRES USER INPUT`.

    **Variable-name sanitization (DETERMINISTIC).**

    If `NewVariable.VarName` contains characters invalid in a C++
    identifier (spaces, etc.):
    1. Strip ASCII whitespace, tabs, hyphens
       (`Max UI Value` -> `MaxUIValue`).
    2. If the result starts with a digit, prepend `Var_`.
    3. If the result clashes with a C++ reserved word, append `_`.
    4. Use the sanitized name as the C++ identifier and preserve the
       BP-facing display name with `meta = (DisplayName = "<original>")`.

    Origin label suffix:
    `name sanitized: "<original>" -> "<C++ identifier>"`.

11. **Variable type resolution (DETERMINISTIC, enums / structs / object refs).**

    `BPVariableDescription.VarType` is a base64-encoded
    `EdGraphPinType` blob. Steps to resolve to a C++ type:

    1. **PinCategory direct mapping** for primitives:

       | PinCategory | C++ |
       |---|---|
       | bool | `bool` |
       | int | `int32` |
       | int64 | `int64` |
       | real (subcat=double) | `double` |
       | real (subcat=float) | `float` |
       | string | `FString` |
       | name | `FName` |
       | text | `FText` |

    2. **Enum (PinCategory `byte` or `enum`)**:
       - Index `PinSubCategoryObject` into the import table
         (`Imports[abs(idx)-1].ObjectName`).
       - C++: enum class -> use the name; native byte enum ->
         `TEnumAsByte<E...>`.
       - Index 0 or missing import -> mark ⚠.

    3. **Struct (PinCategory `struct`)**:
       - Resolve `PinSubCategoryObject` -> struct name.
       - Standard UE structs -> `F<Name>` (e.g. `FVector`,
         `FJsonObjectWrapper`).
       - Project `USTRUCT` -> use the name verbatim; ensure the owning
         module is in `Build.cs` (rule 9).

    4. **Object / class references**:
       - `object` -> `T<ClassName>*` (e.g. `UTexture2D*`).
       - `class` -> `TSubclassOf<U<ClassName>>`.
       - `softobject` -> `TSoftObjectPtr<U<ClassName>>`.
       - `softclass` -> `TSoftClassPtr<U<ClassName>>`.
       - Index 0 / missing import -> mark ⚠.

    Resolution failure -> mark ⚠ and route through the same gate-b flow
    as a missing default. Never guess.

12. **Component constructor generation** (DETERMINISTIC).

    For every entry in `componentsRequired` (from Step 1-E):

    ```cpp
    // In the class constructor:
    <Name> = CreateDefaultSubobject<U<Class>>(TEXT("<Name>"));
    ```

    Then attachment, in topological order (parents before children):

    - If `parentIsNative == true` and the parent name is `RootComponent`
      OR the parent is a known native UE component on this class, attach
      via `<Name>->SetupAttachment(RootComponent);` (or the named native
      component).
    - If `parent` is another entry in `componentsRequired`, attach via
      `<Name>->SetupAttachment(<ParentName>);`.
    - The first scene component (typically `DefaultSceneRoot`) becomes
      `RootComponent`: `RootComponent = <FirstSceneComp>;`.

    **Default-override propagation (DETERMINISTIC — no LLM inference).**

    Each `componentsRequired[i]` entry carries a `defaultOverrides[]`
    array (from `DumpBPGraph` SCS dump). Every entry MUST be applied in
    the constructor; silent omission is forbidden (snapshot verify will
    catch as `valueMismatch`).

    Apply each `defaultOverrides[i]` using the matrix below in order
    (first match wins). `<Name>` is the component variable, `Property`
    is the override key, `OurValue` is the override value (string-form
    from JSON; parse to literal).

    | Property pattern | Emit |
    |---|---|
    | `RelativeLocation` | `<Name>->SetRelativeLocation(FVector(<x>, <y>, <z>));` |
    | `RelativeRotation` | `<Name>->SetRelativeRotation_Direct(FRotator(<pitch>, <yaw>, <roll>));` |
    | `RelativeScale3D` | `<Name>->SetRelativeScale3D(FVector(<x>, <y>, <z>));` |
    | `StaticMesh` | `<Name>->SetStaticMesh(LoadObject<UStaticMesh>(nullptr, TEXT("<path>")));` |
    | `OverrideMaterials[i]` | `<Name>->SetMaterial(<i>, LoadObject<UMaterialInterface>(nullptr, TEXT("<path>")));` |
    | `BodyInstance.CollisionEnabled` | `<Name>->SetCollisionEnabled(ECollisionEnabled::<value>);` |
    | `BodyInstance.CollisionProfileName` | `<Name>->SetCollisionProfileName(FName(TEXT("<value>")));` |
    | `BodyInstance.ObjectType` | `<Name>->SetCollisionObjectType(<value>);` |
    | `bVisible` / `bHiddenInGame` | `<Name>->SetVisibility(<bool>);` / `<Name>->SetHiddenInGame(<bool>);` |
    | `CastShadow` | `<Name>->SetCastShadow(<bool>);` |
    | `Mobility` | `<Name>->SetMobility(EComponentMobility::<value>);` |
    | UPROPERTY with `BlueprintReadWrite` or public access | `<Name>-><Property> = <literal>;` (direct field assignment) |
    | UPROPERTY without public setter and not in table above | mark ⚠ — gate-b prompt; DO NOT silently drop |

    **Critical: `SetRelativeRotation` vs `SetRelativeRotation_Direct`.**
    `SetRelativeRotation(FRotator)` converts to `FQuat` then back, which
    re-normalizes and CAN change the rotator (e.g. `Yaw=-90` snaps to
    `Yaw=-26.565` in gimbal-lock-adjacent values). For migration parity
    with the BP CDO override, ALWAYS use `SetRelativeRotation_Direct` to
    preserve the exact `FRotator` triple.

    **Private-API override map.** Some component setters are protected
    or private — call via the parent-class scope:

    | Component / Class | Member | C++ access |
    |---|---|---|
    | `APlayerCameraManager` | `GetActorLocation()` | `Cam->AActor::GetActorLocation()` |
    | `APlayerCameraManager` | `GetActorRotation()` | `Cam->AActor::GetActorRotation()` |
    | `UPrimitiveComponent` | direct `BodyInstance` mutation | use `Set...` setter where one exists; `BodyInstance.<field> = ...;` is allowed inside the constructor only |

    Extend this table when a new private-access case is found; do NOT
    work around it with `friend` declarations or local subclasses.

    Failing to recreate components OR apply overrides causes BP -> C++
    runtime divergence on initial state (verify `valueMismatch` on
    `RootComponent` / `BlueprintCreatedComponents.length` /
    `<Name>.<Property>`).

13. **Parent CDO override propagation** (DETERMINISTIC).

    For every entry in `parentCDOOverrides`:

    - **Struct overrides** (e.g. `PrimaryActorTick`): assign each
      changed sub-field in the constructor:
      ```cpp
      PrimaryActorTick.bCanEverTick = true;
      ```
    - **Inherited UPROPERTY overrides** (e.g. anim instance class):
      assign in constructor or use a class-level default.
    - **Editor-only fields** (e.g. `ActorLabel`): the BP's value applies
      only inside the editor; for runtime parity it can be skipped, but
      surface it in the migration plan and ask the user.

    Each override MUST appear as either a constructor assignment or an
    explicit "skip — editor-only" entry. Silent omission is forbidden.

14. **Class flag propagation** (DETERMINISTIC).

    For every flag in `classFlags`, mirror in the C++ `UCLASS()`
    specifier:

    | Flag | UCLASS specifier |
    |---|---|
    | Abstract | `Abstract` |
    | NotPlaceable | `NotPlaceable` |
    | DefaultConfig | `DefaultConfig` |
    | Const | `Const` |
    | Hidden | `Hidden` |
    | Deprecated | `Deprecated, deprecationMessage="..."` |

    Empty `classFlags` -> `UCLASS()` plain.

### Generation order

1. Header: `UCLASS(<flags>)`, delegate macros, component `UPROPERTY`s,
   variable `UPROPERTY`s, `UFUNCTION` declarations.
2. Cpp constructor: `CreateDefaultSubobject` for each component +
   `SetupAttachment` (rule 12), parent CDO overrides (rule 13).
3. Cpp: function bodies (including `Broadcast` calls).
4. `Build.cs`: module dependency additions (when needed).
5. Print a summary of generated code to the user.

**Prefer the deterministic generators over hand-typing the corresponding
sections**. Each is a fixed mapping from JSON to C++ -- same input, same
output, no LLM in the loop:

```sh
bpmigrate emit-class-flags         <graph.json>             # UCLASS(...)
bpmigrate emit-dispatcher-delegates <uassetgui.json>        # DECLARE_DYNAMIC_MULTICAST_DELEGATE_*
bpmigrate emit-variable-defaults   <uassetgui.json>         # UPROPERTY <type> <Name> = <default>;
bpmigrate emit-component-overrides <graph.json>             # constructor body
```

It walks `componentsRequired[*].defaultOverrides` and applies the
exact Rule 12 mapping table — `SetRelativeRotation_Direct` for
rotators, `LoadObject<T>` for asset refs, partial `BodyInstance`
sub-field setters, `// TODO:` comments for unmapped properties (so
nothing is silently dropped). Paste the output into the constructor.

---

## Step 4.5: static sanity check (gate c)

Notify: `"Step 4.5/7: static sanity check on generated code..."`

Substitute the generated initializers into the BP graph's call sites and
detect dead-mapping / no-op patterns. No execution; pure static graph +
initializer analysis. Catch-all that fires regardless of user intent.

### 4.5-A. Build the `effective_defaults` table

Parse every `UPROPERTY` initializer in the new header into
`{name: value}`. This combines gate-b user input with gate-a extraction
results.

### 4.5-B. Scan risky calls

In the BP graph (Step 1) plus every function body, grep for:

```
KismetMathLibrary::MapRangeClamped
KismetMathLibrary::MapRangeUnclamped
KismetMathLibrary::Lerp_Double / Lerp
KismetMathLibrary::Clamp_Double / FMath::Clamp
KismetMathLibrary::FInterpTo
```

For each call, look up variable arguments in `effective_defaults`; use
literals as-is.

### 4.5-C. Dead-mapping rules (DETERMINISTIC)

Block any of these matches:

| # | Rule | Block when |
|---|---|---|
| R1 | `MapRange*(value, MinIn, MaxIn, MinOut, MaxOut)` with `MinIn == MaxIn` | every input maps to one output -> dead |
| R2 | `MapRange*(...)` with `MinOut == MaxOut` | constant output (warn only — may be intended) |
| R3 | `Clamp*(value, A, B)` with `A > B` | degenerate clamp (always returns A or B) |
| R4 | `Clamp*(value, A, A)` | always returns A |
| R5 | `Lerp*(A, B, alpha)` with `A == B` | alpha ignored — meaningless (warn only) |

R1 / R3 / R4 block. R2 / R5 warn and require user confirmation.

When triggered, report:

```
⚠ Dead mapping / no-op call detected (Step 4.5):

site: <function> (line <approx>)
call: <pattern>
effective values:
  - <param> = <value> from <origin>
  - ...

Rule R<n> matched: <reason>.

Resolution:
  (1) update the offending variable's effective default (Step 3 option 2 / 3)
  (2) require per-instance override at every call site
  (3) really intended -- pass --skip-sanity (any breakage is on you)
```

R1 / R3 / R4 require explicit (1) / (2) / (3) before Step 5.

### 4.5-D. Cap-toggle false-default warning (R6)

When a boolean toggle (`useMinCap` / `useMaxCap` / `bUse*`) ends up at
`false` and is consumed by:

```
K2Node_MakeStruct_FloatWithBoolean.enabled_<...> = <CapVar>
K2Node_MakeStruct_<...>.bEnabled = <CapVar>
K2Node_MakeStruct_<...>.bClamp = <CapVar>
```

warn that the migrated code disables clamping in the corresponding
native widget (e.g. `USpinBox::SetMinValue` is never called -> text /
drag / wheel inputs are unbounded). Survey the call sites and present:

```
⚠ Clamp-disabled default detected (R6):
  variable: <name> (bool) effective default = false
  use site: <function> bytecode -> FFloatWithBoolean.enabled
  -> after migration the native widget's clamp is inactive.
  pre-migration call sites:
    - <site 1> -- per-instance override? <result>
    - ...
  user decision: (1) flip default to true / (2) keep false (call sites
  intentionally rely on no clamping)
```

R6 is warning-only; require explicit user response before Step 5.

### 4.5-E. Result

- Every R1 / R3 / R4 block resolved AND every R2 / R5 / R6 warning
  acknowledged -> proceed to Step 5.
- Unresolved blockers -> exit with code 1, do not enter Step 5.

---

## Step 4.6: Reparent target identification (gate d)

Notify: `"Step 4.6/7: identifying reparent-target Blueprints..."`

When Step 4 created a new C++ base class (e.g. `MyWidgetBase`), identify
the BPs that should be reparented onto it. Reparent omission is as
common a regression as missing variable defaults; gate it separately.

### 4.6-A. Auto-discovery (DETERMINISTIC)

Use heuristic H1 first; fall back to H2 only if it returns 0 results.
H3 filters whichever set H1 / H2 produced.

**H1 — common-substring exact match (most precise)**

If the new base name is `<Prefix><Common>Base` or `<Common>Base`
(e.g. `MyWidgetBase` -> `Common = MyWidget`), search Content/ for
files whose name contains `Common` verbatim:

```bash
find "$BPMIGRATION_PROJECT_ROOT/Content" -iname "*MyWidget*.uasset"
```

Usually 1-3 results (precise). H2 becomes verification when H1 produces
candidates.

**H2 — prefix match + parent verification (fallback)**

When H1 yields 0 candidates or you need broader coverage:

```bash
find "$BPMIGRATION_PROJECT_ROOT/Content" -iname "<Prefix>*.uasset"
```

Short prefixes have many false positives, so route H2 through H3.

**H3 — `ParentClass` filter**

For each H1 / H2 candidate, run `bpmigrate uasset-tojson` and check the
import table for the new base's parent class. Keep candidates whose
parent class matches:

```bash
for BP in <candidates>; do
  TMP="<tmp>/$(basename $BP .uasset)_parent.json"
  bpmigrate uasset-tojson "$BP" -o "$TMP"
  python -c "
import json, sys
with open(r'$TMP', 'r', encoding='utf-8') as f:
    d = json.load(f)
target = '<the new base's parent class, e.g. UserWidget>'
ok = any(
    str(imp.get('ObjectName','')) == target and imp.get('ClassName','') == 'Class'
    for imp in d.get('Imports', [])
)
print('1' if ok else '0')
"
done
```

**H4 — Direct child of the migrated BP (informational only)**

BPs that consume the migrated BP as a component may be impacted but are
NOT reparent targets. Surface them as informational so the user can
audit pin connections post-migration.

### 4.6-B. User confirmation

Show the candidate list and ask explicitly per BP:

```
A new base class `<BaseName>` was created. Reparent candidates:

  [candidate] <BP name 1>
    - current parent: <parent>
    - name match: <reason>
    - file: <abs path>
    - reparent? (y/N)

  [candidate] <BP name 2>
    ...
```

Wait for an explicit `y` / `N` per candidate. Record `N` decisions in
the report; they are excluded from Step 5 / Step 6 reparent verification.

### 4.6-C. Zero-candidate path

When auto-discovery returns nothing, still confirm with the user:

```
A new base class `<BaseName>` was created but no reparent candidates
were detected automatically. Either:
  (1) really no reparent needed (e.g. abstract base for future use)
  (2) detection rules missed something — provide BP names manually
```

Do not proceed without an explicit response.

### 4.6-D. Hand-off to Step 5 / 6

Pass the confirmed reparent list to:
- **Step 5-A-2** (manual editor work): the per-BP reparent walkthrough.
- **Step 6-A** (regression verification): the static parent-class check.

---

## Step 5: editor manual work + verification

Notify: `"Step 5/7: editor manual-work guide"`

After C++ is generated, lay out the editor steps the user must perform.

### 5-A. Delete from the BP

```markdown
### In <BP> editor:
1. Delete CustomEvent nodes (Event Graph):
   - <Function 1> -- replaced by C++ U<...>::<Function 1>()
   - ...

2. Delete event dispatchers (My Blueprint panel):
   - <Dispatcher 1> -- replaced by C++ UPROPERTY <Dispatcher 1>
   - ...

3. Compile -> Save.
```

#### 5-A-1. Cook-reference variables (DO NOT delete)

For each variable identified in Step 2-C-1:

```markdown
### Cook-reference variable handling for <BP>:
1. Verify the C++ base class (modified / created in Step 4) added the
   inherited UPROPERTY.
2. In the editor, BP > My Blueprint > Variables, delete the BP-local
   variable.
3. In Class Defaults, find the inherited <VariableName> (now coming from
   the base).
4. Set its default value to the same path the BP-local variable held.
5. Compile -> Save.
6. **Verify**: run `bpmigrate uasset-tojson` on the saved BP and confirm
   the dependency package is still listed in the import table. If
   missing, the cook-ref chain is broken; revisit step 4.

⚠ Skipping the base UPROPERTY uplift and just deleting the BP variable
breaks cook tracking. Editor / PIE behave correctly; the regression
surfaces only in packaged builds.
```

#### 5-A-2. Reparent (gate-d outcome)

For each reparent target confirmed in Step 4.6:

**Reparent `<BP>` onto new base `<BaseName>`**:

1. Open the BP in the editor.
2. File -> Reparent Blueprint.
3. In the dialog, search and pick `<BaseName>`.
4. Save (`Ctrl+S`) -> Compile (`F7`).
5. Reconnect any nodes the editor flags as broken.

**Verify** (mandatory):
- Reconcile the BP `.uasset` in source control to confirm the change.
- `bpmigrate uasset-tojson` and grep for `<BaseName>_C` in the import
  table. Absence means reparent did not actually take.
- **Broken-reference scan (Layer 1 — deterministic detect):**
  ```bash
  bpmigrate dump-graph /Game/.../<BP> -o <out>/<BP>_post_reparent.json
  bpmigrate detect-gaps <out>/<BP>.json --graph <out>/<BP>_post_reparent.json \
    -o <out>/<BP>_post_reparent_gaps.json
  ```
  Expected after a clean reparent: `brokenReferences: []`. Any entry
  means the BP still calls / reads members from the old base. The
  editor's red highlight is one signal; this scan is the source of truth.

#### 5-A-2-bis. Inheritance-only fast path (when applicable)

If **both** of these hold for the function being migrated:

1. The BP-side function name has **no spaces** (e.g. `SpawnCharacter`,
   not `Spawn Character`) — i.e. the C++ identifier and the
   `K2Node_CallFunction.MemberName` already match.
2. The target C++ parent already exists (no reparent needed; the BP is
   already a child of a native class).

…then `RewriteCallers` is **not necessary**. The simpler cycle:

1. Add the new `UFUNCTION` to the existing C++ parent (matching pin
   types and names; pass `UPARAM(ref)` for `FTransform&` etc.).
2. Build the C++ module.
3. Open the BP and **delete the BP-side function graph** (or drive it
   from a commandlet: `BlueprintEditorLibrary.remove_graph`).
4. Compile + save the BP.
5. `bpmigrate verify-callers --target=<BP>` — every caller's
   `K2Node_CallFunction(<FuncName>)` now resolves to the inherited C++
   method automatically because the lookup name matches.

When the conditions above hold, prefer this path — it's the smallest
diff and absorbs future caller-graph changes automatically.

If either condition fails (renamed function or reparent needed), fall
back to the standard cycle: `bpmigrate rewrite-callers` plus the rules
in 5-A-3 below.

---

#### 5-A-3. SCS-overlap fallback (clean-slate replacement)

**When**: the BP has SCS components whose names match UPROPERTY components on
the new C++ parent (e.g. BP defines `SUN`, parent defines `SUN`).
**Detect**: intersect the BP's `componentsRequired` (from `dump-graph`) with
the new parent's components reflection. Run **before** Step 5-A-2 reparent.

**Why in-place reparent does not work**: SKEL compile fails with
`Tried to create a property X in scope SKEL_BP_C, but another object
already exists`. Cleanup via `SubobjectDataSubsystem.delete_subobjects`
raises `Ensure ParentNode` (`SimpleConstructionScript.cpp:942`) on the
entangled BP-side / inherited handle list — there is no public-API
recovery path. See `LIMITATIONS.md`.

**Do** (deterministic procedure, every time):

1. **Skip Step 5-A-2 reparent** for this BP.
2. **Create a clean replacement BP** as a child of the C++ class:
   ```python
   factory = unreal.BlueprintFactory()
   factory.set_editor_property('parent_class', cpp_class)
   new_bp = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
       asset_name='<OldBP>_Migrated',
       package_path='<original directory>',
       asset_class=unreal.Blueprint,
       factory=factory,
   )
   unreal.BlueprintEditorLibrary.compile_blueprint(new_bp)
   unreal.EditorAssetLibrary.save_asset(new_bp.get_path_name().split('.')[0])
   ```
   Verify the new BP's CDO exposes the inherited components / variables
   (`get_default_object(generated_class)` then `get_editor_property('SUN')` etc.).
3. **List callers** of the original BP:
   ```python
   refs = unreal.EditorAssetLibrary.find_package_referencers_for_asset(
       '/Game/.../<OldBP>', load_assets_to_confirm=False)
   ```
4. **Redirect each caller** — the toolchain provides `bpmigrate
   rewrite-callers`. It rewrites every `K2Node_CallFunction` that
   targets the old member to point at the new one, preserves
   `DefaultObject` (CDO) on the self pin, and inserts a pure
   DynamicCast for wide→narrow downcasts.

   ```bash
   bpmigrate rewrite-callers \
     --callers=/Game/Path/CallerA,/Game/Path/CallerB,...   \
     --old="<OldClassPath>.<OldFnName>"  \
     --new="<NewClassPath>.<NewFnName>"  \
     --pinmap="OldOutPin=NewOutPin"      \
     --save
   ```

   Caller kinds the CLI does **not** cover (still manual):
   | Caller kind | Editor action |
   |---|---|
   | Map (placement instance) | Outliner → select all instances of the old BP → right-click → Replace Selected Actors With → pick the new BP. Save. |
   | C++ | grep the old class name; manual edit; rebuild. |

5. **Verify** the rewritten callers with the dedicated commandlet:
   ```bash
   bpmigrate verify-callers --target=/Game/Path/<TargetBP> \
     --output=<out>/verify_callers_report.json
   ```
   Force-unloads every package, fresh-loads from disk, recompiles, and
   reads `FCompilerResultsLog::NumErrors`. Exit code = total errors;
   the JSON report has per-caller error/warning detail. Treat any non-zero
   error count as a real failure regardless of what an in-memory
   `compile_blueprint()` reported (the latter is a known false positive —
   see `LIMITATIONS.md`).
6. **Decommission the original BP** (delete or move to a deprecated
   folder) only after every caller verifies clean.

- **Auto-mapping classifier (Layer 2 — deterministic classify):**
  When `brokenReferences` is non-empty, run the classifier to split
  auto-fixable vs needs-user-mapping:
  ```bash
  bpmigrate dump-class-reflection /Script/<Module>.<NewBaseName> \
    -o <out>/<NewBaseName>_reflection.json
  bpmigrate dump-class-reflection /Script/<Module>.<OldBaseName> \
    -o <out>/<OldBaseName>_reflection.json   # optional, enables signature compare
  bpmigrate map-broken-refs \
    --gaps <out>/<BP>_post_reparent_gaps.json \
    --new-parent <out>/<NewBaseName>_reflection.json \
    --old-parent <out>/<OldBaseName>_reflection.json \
    -o <out>/<BP>_mapping.json
  ```
  The mapping JSON splits each broken ref into `auto` / `user_required`
  / `reject`. With `--old-parent` supplied, name-matched candidates are
  also signature/type-compared -- mismatches drop from `auto` to
  `user_required` with the diff in `rationale`. For `user_required`
  rows, `candidates` lists name-similar members on the new parent.

- **Instruction emit (Layer 3 — dry-run, no .uasset modification):**
  ```bash
  bpmigrate apply-fix-mapping <out>/<BP>_mapping.json \
    -o <out>/<BP>_instructions.json
  ```
  Produces a `broken_refs_instructions_v1` JSON. Each entry has
  `kind: editorAction | deferredToUser | noFix` plus an `editorSteps`
  list the user can follow node-by-node. The instruction shape only
  reads the mapping's `status` field -- the same JSON is the input
  contract for any future surgery backend that auto-applies the fixes
  (none ships with OSS; bring-your-own). The contract + required
  safety (snapshot, lock, atomic, idempotent, audit trail) is
  documented in `docs/SURGERY_BACKEND.md`.

⚠ Skipping the reparent leaves the new C++ base's UPROPERTY / UFUNCTION
unwired on instances. Symptoms: variables missing in editor / PIE; in
worst case, divergent runtime behavior in packaged builds.

### 5-B. Reconnect in consumer BPs

```markdown
### In <consumer BP> editor:
- Delete then re-create any broken nodes with the same name.
- If a component reference broke: Components -> Add -> <C++ class>.
- Compile -> Save.
```

### 5-C. Verification checklist

```markdown
### Verification
- [ ] C++ build succeeds (no errors / warnings).
- [ ] BP compiles.
- [ ] Consumer BPs compile.
- [ ] Editor exercise of the migrated feature behaves as expected.
- [ ] Delegate bindings still fire (UI updates, etc.).
```

### 5-D. Source-control workflow

```markdown
### Source control
- Bundle the C++ changes and the BP changes into a single commit / CL.
- BP deletes are best in a separate commit (easier conflict resolution
  for collaborators).
- Reference both diffs in the review description.
```

### 5-E. `rewrite-callers` failure recovery

`bpmigrate rewrite-callers --save` saves each caller as it goes — it
is non-atomic. If caller #5 fails to save (P4 read-only, disk full,
etc.), callers #1–4 are already on disk. The commandlet's exit code
reflects load + save failure counts (non-zero = at least one caller
failed). Always run `verify-callers` afterwards to confirm the
on-disk state compiles.

When `verify-callers` reports `totalErrors > 0`:

1. **Pre-existing vs surgery-introduced.** Compare each error against
   `bpmigrate detect-gaps` `brokenReferences` from the *pre-surgery*
   `dump-graph`. If the error existed before surgery, it is the BP's
   own state, not a regression — surface separately.
2. **Surgery-introduced patterns** (most common):
   - **`pin '<X>' incompatible -- link to ... dropped`** in the rewriter
     log: a downstream input pin's expected type didn't match the new
     return; provide an `--pinmap` rename or accept the dropped link.
   - **`self is not a <Class>`** at compile time: the new function moved
     to an unrelated parent and the self pin's CDO is now of the wrong
     class. Re-run `rewrite-callers` after confirming the class IsChildOf
     gate covers it (file an issue with the BP path if it doesn't).
   - **Missing `--pinmap` entry**: callers that depended on a specific
     output pin name show "no such pin"; map old → new in `--pinmap`.
3. **Reverting `--save`**:
   - **Perforce**: `p4 revert <caller.uasset>` per caller, or
     `p4 revert -c default //...` for the whole pending CL.
   - **Git**: `git checkout -- <caller.uasset>` (binary-safe restore).
   - The commandlet does not write backups — VCS is the only undo path.
     If you ran `--save` outside a clean VCS state, recover from the
     editor's `.uasset.bak` / Saved/Autosave snapshot if available.

---

## Step 6: regression test

Notify: `"Step 6/7: running regression tests..."`

Compare the captured behavior trace from Step 2.5 against the migrated
class:

```bash
bpmigrate verify \
  --behavior "<tmp>/<BP>_behavior.json" \
  --class "/Script/<Module>.<ClassName>" \
  -o "<tmp>/<BP>_regression_report.json"
```

**Result interpretation:**
- `result: PASS` — every step matches; migration succeeded.
- `result: FAIL` — review the `diffs` array. Each diff has `path`,
  `expected`, `actual`.

On FAIL:
1. Show diffs to the user.
2. Patch the offending C++ logic.
3. Rebuild, re-run Step 6. Loop until PASS.

### Step 6-A. Reparent static verification (gate d)

Independent of `bpmigrate verify`'s behavior comparison: confirm every
reparent-target BP from Step 4.6 actually points at the new base.

**Important**: UAssetGUI emits the BPGeneratedClass `_C` export as a
RawExport, so its `SuperStruct` field often comes out as 0. Use the
import-table presence/absence as the signal instead (verified method).

```bash
for BP_PATH in <reparent_targets>; do
  BP_NAME=$(basename "$BP_PATH" .uasset)
  bpmigrate uasset-tojson "$BP_PATH" -o "<tmp>/${BP_NAME}_verify.json"

  EXPECTED_BASE="<BaseName>"           # from Step 4.6
  PRIOR_PARENT="<PriorParent>"         # pre-migration parent

  RESULT=$(python -c "
import json
with open(r'<tmp>/${BP_NAME}_verify.json', 'r', encoding='utf-8') as f:
    d = json.load(f)
imports = d.get('Imports', [])

has_new_base = any(
    str(imp.get('ObjectName','')) == r'$EXPECTED_BASE'
    and imp.get('ClassName','') in ('Class', 'BlueprintGeneratedClass')
    for imp in imports
)

# Prior parent persists -> reparent did not happen. But the prior parent
# may also be imported for unrelated reasons (widget tree, etc.), so
# limit to entries marked as Class.
has_prior_parent = any(
    str(imp.get('ObjectName','')) == r'$PRIOR_PARENT'
    and imp.get('ClassName','') == 'Class'
    for imp in imports
)

if has_new_base and not has_prior_parent:
    print('PASS')
elif has_new_base and has_prior_parent:
    print('AMBIGUOUS')
elif not has_new_base and has_prior_parent:
    print('FAIL_NO_REPARENT')
else:
    print('UNKNOWN')
")

  case "$RESULT" in
    PASS)             echo "[OK]   $BP_NAME: reparented" ;;
    FAIL_NO_REPARENT) echo "[FAIL] $BP_NAME: still on $PRIOR_PARENT, not reparented"; REPARENT_FAIL=1 ;;
    AMBIGUOUS)        echo "[WARN] $BP_NAME: new base present but prior parent also imported -- verify in editor" ;;
    *)                echo "[?]    $BP_NAME: cannot determine parent -- verify in editor" ;;
  esac
done

if [ -n "$REPARENT_FAIL" ]; then
  echo "Reparent verification failed; do not enter Step 7."
  exit 1
fi
```

Resolve every FAIL (re-run the Step 5-A-2 walkthrough) before Step 7.

---

## Step 7: completion report

Notify: `"Step 7/7: migration complete"`

```markdown
# <BP> -> C++ migration complete

## Result
- Regression tests: PASS (N/N steps)
- C++ files: <list>
- Modified BPs: <list>

## Source control
- <commit / CL identifiers>
```

---

## Usage

```
/migrate-bp MyBlueprint
/migrate-bp MyBlueprint --dry-run
/migrate-bp MyBlueprint --target-module MyGameCore
/migrate-bp MyBlueprint --accept-defaults
/migrate-bp /abs/path/to/MyBlueprint.uasset
```
