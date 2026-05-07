// SPDX-License-Identifier: MIT
// Copyright (c) 2026 xkdlaldfjtnl

#pragma once

#include "CoreMinimal.h"
#include "Templates/Function.h"

class UBlueprint;
class UEdGraph;

/**
 * Single source of truth for "walk every executable graph in a Blueprint".
 *
 * Three plugin features (DumpBPGraph, DumpCallSites, FBPCallFunctionRewriter)
 * historically duplicated this walk. Each duplicate had to be kept in sync
 * by review discipline and a "must stay congruent" comment; in practice the
 * three drifted twice (delegate-signature graphs missed, then SubGraphs
 * recursion missed -- both surfaced as silent under-rewrite + silent PASS
 * in the closed-loop verifier). Centralising the walk here removes the
 * sync surface entirely.
 *
 * Walk order (stable, deterministic):
 *   - UbergraphPages          (label "EventGraph")
 *   - FunctionGraphs          (label "Function")
 *   - MacroGraphs             (label "Macro")
 *   - DelegateSignatureGraphs (label "DelegateSignature")
 *   - ImplementedInterfaces[].Graphs (label "Interface")
 * Each graph above is followed by its `SubGraphs` recursively (Composite /
 * Collapsed-Graph node bodies). A `TSet<UEdGraph*>` cycle-guard tolerates
 * any malformed asset that would otherwise loop.
 *
 * Sub-graphs inherit their parent's label (e.g. a Composite collapsed inside
 * a FunctionGraph emits with label "Function") so callers that key off the
 * label do not need a separate "SubGraph" branch.
 */
class BPMIGRATION_API FBPGraphWalk
{
public:
	using FVisitor = TFunctionRef<void(UEdGraph* Graph, const FString& GraphTypeLabel)>;

	/** Visit every reachable exec graph in `BP` exactly once. No-op on null. */
	static void ForEachExecGraph(UBlueprint* BP, FVisitor Visitor);
};
