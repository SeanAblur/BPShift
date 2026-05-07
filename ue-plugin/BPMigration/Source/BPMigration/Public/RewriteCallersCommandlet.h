// SPDX-License-Identifier: MIT
// Copyright (c) 2026 xkdlaldfjtnl

#pragma once

#include "CoreMinimal.h"
#include "Commandlets/Commandlet.h"
#include "RewriteCallersCommandlet.generated.h"

/**
 * Rewrites every K2Node_CallFunction in one or more caller Blueprints that
 * targets `<OldClass>.<OldFn>` so it instead targets `<NewClass>.<NewFn>`.
 *
 * Use after a BP→C++ migration where the BP-side function was renamed
 * (display-name with spaces → sanitized C++ identifier) or moved onto a new
 * parent class. Hidden state preserved per-call:
 *   - DefaultObject (self-pin CDO),
 *   - DefaultValue / DefaultText,
 *   - downstream links — narrow inputs receive an auto-inserted pure
 *     DynamicCast when the C++ return type is wider than the BP return type.
 *
 * Usage:
 *   UnrealEditor-Cmd.exe <Project>.uproject -run=RewriteCallers
 *       -callers=/Game/Path/To/CallerA,/Game/Path/To/CallerB
 *       -old=<OldClassPath>.<OldFnName>
 *       -new=<NewClassPath>.<NewFnName>
 *       [-pinmap=OldPin1=NewPin1,OldPin2=NewPin2]
 *       [-save]
 *
 * Example (display-name -> sanitized rename inside the same BP class):
 *   -run=RewriteCallers
 *     -callers=/Game/Path/CallerA,/Game/Path/CallerB
 *     -old="/Game/Path/MyBP.MyBP_C.Get Foo Bar"
 *     -new="/Game/Path/MyBP.MyBP_C.GetFooBar"
 *     -pinmap="AsFooBar=ReturnValue"
 *     -save
 */
UCLASS()
class BPMIGRATION_API URewriteCallersCommandlet : public UCommandlet
{
	GENERATED_BODY()

public:
	/**
	 * Flags (parsed from `Params` via `FParse::Value` / `FParse::Param`):
	 *   -callers=A,B,C   (required) comma-separated list of caller BP package paths.
	 *   -old=<spec>      (required) `<ClassPath>.<FuncName>` — last `.` separates.
	 *   -new=<spec>      (required) `<ClassPath>.<FuncName>` of the replacement.
	 *   -pinmap=<list>   (optional) `OldPinName=NewPinName` pairs, comma-separated.
	 *                    Drop a pin with any of: `OldPinName=` (empty),
	 *                    `OldPinName=None`, or `OldPinName=NAME_None`
	 *                    (case-insensitive, all map to `NAME_None`).
	 *   -save            (optional, bare flag) compile + save each modified caller.
	 *                    Without `-save` the in-memory edit is discarded on exit.
	 *
	 * @return 0 on success; non-zero on argument or load failure. Per-node
	 * surgery failures are warnings, not errors — call `VerifyCallers` after
	 * for the authoritative compile-pass signal.
	 */
	virtual int32 Main(const FString& Params) override;
};
