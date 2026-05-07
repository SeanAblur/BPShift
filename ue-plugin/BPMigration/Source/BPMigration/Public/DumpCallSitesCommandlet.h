// SPDX-License-Identifier: MIT
// Copyright (c) 2026 xkdlaldfjtnl

#pragma once

#include "CoreMinimal.h"
#include "Commandlets/Commandlet.h"
#include "DumpCallSitesCommandlet.generated.h"

/**
 * Ground-truth call-site enumerator.
 *
 * Walks every graph (Ubergraph + Function + Macro) of every supplied caller
 * Blueprint, collects every `K2Node_CallFunction` node, and reports the exact
 * `(caller, target_class_path, function_name, count, locations)` set as JSON.
 *
 * This is the *single source of truth* used to validate the heuristic
 * `bpmigrate plan-rewrite-callers` output:
 *   - if `plan-rewrite-callers` reports a (caller, function) pair that
 *     `DumpCallSites` does not, that is a *false positive*;
 *   - if `DumpCallSites` reports a pair that `plan-rewrite-callers` does
 *     not, that is a *false negative*.
 *
 * Because this commandlet uses UE's own graph walk (not bytecode parsing
 * or NameMap heuristics), its output is by construction what the editor
 * itself sees — there is no parser disagreement to resolve.
 *
 * Usage:
 *   UnrealEditor-Cmd.exe <Project>.uproject -run=DumpCallSites
 *       -callers=/Game/Path/CallerA,/Game/Path/CallerB
 *       [-target=/Game/Path/TargetBP]    (optional filter; matches any
 *                                         function whose MemberParentClass
 *                                         resolves to TargetBP_C)
 *       -output=<absolute path>.json
 *
 * Output JSON (sorted by caller, then function for determinism):
 *   {
 *     "schema": "callsites_v1",
 *     "target": "/Game/Path/TargetBP",
 *     "callers_requested": ["/Game/Path/CallerA", ...],
 *     "callers_loaded": ["/Game/Path/CallerA", ...],
 *     "callers_failed": [],
 *     "callsites": [
 *       { "caller": "/Game/Path/CallerA",
 *         "target_class": "/Game/Path/TargetBP.TargetBP_C",
 *         "function": "Save Project",
 *         "count": 3,
 *         "graphs": ["EventGraph", "DoStuff"]
 *       },
 *       ...
 *     ]
 *   }
 */
UCLASS()
class BPMIGRATION_API UDumpCallSitesCommandlet : public UCommandlet
{
	GENERATED_BODY()

public:
	virtual int32 Main(const FString& Params) override;
};
