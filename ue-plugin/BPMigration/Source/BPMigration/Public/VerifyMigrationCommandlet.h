// SPDX-License-Identifier: MIT
// Copyright (c) 2026 xkdlaldfjtnl

#pragma once

#include "CoreMinimal.h"
#include "Commandlets/Commandlet.h"
#include "VerifyMigrationCommandlet.generated.h"

/**
 * Compares the runtime behavior of a migrated C++ class against the expected
 * Blueprint behavior trace. Use the output of SnapshotBPBehavior as -behavior.
 *
 * Usage:
 *   UnrealEditor-Cmd.exe <YourProject>.uproject -run=VerifyMigration
 *       -behavior=<behavior>.json
 *       -class=/Script/<Module>.<ClassName>
 *       -output=<report>.json
 *
 * Exit code: 0 = PASS (every step matches), 1 = FAIL (diffs detected).
 */
UCLASS()
class BPMIGRATION_API UVerifyMigrationCommandlet : public UCommandlet
{
	GENERATED_BODY()

public:
	virtual int32 Main(const FString& Params) override;
};
