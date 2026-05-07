# Worked example: an Actor BP with components + CDO override

This example shows what the toolchain produces at each stage for a
typical Actor Blueprint. The numbers are taken from a real verified
end-to-end run (a 4-component visual gizmo Actor with one CDO override
and one custom variable). Use it as a reference when sanity-checking
your own runs.

## Source BP shape

- **Parent class**: `AActor`
- **Custom variable**: `ScreenSize` (BP "float" -- emits as C++
  `double` since UE 5.0; the BP "float" pin category resolves to
  `PinCategory=real, PinSubCategory=double`), default value (CDO
  override) = `2.5`
- **Custom variable**: BP-only display field (skipped for runtime
  parity)
- **CDO override**: `PrimaryActorTick.bCanEverTick = true`
- **Components** (4, all SCS-created):
  - `DefaultSceneRoot` (`USceneComponent`, root)
    - `SmallArrow_X` (`UStaticMeshComponent`, attached to root)
    - `SmallArrow_Y` (same)
    - `SmallArrow_Z` (same)
- **Functions / events**: empty (variables-only migration test case)

## Stage 1 — `bpmigrate inspect <BP>`

`summarize` output:

```
=== ExampleActorBP ===
Parent Class: Actor
Asset Type: ActorBlueprint

--- Variables (3) ---
  ScreenSize: Unknown = 2.5
  PrimaryActorTick: FActorTickFunction
  ActorLabel: FString = ExampleActorBP-1
```

The `Variables` section here uses what bytecode visibility provides;
NewVariable types are opaque (the VarType blob is base64).

## Stage 2 — `bpmigrate detect-gaps --graph <graph>.json`

```json
{
  "schema": "bpmigrate_gaps_v2",
  "missingFunctionExports": [],
  "cookRefCandidates": [],
  "referencesBodyGap": [],
  "orphanPins": [],
  "unconnectedDataInputs": [],
  "componentsRequired": [
    { "name": "DefaultSceneRoot", "componentClass": "SceneComponent",
      "parent": "<root>", "parentIsNative": false },
    { "name": "SmallArrow_Y",     "componentClass": "StaticMeshComponent",
      "parent": "DefaultSceneRoot", "parentIsNative": false },
    { "name": "SmallArrow_Z",     "componentClass": "StaticMeshComponent",
      "parent": "DefaultSceneRoot", "parentIsNative": false },
    { "name": "SmallArrow_X",     "componentClass": "StaticMeshComponent",
      "parent": "DefaultSceneRoot", "parentIsNative": false }
  ],
  "classFlags": [],
  "parentCDOOverrides": [
    { "Property": "PrimaryActorTick", "Type": "StructProperty",
      "OurValue":      "(bCanEverTick=True,bStartWithTickEnabled=True,bAllowTickOnDedicatedServer=True)",
      "ParentDefault": "(bStartWithTickEnabled=True,bAllowTickOnDedicatedServer=True)" },
    { "Property": "ActorLabel", "Type": "StrProperty",
      "OurValue":      "ExampleActorBP-1",
      "ParentDefault": "" }
  ]
}
```

Per recipe Step 1-E: each non-empty list above is a migration
obligation. `componentsRequired` -> rule 12, `parentCDOOverrides` ->
rule 13, `classFlags` -> rule 14 (empty here), and so on.

## Stage 3 — Migrated C++

Header (`AExampleActor.h`):

```cpp
#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "AExampleActor.generated.h"

class USceneComponent;
class UStaticMeshComponent;

UCLASS()  // rule 14: classFlags empty -> plain UCLASS()
class AExampleActor : public AActor
{
    GENERATED_BODY()

public:
    AExampleActor();

    // rule 10 priority 1: from CDO override
    // Note: BP "float" pins emit as C++ `double` since UE 5.0.
    UPROPERTY(EditAnywhere, BlueprintReadWrite)
    double ScreenSize = 2.5;

    // rule 12: components
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly)
    USceneComponent* DefaultSceneRoot;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly)
    UStaticMeshComponent* SmallArrow_X;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly)
    UStaticMeshComponent* SmallArrow_Y;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly)
    UStaticMeshComponent* SmallArrow_Z;
};
```

Implementation (`AExampleActor.cpp`):

