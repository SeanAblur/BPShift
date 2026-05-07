// SPDX-License-Identifier: MIT
// Copyright (c) 2026 xkdlaldfjtnl

#pragma once

#include "CoreMinimal.h"
#include "Commandlets/Commandlet.h"
#include "DumpBPGraphCommandlet.generated.h"

/**
 * Dumps every node, pin, and connection of a Blueprint graph to JSON.
 * Produces information identical to what is visible in the editor when the
 * Blueprint is opened. (Editor builds only; returns a stub in packaged builds.)
 *
 * Usage:
 *   UnrealEditor-Cmd.exe <YourProject>.uproject -run=DumpBPGraph
 *       /Game/Path/To/Blueprint
 *       -output=<absolute path>.json
 */
UCLASS()
class BPMIGRATION_API UDumpBPGraphCommandlet : public UCommandlet
{
	GENERATED_BODY()

public:
	virtual int32 Main(const FString& Params) override;

private:
	TSharedPtr<FJsonObject> SerializeGraph(class UEdGraph* Graph);
	TSharedPtr<FJsonObject> SerializeNode(class UEdGraphNode* Node);
	TSharedPtr<FJsonObject> SerializePin(class UEdGraphPin* Pin);

	/** Self scope for K2Node FunctionReference / VariableReference resolution.
	 *  Set by Main() to the Blueprint's skeleton or generated class so that
	 *  SerializeNode() can detect broken references (a renamed parent member,
	 *  a deleted Cast target, an unimplemented BIE, etc.). */
	class UClass* SelfScope = nullptr;
};
