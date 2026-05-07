// SPDX-License-Identifier: MIT
// Copyright (c) 2026 xkdlaldfjtnl

#pragma once

#include "CoreMinimal.h"
#include "Commandlets/Commandlet.h"
#include "VerifyCallersCommandlet.generated.h"

/**
 * Force-unload + fresh-load + recompile every caller of a target Blueprint
 * and report PASS/FAIL based on the compile result log (FCompilerResultsLog
 * NumErrors). Solves the false-positive problem where `compile_blueprint`
 * returns success on a stale in-memory cache while the disk asset would fail
 * a fresh load.
 *
 * Use right after a graph-surgery pass (e.g. `RewriteCallers`) to confirm
 * every caller's saved .uasset really compiles.
 *
 * Usage:
 *   UnrealEditor-Cmd.exe <Project>.uproject -run=VerifyCallers
 *       -target=/Game/Path/To/TargetBP
 *       [-callers=/Game/Path/A,/Game/Path/B]   ; explicit list overrides auto-discovery
 *       [-output=<absolute path>.json]         ; per-caller error/warning detail
 *
 * Without -callers, callers are auto-discovered via the AssetRegistry's
 * package referencers of `target`. Maps and non-Blueprint assets are skipped.
 *
 * Exit code: 0 if every caller compiles clean, 1 if any caller has errors.
 */
UCLASS()
class BPMIGRATION_API UVerifyCallersCommandlet : public UCommandlet
{
	GENERATED_BODY()

public:
	virtual int32 Main(const FString& Params) override;
};
