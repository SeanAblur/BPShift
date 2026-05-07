// SPDX-License-Identifier: MIT
// Copyright (c) 2026 xkdlaldfjtnl

#pragma once

#include "CoreMinimal.h"
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"

class UObject;
class UWorld;
class UFunction;

/**
 * Regression-test engine for Blueprint -> C++ migration.
 *
 * Two roles:
 * 1. SnapshotBPBehavior: instantiates the Blueprint, runs a scenario, and
 *    captures state snapshots after each step into JSON.
 * 2. VerifyMigration: replays the same scenario against the migrated C++
 *    class and diffs the snapshots, producing a regression report.
 */
class BPMIGRATION_API FBPBehaviorSnapshot
{
public:
	// -- Instance creation --

	/** Creates a test instance appropriate for the class kind (Component / Actor / Widget / FunctionLibrary). */
	static UObject* CreateTestInstance(UClass* Class, UWorld* World);

	/** Creates a transient world for tests. */
	static UWorld* CreateTransientWorld();

	// -- Property snapshot --

	/** Serializes every UPROPERTY of the object to JSON. */
	static TSharedPtr<FJsonObject> SnapshotProperties(UObject* Object);

	// -- Function invocation --

	/** Invokes a function via ProcessEvent and returns the output parameters as JSON. */
	static TSharedPtr<FJsonObject> CallFunction(
		UObject* Object,
		const FString& FunctionName,
		const TSharedPtr<FJsonObject>& Params,
		UWorld* WorldContext = nullptr
	);

	// -- Scenario execution --

	struct FStepResult
	{
		FString Name;
		FString FunctionName;
		TSharedPtr<FJsonObject> Outputs;
		TSharedPtr<FJsonObject> StateAfter;
	};

	/** Runs the entire scenario and collects a state snapshot after each step. */
	static TArray<FStepResult> RunScenario(
		UObject* Instance,
		const TSharedPtr<FJsonObject>& Scenario,
		UWorld* WorldContext = nullptr
	);

	/** Auto-generates a default scenario from the BlueprintCallable functions on the class. */
	static TSharedPtr<FJsonObject> AutoGenerateScenario(UClass* Class);

	// -- Comparison --

	struct FDiff
	{
		FString Path;
		FString Expected;
		FString Actual;
	};

	/** Returns the list of differences between two snapshots. */
	static TArray<FDiff> CompareSnapshots(
		const TSharedPtr<FJsonObject>& Expected,
		const TSharedPtr<FJsonObject>& Actual,
		const FString& PathPrefix = TEXT("")
	);

	// -- JSON I/O --

	static bool SaveJsonToFile(const TSharedPtr<FJsonObject>& Json, const FString& FilePath);
	static TSharedPtr<FJsonObject> LoadJsonFromFile(const FString& FilePath);

private:
	// -- Property (de)serialization helpers --

	static TSharedPtr<FJsonValue> SerializeProperty(FProperty* Property, const void* ValuePtr, int32 Depth = 0);
	static void DeserializeProperty(FProperty* Property, void* ValuePtr, const TSharedPtr<FJsonValue>& JsonValue);

	/** Deterministic placeholder management for FGuid values. */
	static TMap<FGuid, int32> GuidPlaceholderMap;
	static int32 GuidPlaceholderCounter;

	static FString GuidToPlaceholder(const FGuid& Guid);
	static void ResetGuidPlaceholders();
};