```cpp
#include "AExampleActor.h"
#include "Components/SceneComponent.h"
#include "Components/StaticMeshComponent.h"

AExampleActor::AExampleActor()
{
    // rule 13: parent CDO override
    PrimaryActorTick.bCanEverTick = true;

    // rule 12: components in topological order (parent before children)
    DefaultSceneRoot = CreateDefaultSubobject<USceneComponent>(TEXT("DefaultSceneRoot"));
    RootComponent = DefaultSceneRoot;

    SmallArrow_Y = CreateDefaultSubobject<UStaticMeshComponent>(TEXT("SmallArrow_Y"));
    SmallArrow_Y->SetupAttachment(DefaultSceneRoot);

    SmallArrow_Z = CreateDefaultSubobject<UStaticMeshComponent>(TEXT("SmallArrow_Z"));
    SmallArrow_Z->SetupAttachment(DefaultSceneRoot);

    SmallArrow_X = CreateDefaultSubobject<UStaticMeshComponent>(TEXT("SmallArrow_X"));
    SmallArrow_X->SetupAttachment(DefaultSceneRoot);

    // ActorLabel CDO override skipped per rule 13 sub-rule (editor-only).
}
```

## Stage 4 — Verify

```
bpmigrate snapshot /Game/.../ExampleActorBP --scenario empty.json -o trace.json
bpmigrate verify   --behavior trace.json --class /Script/<Module>.ExampleActor -o report.json
```

Expected report:

```json
{
  "schema": "regression_report_v3",
  "result": "PASS",
  "totalSteps": 0, "passedSteps": 0, "failedSteps": 0,
  "initialValueMismatches": 0,
  "initialStateDiffs": [],
  "reflectionParityIssues": []
}
```

## What this fixture demonstrates

- Step 1-E surfaces **everything** the recipe needs you to handle.
- Rule 12 (components) + rule 13 (CDO override) + rule 10 priority 1
  (variable default from CDO) together produce a runtime-equivalent
  C++ class.
- `BlueprintCreatedComponents` does not appear in initialStateDiffs
  because `BPBehaviorSnapshot` filters it out (it has no native-C++
  equivalent, and the components themselves are compared via their
  named UPROPERTY references).

## What this fixture does NOT cover

This is a variables + components example. For function migration,
delegate broadcast, dispatcher binding, or graph-only function
translation, see the recipe sections cited per rule. Specific failure
modes (intentional bug injection to verify divergence detection) are
documented in `LIMITATIONS.md` under the "Verified scope" section.

For the **caller graph surgery** cycle that runs after a BP→C++ rename
(`bpmigrate analyze-candidate` → `plan-rewrite-callers` → emit C++ →
`rewrite-callers` → `verify-callers`), see `skill/migrate-bp.md` Step 0.5
(automation entry point), Step 5-A-2-bis (inheritance-only fast path)
and Step 5-A-3 (clean-slate replacement when SCS overlaps with native
UPROPERTY components).

## Reproducing on your own BP

If you have an Actor BP with similar shape (`<out>` is whatever
working directory you prefer; the CLI defaults to
`<system temp>/BPShift` if you omit `-o`):

```bash
# 1. Inspect
bpmigrate inspect <YourBP>

# 2. Get the gap report
bpmigrate dump-graph /Game/.../YourBP -o <out>/your_bp_graph.json
bpmigrate detect-gaps <out>/your_bp.json \
  --graph <out>/your_bp_graph.json \
  --summary-text <out>/your_bp.txt

# 3. Apply rules (manual or via /migrate-bp slash command)

# 4. Verify
echo '{"schema":"scenario_v1","steps":[]}' > <out>/empty.json
bpmigrate snapshot /Game/.../YourBP \
  --scenario <out>/empty.json \
  -o <out>/your_bp_trace.json
bpmigrate verify \
  --behavior <out>/your_bp_trace.json \
  --class /Script/<Module>.<YourClass> \
  -o <out>/report.json

cat <out>/report.json | \
  python -c "import json,sys; r=json.load(sys.stdin); print(r['result'], 'mismatches:', r['initialValueMismatches'])"
```

If you don't see `PASS, mismatches: 0`, check the `initialStateDiffs`
array — each `valueMismatch` is a real migration bug; each
`structural` is a "your minimal C++ is missing X field" hint.
