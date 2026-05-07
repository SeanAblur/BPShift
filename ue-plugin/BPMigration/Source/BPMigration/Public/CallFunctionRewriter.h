// SPDX-License-Identifier: MIT
// Copyright (c) 2026 xkdlaldfjtnl

#pragma once

#include "CoreMinimal.h"
#include "UObject/Object.h"
#include "UObject/NameTypes.h"

class UBlueprint;
class UClass;

/**
 * Rewrites every K2Node_CallFunction in a Blueprint that targets
 * `OldFuncOwner.OldFuncName` so it instead targets `NewFuncOwner.NewFuncName`.
 * Handles BP→C++ migrations where:
 *   - the BP function had a display-name with spaces (e.g. "Get Foo Bar")
 *     that the C++ port sanitized (e.g. "GetFooBar");
 *   - the BP function returned a narrow BP class (e.g. BP_Sub_C) and the
 *     C++ port returns a wider native class (e.g. the native base) — a pure
 *     DynamicCast is inserted to preserve every downstream link;
 *   - the original self pin defaulted to a CDO (DefaultObject), which would be
 *     silently lost on a naive replace and surface as
 *     "self is not a <Class>, therefore 'Target' must have a connection"
 *     during a fresh-load reload-time recompile.
 *
 * All graph kinds are walked: UbergraphPages + FunctionGraphs + MacroGraphs.
 *
 * Returns the number of K2Node_CallFunction nodes replaced.
 *
 * Validated end-to-end against a real production cycle (multi-caller real BP,
 * dozens of K2Node_CallFunction sites, 0 LogBlueprint Errors after force-unload
 * + fresh-load + recompile).
 */
class BPMIGRATION_API FBPCallFunctionRewriter
{
public:
	/**
	 * UClass* form.
	 *
	 * Invariants preserved per matched node:
	 *   - every existing link either survives, OR is bridged by an
	 *     auto-inserted pure DynamicCast (when the new return type is wider
	 *     than the linked downstream input type), OR is dropped with a
	 *     `LogTemp Warning: BPCallFunctionRewriter: pin '<n>' incompatible
	 *     -- link to ... dropped` log line so the user can see exactly which
	 *     edges did not survive;
	 *   - on cast-inject wiring failure the half-link is broken and the
	 *     orphan cast node is removed (no half-wired DynamicCast left in
	 *     the graph);
	 *   - DefaultValue + DefaultObject (CDO) + DefaultTextValue are copied
	 *     when the new pin's PinCategory matches the old pin's PinCategory.
	 *
	 * @param ExplicitPinMap  Optional rename mapping `OldPinName -> NewPinName`
	 *                        applied before the same-name lookup. Pins not
	 *                        present in the map fall through to same-name
	 *                        matching against the new function's pin set.
	 *                        A `NAME_None` mapped value means *drop this
	 *                        pin entirely* — neither links nor defaults are
	 *                        copied for it (use this for pins the new
	 *                        function does not have).
	 *
	 * @return number of K2Node_CallFunction nodes replaced.
	 */
	static int32 Rewrite(
		UBlueprint* Blueprint,
		UClass* OldFuncOwner, FName OldFuncName,
		UClass* NewFuncOwner, FName NewFuncName,
		const TMap<FName, FName>& ExplicitPinMap);

	/** ClassPath-string form (Python-friendly). Invariants identical to `Rewrite`. */
	static int32 RewriteByPath(
		UBlueprint* Blueprint,
		const FString& OldFuncOwnerPath, FName OldFuncName,
		const FString& NewFuncOwnerPath, FName NewFuncName,
		const TMap<FName, FName>& ExplicitPinMap);
};
