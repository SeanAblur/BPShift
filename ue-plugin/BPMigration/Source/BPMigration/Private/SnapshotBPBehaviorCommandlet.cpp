// SPDX-License-Identifier: MIT
// Copyright (c) 2026 xkdlaldfjtnl

#include "SnapshotBPBehaviorCommandlet.h"
#include "Tests/Utils/BPBehaviorSnapshot.h"
#include "Engine/Blueprint.h"
#include "Misc/DateTime.h"

int32 USnapshotBPBehaviorCommandlet::Main(const FString& Params)
{
	TArray<FString> Tokens;
	TArray<FString> Switches;
	TMap<FString, FString> ParamsMap;
	ParseCommandLine(*Params, Tokens, Switches, ParamsMap);

	if (Tokens.Num() == 0)
	{
		UE_LOG(LogTemp, Error, TEXT("Usage: -run=SnapshotBPBehavior /Game/Path/To/BP [-scenario=path.json] [-output=path.json]"));
		return 1;
	}

	const FString AssetPath = Tokens[0];
	const FString ScenarioPath = ParamsMap.FindRef(TEXT("scenario"));
	FString OutputPath = ParamsMap.FindRef(TEXT("output"));

	// Load Blueprint
	UBlueprint* Blueprint = LoadObject<UBlueprint>(nullptr, *AssetPath);
	if (!Blueprint || !Blueprint->GeneratedClass)
	{
		UE_LOG(LogTemp, Error, TEXT("[SnapshotBP] Failed to load Blueprint: %s"), *AssetPath);
		return 1;
	}

	UClass* BPClass = Blueprint->GeneratedClass;
	UE_LOG(LogTemp, Display, TEXT("[SnapshotBP] Loaded: %s (Parent: %s)"),
		*BPClass->GetName(), *BPClass->GetSuperClass()->GetName());

	// Load scenario or auto-generate
	TSharedPtr<FJsonObject> Scenario;
	if (!ScenarioPath.IsEmpty())
	{
		Scenario = FBPBehaviorSnapshot::LoadJsonFromFile(ScenarioPath);
		if (!Scenario.IsValid())
		{
			UE_LOG(LogTemp, Error, TEXT("[SnapshotBP] Failed to load scenario: %s"), *ScenarioPath);
			return 1;
		}
		UE_LOG(LogTemp, Display, TEXT("[SnapshotBP] Loaded scenario: %s"), *ScenarioPath);
	}
	else
	{
		Scenario = FBPBehaviorSnapshot::AutoGenerateScenario(BPClass);
		UE_LOG(LogTemp, Display, TEXT("[SnapshotBP] Auto-generated scenario with %d steps"),
			Scenario->GetArrayField(TEXT("steps")).Num());
	}

	// Default output path
	if (OutputPath.IsEmpty())
	{
		FString AssetName = FPaths::GetBaseFilename(AssetPath);
		OutputPath = FPaths::Combine(FPlatformProcess::UserTempDir(),
			TEXT("BPShift"), AssetName + TEXT("_behavior.json"));
	}

	// Create world
	UWorld* World = FBPBehaviorSnapshot::CreateTransientWorld();

	// Create instance
	UObject* Instance = FBPBehaviorSnapshot::CreateTestInstance(BPClass, World);
	if (!Instance)
	{
		UE_LOG(LogTemp, Error, TEXT("[SnapshotBP] Failed to create instance of %s"), *BPClass->GetName());
		return 1;
	}

	// Initial state snapshot
	TSharedPtr<FJsonObject> InitialState = FBPBehaviorSnapshot::SnapshotProperties(Instance);

	// Run scenario
	UE_LOG(LogTemp, Display, TEXT("[SnapshotBP] Running scenario..."));
	TArray<FBPBehaviorSnapshot::FStepResult> StepResults =
		FBPBehaviorSnapshot::RunScenario(Instance, Scenario, World);

	// Build result JSON
	TSharedPtr<FJsonObject> TraceJson = MakeShareable(new FJsonObject);
	TraceJson->SetStringField(TEXT("schema"), TEXT("behavior_trace_v1"));
	TraceJson->SetStringField(TEXT("sourceClass"), BPClass->GetPathName());
	TraceJson->SetStringField(TEXT("sourceClassParent"), BPClass->GetSuperClass()->GetPathName());
	TraceJson->SetStringField(TEXT("capturedAt"), FDateTime::Now().ToIso8601());
	TraceJson->SetObjectField(TEXT("scenario"), Scenario);
	TraceJson->SetObjectField(TEXT("initialState"), InitialState);

	TArray<TSharedPtr<FJsonValue>> StepsArray;
	for (int32 i = 0; i < StepResults.Num(); ++i)
	{
		const auto& Step = StepResults[i];
		TSharedPtr<FJsonObject> StepObj = MakeShareable(new FJsonObject);
		StepObj->SetNumberField(TEXT("index"), i);
		StepObj->SetStringField(TEXT("name"), Step.Name);
		StepObj->SetStringField(TEXT("function"), Step.FunctionName);
		if (Step.Outputs.IsValid())
		{
			StepObj->SetObjectField(TEXT("outputs"), Step.Outputs);
		}
		StepObj->SetObjectField(TEXT("stateAfter"), Step.StateAfter);
		StepsArray.Add(MakeShareable(new FJsonValueObject(StepObj)));
	}
	TraceJson->SetArrayField(TEXT("steps"), StepsArray);

	// Save
	if (FBPBehaviorSnapshot::SaveJsonToFile(TraceJson, OutputPath))
	{
		UE_LOG(LogTemp, Display, TEXT("[SnapshotBP] Written: %s (%d steps)"), *OutputPath, StepResults.Num());
	}
	else
	{
		UE_LOG(LogTemp, Error, TEXT("[SnapshotBP] Failed to write: %s"), *OutputPath);
		return 1;
	}

	return 0;
}
