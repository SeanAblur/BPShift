// SPDX-License-Identifier: MIT
// Copyright (c) 2026 xkdlaldfjtnl

#pragma once

#include "CoreMinimal.h"
#include "Commandlets/Commandlet.h"
#include "DumpClassReflectionCommandlet.generated.h"

/**
 * Dumps a UClass's reflection data (UFunctions + FProperties) to JSON.
 * Used by `bpmigrate map-broken-refs` to compare a Blueprint's broken
 * references against a candidate new parent class.
 *
 * Works for both BP-generated classes and C++ classes. The JSON shape is
 * intentionally aligned with what detect-gaps' `brokenReferences` carries
 * so the Layer 2 classifier can match by name + signature deterministically.
 *
 * Usage:
 *   UnrealEditor-Cmd.exe <YourProject>.uproject -run=DumpClassReflection
 *       /Script/MyModule.MyNewBase
 *       -output=<absolute path>.json
 *
 *   # BP class:
 *   ... -run=DumpClassReflection /Game/Path/BP_NewBase
 */
UCLASS()
class BPMIGRATION_API UDumpClassReflectionCommandlet : public UCommandlet
{
	GENERATED_BODY()

public:
	virtual int32 Main(const FString& Params) override;
};
