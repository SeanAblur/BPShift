// SPDX-License-Identifier: MIT
// Copyright (c) 2026 xkdlaldfjtnl

#pragma once

#include "CoreMinimal.h"
#include "Commandlets/Commandlet.h"
#include "SnapshotBPBehaviorCommandlet.generated.h"

/**
 * Captures the runtime behavior of a Blueprint and writes it to JSON.
 * Run before migration to record the expected behavior trace that the
 * migrated C++ implementation must reproduce.
 *
 * Usage:
 *   UnrealEditor-Cmd.exe <YourProject>.uproject -run=SnapshotBPBehavior
 *       /Game/Path/To/Blueprint
 *       -scenario=<scenario>.json
 *       -output=<behavior>.json
 *
 * If -scenario is omitted, a default scenario is auto-generated from the
 * Blueprint's BlueprintCallable function list.
 */
UCLASS()
class BPMIGRATION_API USnapshotBPBehaviorCommandlet : public UCommandlet
{
	GENERATED_BODY()

public:
	virtual int32 Main(const FString& Params) override;
};
