# Unreal Engine version compatibility

Tested: **UE 5.2** (verified end-to-end with the bundled commandlets).

Other UE versions are unverified. Below is the inventory of
version-sensitive points and what you need to update for each.

## What changes between UE versions

### `BPMigration.uplugin` — `EngineVersion`

```json
"EngineVersion": "5.2.0"
```

Bump to `5.3.0`, `5.4.0`, etc. when targeting that engine.

### `BPMigration.Build.cs`

```csharp
PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
```

UE 5.3+ sometimes wants
```csharp
IncludeOrderVersion = EngineIncludeOrderVersion.Unreal5_3;
```
in the `Target.cs` of the parent project (not the plugin). Add it when
the build complains about deprecated include order.

The set of public/private dependencies is stable from 5.2 through
5.4 to our knowledge. If a module is renamed or removed in a later
version, file an issue.

### UAssetGUI version string

The CLI defaults to passing `UE5_2` to `UAssetGUI tojson`. For other
engines:

```
export BPMIGRATION_UE_VERSION=UE5_3        # or UE5_4
```

UAssetGUI's accepted values are listed in its own docs:
<https://github.com/atenfyr/UAssetGUI>. Use the value that matches your
project's serialization.

### `K2Node_*` C++ APIs

Recipe Step 4 rule 7's mapping table covers nodes available across
5.2-5.4. New nodes introduced in later versions need explicit entries
- contributions welcome (see [CONTRIBUTING.md](../CONTRIBUTING.md)).

### `BPBehaviorSnapshot` reflection APIs

`FProperty` iteration / `ProcessEvent` / `FScriptArrayHelper` / etc.
have been stable since 5.0. No version-specific code paths in our
commandlet.

`BlueprintCreatedComponents` is filtered out of snapshots (BP-internal
accounting that has no native-C++ equivalent). This filter is engine-
version-agnostic.

## Migration check-list when bumping engine version

- [ ] Update `BPMigration.uplugin` `EngineVersion`
- [ ] Set `BPMIGRATION_UE_VERSION` env var (or config-file `ue_version`)
      to match
- [ ] Rebuild the plugin against the new engine
- [ ] Run `bpmigrate inspect <SomeBP>` on a known BP — confirm output
      shape matches what 5.2 produced
- [ ] Run `bpmigrate dump-graph` then `detect-gaps --graph` — confirm
      `componentsRequired`, `parentCDOOverrides`, `classFlags` populate
      correctly (these depend on UE-side reflection)
- [ ] Run a `snapshot` + `verify` round-trip with a trivial scenario
- [ ] Run `bpmigrate analyze-candidate` + `plan-rewrite-callers` against a
      BP that has at least one caller — these stage the AssetRegistry
      `GetReferencers` API and the `K2Node_CallFunction` member-name match
      paths, both of which sit on engine internals most likely to drift
      across UE versions
- [ ] Run a small `rewrite-callers` + `verify-callers` cycle on a synthetic
      rename — the rewriter touches `K2Node_DynamicCast::SetPurity`,
      `UEdGraphSchema::TryCreateConnection`, and `FCompilerResultsLog`
      internals; if the cast inject silently degrades or the
      compile-error count reports `0` after a known-broken caller, the
      engine surface has changed
- [ ] Run `tests/test_caller_plan_accuracy.py /Game/.../<KnownBP>` against
      a BP whose call-site count is already known. PASS = the shared
      `FBPGraphWalk::ForEachExecGraph` + `DumpCallSites` + plan path all
      resolve `(caller, function, count)` triples identically to UE 5.2.
      The same script's sanity-negative check perturbs the truth file in
      three ways; if any perturbation goes undetected the diff logic
      itself has regressed.
- [ ] If any output shape changed, document the delta here and bump
      schema versions in `bpmigrate.py` and the commandlets

## Known unknowns

- **UE 5.4 large worlds**: The snapshot's `FVector` is 64-bit double in
  5.0+; bytecode decompiler treats `EX_DoubleConst` and `EX_FloatConst`
  the same. No anticipated issues but unverified.
- **UE 5.5+ Editor module reorg**: Epic occasionally splits BlueprintGraph
  / Kismet — re-pin module names if linking fails.
- **Verse / Editor Utility Widgets**: Out of scope. The commandlets
  only handle `UBlueprint`-typed assets.

If you bump and encounter issues, please open an issue with the engine
version, OS, and the failing command's stderr.
