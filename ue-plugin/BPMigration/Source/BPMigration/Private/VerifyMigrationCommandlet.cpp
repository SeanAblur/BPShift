// SPDX-License-Identifier: MIT
// Copyright (c) 2026 xkdlaldfjtnl

#include "VerifyMigrationCommandlet.h"
#include "Tests/Utils/BPBehaviorSnapshot.h"
#include "Misc/DateTime.h"

int32 UVerifyMigrationCommandlet::Main(const FString& Params)
{
	TArray<FString> Tokens;
	TArray<FString> Switches;
	TMap<FString, FString> ParamsMap;
	ParseCommandLine(*Params, Tokens, Switches, ParamsMap);

	const FString BehaviorPath = ParamsMap.FindRef(TEXT("behavior"));
	const FString ClassPath = ParamsMap.FindRef(TEXT("class"));
	FString OutputPath = ParamsMap.FindRef(TEXT("output"));

	if (BehaviorPath.IsEmpty() || ClassPath.IsEmpty())
	{
		UE_LOG(LogTemp, Error, TEXT("Usage: -run=VerifyMigration -behavior=<trace.json> -class=<ClassPath> [-output=<report.json>]"));
		return 1;
	}

	// Load behavior trace
	TSharedPtr<FJsonObject> Trace = FBPBehaviorSnapshot::LoadJsonFromFile(BehaviorPath);
	if (!Trace.IsValid())
	{
		UE_LOG(LogTemp, Error, TEXT("[VerifyMigration] Failed to load behavior trace: %s"), *BehaviorPath);
		return 1;
	}

	// Extract scenario
	const TSharedPtr<FJsonObject>* ScenarioPtr = nullptr;
	if (!Trace->TryGetObjectField(TEXT("scenario"), ScenarioPtr) || !ScenarioPtr)
	{
		UE_LOG(LogTemp, Error, TEXT("[VerifyMigration] Behavior trace has no scenario"));
		return 1;
	}
	TSharedPtr<FJsonObject> Scenario = *ScenarioPtr;

	// Load C++ class
	UClass* CppClass = LoadClass<UObject>(nullptr, *ClassPath);
	if (!CppClass)
	{
		UE_LOG(LogTemp, Error, TEXT("[VerifyMigration] Failed to load class: %s"), *ClassPath);
		return 1;
	}

	UE_LOG(LogTemp, Display, TEXT("[VerifyMigration] Testing: %s against %s"),
		*CppClass->GetName(), *BehaviorPath);

	// (D) Reflection signature parity check.
	// Compare the BP class's UFUNCTION and UPROPERTY surface to the
	// migrated C++ class. Mismatches mean the migration dropped or
	// renamed members that the BP exposed -- caught at the plan stage,
	// before any runtime divergence.
	TArray<TSharedPtr<FJsonValue>> ParityIssues;
	{
		const FString BPClassPath = Trace->GetStringField(TEXT("sourceClass"));
		UClass* BPClass = LoadClass<UObject>(nullptr, *BPClassPath);
		if (!BPClass)
		{
			UE_LOG(LogTemp, Warning, TEXT("[VerifyMigration] Could not load BP class %s for parity check"), *BPClassPath);
		}
		else
		{
			TSet<FName> BPFunctions;
			for (TFieldIterator<UFunction> It(BPClass, EFieldIteratorFlags::IncludeSuper); It; ++It)
				BPFunctions.Add(It->GetFName());
			TSet<FName> CppFunctions;
			for (TFieldIterator<UFunction> It(CppClass, EFieldIteratorFlags::IncludeSuper); It; ++It)
				CppFunctions.Add(It->GetFName());

			TSet<FName> BPProperties;
			for (TFieldIterator<FProperty> It(BPClass, EFieldIteratorFlags::IncludeSuper); It; ++It)
				BPProperties.Add(It->GetFName());
			TSet<FName> CppProperties;
			for (TFieldIterator<FProperty> It(CppClass, EFieldIteratorFlags::IncludeSuper); It; ++It)
				CppProperties.Add(It->GetFName());

			auto AddIssue = [&](const FString& Kind, const FString& Name)
			{
				TSharedPtr<FJsonObject> O = MakeShareable(new FJsonObject);
				O->SetStringField(TEXT("kind"), Kind);
				O->SetStringField(TEXT("name"), Name);
				ParityIssues.Add(MakeShareable(new FJsonValueObject(O)));
			};

			for (const FName& N : BPFunctions.Difference(CppFunctions))
				AddIssue(TEXT("missingFunction"), N.ToString());
			for (const FName& N : CppFunctions.Difference(BPFunctions))
				AddIssue(TEXT("extraFunction"), N.ToString());
			for (const FName& N : BPProperties.Difference(CppProperties))
				AddIssue(TEXT("missingProperty"), N.ToString());
			for (const FName& N : CppProperties.Difference(BPProperties))
				AddIssue(TEXT("extraProperty"), N.ToString());

			if (ParityIssues.Num() > 0)
			{
				UE_LOG(LogTemp, Warning, TEXT("[VerifyMigration] Reflection parity: %d differences (BP vs C++ class)"),
					ParityIssues.Num());
			}
			else
			{
				UE_LOG(LogTemp, Display, TEXT("[VerifyMigration] Reflection parity OK"));
			}
		}
	}

	// Default output path
	if (OutputPath.IsEmpty())
	{
		OutputPath = FPaths::Combine(FPlatformProcess::UserTempDir(),
			TEXT("BPShift"), TEXT("regression_report.json"));
	}

	// Create world and instance
	UWorld* World = FBPBehaviorSnapshot::CreateTransientWorld();
	UObject* Instance = FBPBehaviorSnapshot::CreateTestInstance(CppClass, World);
	if (!Instance)
	{
		UE_LOG(LogTemp, Error, TEXT("[VerifyMigration] Failed to create instance of %s"), *CppClass->GetName());
		return 1;
	}

	// Compare initial state
	TSharedPtr<FJsonObject> ExpectedInitial = Trace->GetObjectField(TEXT("initialState"));
	TSharedPtr<FJsonObject> ActualInitial = FBPBehaviorSnapshot::SnapshotProperties(Instance);
	TArray<FBPBehaviorSnapshot::FDiff> InitialDiffs =
		FBPBehaviorSnapshot::CompareSnapshots(ExpectedInitial, ActualInitial);

	// Run scenario
	TArray<FBPBehaviorSnapshot::FStepResult> ActualResults =
		FBPBehaviorSnapshot::RunScenario(Instance, Scenario, World);

	// Load expected steps
	const TArray<TSharedPtr<FJsonValue>>& ExpectedSteps = Trace->GetArrayField(TEXT("steps"));

	// Collect initial-state diff paths -- these are BP default-value differences and are excluded from step diffs
	TSet<FString> InitialDiffPaths;
	for (const auto& Diff : InitialDiffs)
	{
		InitialDiffPaths.Add(Diff.Path);
	}
	if (InitialDiffPaths.Num() > 0)
	{
		UE_LOG(LogTemp, Display, TEXT("[VerifyMigration] %d initial state diffs will be excluded from step comparison (BP default values)"),
			InitialDiffPaths.Num());
	}

	// Compare
	int32 TotalSteps = ExpectedSteps.Num();
	int32 PassedSteps = 0;
	int32 FailedSteps = 0;

	TArray<TSharedPtr<FJsonValue>> StepReports;

	for (int32 i = 0; i < TotalSteps; ++i)
	{
		const TSharedPtr<FJsonObject>& ExpStep = ExpectedSteps[i]->AsObject();
		FString StepName = ExpStep->GetStringField(TEXT("name"));

		TSharedPtr<FJsonObject> StepReport = MakeShareable(new FJsonObject);
		StepReport->SetNumberField(TEXT("index"), i);
		StepReport->SetStringField(TEXT("name"), StepName);

		if (i < ActualResults.Num())
		{
			// Compare state
			TSharedPtr<FJsonObject> ExpectedState = ExpStep->GetObjectField(TEXT("stateAfter"));
			TSharedPtr<FJsonObject> ActualState = ActualResults[i].StateAfter;

			TArray<FBPBehaviorSnapshot::FDiff> AllDiffs =
				FBPBehaviorSnapshot::CompareSnapshots(ExpectedState, ActualState);

			// Exclude properties that already differed in the initial state (BP default-value difference)
			TArray<FBPBehaviorSnapshot::FDiff> Diffs;
			for (const auto& Diff : AllDiffs)
			{
				// Check whether the path's root property is in the initial diff set
				FString RootPath = Diff.Path;
				int32 DotIdx;
				if (RootPath.FindChar('.', DotIdx)) RootPath = RootPath.Left(DotIdx);
				int32 BracketIdx;
				if (RootPath.FindChar('[', BracketIdx)) RootPath = RootPath.Left(BracketIdx);

				if (!InitialDiffPaths.Contains(Diff.Path) && !InitialDiffPaths.Contains(RootPath))
				{
					Diffs.Add(Diff);
				}
			}

			if (Diffs.Num() == 0)
			{
				StepReport->SetStringField(TEXT("result"), TEXT("PASS"));
				PassedSteps++;
			}
			else
			{
				StepReport->SetStringField(TEXT("result"), TEXT("FAIL"));
				FailedSteps++;

				TArray<TSharedPtr<FJsonValue>> DiffArray;
				for (const auto& Diff : Diffs)
				{
					TSharedPtr<FJsonObject> DiffObj = MakeShareable(new FJsonObject);
					DiffObj->SetStringField(TEXT("path"), Diff.Path);
					DiffObj->SetStringField(TEXT("expected"), Diff.Expected);
					DiffObj->SetStringField(TEXT("actual"), Diff.Actual);
					DiffArray.Add(MakeShareable(new FJsonValueObject(DiffObj)));

					UE_LOG(LogTemp, Warning, TEXT("[VerifyMigration] DIFF Step %d (%s): %s -- expected: %s, actual: %s"),
						i, *StepName, *Diff.Path, *Diff.Expected, *Diff.Actual);
				}
				StepReport->SetArrayField(TEXT("diffs"), DiffArray);
			}
		}
		else
		{
			StepReport->SetStringField(TEXT("result"), TEXT("MISSING"));
			FailedSteps++;
			UE_LOG(LogTemp, Error, TEXT("[VerifyMigration] Step %d (%s): no actual result (function may not exist)"),
				i, *StepName);
		}

		StepReports.Add(MakeShareable(new FJsonValueObject(StepReport)));
	}

	// Categorize initial diffs:
	//   - structural: one side has the property, other does not (e.g. BP
	//     component absent from C++). Informational only.
	//   - value mismatch: both sides have actual values, the values differ.
	//     Indicates a real migration bug (e.g. variable default not promoted
	//     from CDO override). Counts toward FAIL.
	auto IsStructuralMarker = [](const FString& V)
	{
		return V == TEXT("<present>") || V == TEXT("<missing>")
			|| V == TEXT("<null>") || V == TEXT("<root>");
	};

	int32 InitialValueMismatchCount = 0;
	TArray<TSharedPtr<FJsonValue>> InitDiffArray;
	for (const auto& Diff : InitialDiffs)
	{
		const bool bStructural = IsStructuralMarker(Diff.Expected) || IsStructuralMarker(Diff.Actual);
		TSharedPtr<FJsonObject> DiffObj = MakeShareable(new FJsonObject);
		DiffObj->SetStringField(TEXT("path"), Diff.Path);
		DiffObj->SetStringField(TEXT("expected"), Diff.Expected);
		DiffObj->SetStringField(TEXT("actual"), Diff.Actual);
		DiffObj->SetStringField(TEXT("kind"), bStructural ? TEXT("structural") : TEXT("valueMismatch"));
		InitDiffArray.Add(MakeShareable(new FJsonValueObject(DiffObj)));

		if (!bStructural)
		{
			InitialValueMismatchCount++;
			UE_LOG(LogTemp, Error, TEXT("[VerifyMigration] INITIAL VALUE MISMATCH: %s -- expected: %s, actual: %s"),
				*Diff.Path, *Diff.Expected, *Diff.Actual);
		}
		else
		{
			UE_LOG(LogTemp, Warning, TEXT("[VerifyMigration] INITIAL STRUCTURAL DIFF: %s -- expected: %s, actual: %s"),
				*Diff.Path, *Diff.Expected, *Diff.Actual);
		}
	}

	// PASS only when zero step failures AND zero initial value mismatches
	// AND zero reflection-parity differences.
	const bool bOverallPass = (FailedSteps == 0
		&& InitialValueMismatchCount == 0
		&& ParityIssues.Num() == 0);
	FString OverallResult = bOverallPass ? TEXT("PASS") : TEXT("FAIL");

	TSharedPtr<FJsonObject> Report = MakeShareable(new FJsonObject);
	Report->SetStringField(TEXT("schema"), TEXT("regression_report_v3"));
	Report->SetStringField(TEXT("sourceTrace"), BehaviorPath);
	Report->SetStringField(TEXT("testedClass"), ClassPath);
	Report->SetStringField(TEXT("testedAt"), FDateTime::Now().ToIso8601());
	Report->SetStringField(TEXT("result"), OverallResult);
	Report->SetNumberField(TEXT("totalSteps"), TotalSteps);
	Report->SetNumberField(TEXT("passedSteps"), PassedSteps);
	Report->SetNumberField(TEXT("failedSteps"), FailedSteps);
	Report->SetNumberField(TEXT("initialValueMismatches"), InitialValueMismatchCount);
	Report->SetNumberField(TEXT("reflectionParityIssues"), ParityIssues.Num());

	if (InitDiffArray.Num() > 0)
	{
		Report->SetArrayField(TEXT("initialStateDiffs"), InitDiffArray);
	}
	if (ParityIssues.Num() > 0)
	{
		Report->SetArrayField(TEXT("reflectionParity"), ParityIssues);
	}

	Report->SetArrayField(TEXT("steps"), StepReports);

	// Save
	if (FBPBehaviorSnapshot::SaveJsonToFile(Report, OutputPath))
	{
		UE_LOG(LogTemp, Display, TEXT("[VerifyMigration] Report: %s -- %s (%d/%d passed)"),
			*OutputPath, *OverallResult, PassedSteps, TotalSteps);
	}
	else
	{
		UE_LOG(LogTemp, Error, TEXT("[VerifyMigration] Failed to write report: %s"), *OutputPath);
	}

	return FailedSteps > 0 ? 1 : 0;
}
