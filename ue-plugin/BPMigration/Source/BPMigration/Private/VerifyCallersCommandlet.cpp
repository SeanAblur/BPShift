// SPDX-License-Identifier: MIT
// Copyright (c) 2026 xkdlaldfjtnl

#include "VerifyCallersCommandlet.h"

#include "AssetRegistry/AssetRegistryModule.h"
#include "AssetRegistry/IAssetRegistry.h"
#include "Engine/Blueprint.h"
#include "FileHelpers.h"
#include "PackageTools.h"
#include "Kismet2/CompilerResultsLog.h"
#include "Logging/TokenizedMessage.h"
#include "Kismet2/KismetEditorUtilities.h"
#include "Misc/CommandLine.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Misc/Parse.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "UObject/Package.h"

namespace
{
	/**
	 * Walk AssetRegistry referencers and return every package that references
	 * `TargetPackage`, except those under `/Game/Maps/`. The downstream loop
	 * filters via `Cast<UBlueprint>` after load — this stage is best-effort
	 * (a non-BP, non-Map referencer like a DataAsset would survive here and
	 * be filtered later); the map-skip is a coarse cheap pre-filter.
	 */
	TArray<FString> AutoDiscoverCallers(const FString& TargetPackage)
	{
		TArray<FString> Out;
		FAssetRegistryModule& Reg = FModuleManager::LoadModuleChecked<FAssetRegistryModule>("AssetRegistry");
		IAssetRegistry& AR = Reg.Get();
		// Make sure the registry is fully populated for this commandlet run.
		AR.SearchAllAssets(/*bSynchronousSearch=*/true);

		TArray<FName> Referencers;
		AR.GetReferencers(FName(*TargetPackage), Referencers);
		for (FName R : Referencers)
		{
			const FString S = R.ToString();
			if (S.StartsWith(TEXT("/Game/Maps/"))) continue;
			Out.Add(S);
		}
		Out.Sort();
		return Out;
	}

	struct FCallerResult
	{
		FString Path;
		int32 NumErrors = 0;
		int32 NumWarnings = 0;
		TArray<FString> ErrorMessages;
		FString Skipped;
	};
}

int32 UVerifyCallersCommandlet::Main(const FString& Params)
{
	FString TargetPath, ExplicitList, OutputPath;
	if (!FParse::Value(*Params, TEXT("target="), TargetPath) && !FParse::Value(*Params, TEXT("callers="), ExplicitList))
	{
		UE_LOG(LogTemp, Error, TEXT("VerifyCallers: need -target=<BP> or -callers=A,B,..."));
		return 2;
	}
	FParse::Value(*Params, TEXT("callers="), ExplicitList);
	FParse::Value(*Params, TEXT("output="), OutputPath);

	TArray<FString> CallerPaths;
	if (!ExplicitList.IsEmpty())
	{
		ExplicitList.ParseIntoArray(CallerPaths, TEXT(","), true);
	}
	else
	{
		// Validate the target package exists BEFORE asking AssetRegistry
		// for referencers. Otherwise a typo in -target= silently returns
		// "0 referencers + 0 errors", which the caller would read as PASS.
		FAssetRegistryModule& Reg = FModuleManager::LoadModuleChecked<FAssetRegistryModule>("AssetRegistry");
		IAssetRegistry& AR = Reg.Get();
		AR.SearchAllAssets(/*bSynchronousSearch=*/true);
		TArray<FAssetData> TargetAssets;
		AR.GetAssetsByPackageName(FName(*TargetPath), TargetAssets);
		if (TargetAssets.Num() == 0)
		{
			UE_LOG(LogTemp, Error,
				TEXT("VerifyCallers: -target=%s does not resolve to any asset (typo? deleted? wrong /Game/ prefix?)"),
				*TargetPath);
			return 2;
		}
		CallerPaths = AutoDiscoverCallers(TargetPath);
		UE_LOG(LogTemp, Display, TEXT("VerifyCallers: auto-discovered %d caller(s) of %s"),
			CallerPaths.Num(), *TargetPath);
	}

	TArray<FCallerResult> Results;
	int32 TotalErrors = 0;

	for (const FString& CPRaw : CallerPaths)
	{
		FCallerResult R;
		R.Path = CPRaw.TrimStartAndEnd();

		// Force-unload before fresh load so we read the current .uasset
		// and not whatever in-memory cache the editor session may hold.
		const FString PackageName = R.Path;
		if (UPackage* Existing = FindPackage(nullptr, *PackageName))
		{
			TArray<UPackage*> ToUnload = { Existing };
			FText OutError;
			UPackageTools::UnloadPackages(ToUnload, OutError);
		}

		UObject* Loaded = LoadObject<UObject>(nullptr, *R.Path);
		UBlueprint* BP = Cast<UBlueprint>(Loaded);
		if (!BP)
		{
			R.Skipped = FString::Printf(TEXT("not a Blueprint (loaded=%s)"), *GetNameSafe(Loaded));
			Results.Add(R);
			continue;
		}

		FCompilerResultsLog Log;
		Log.SetSourcePath(R.Path);
		FKismetEditorUtilities::CompileBlueprint(
			BP,
			EBlueprintCompileOptions::SkipGarbageCollection,
			&Log);
		R.NumErrors   = Log.NumErrors;
		R.NumWarnings = Log.NumWarnings;
		for (TSharedRef<class FTokenizedMessage> Msg : Log.Messages)
		{
			if (Msg->GetSeverity() == EMessageSeverity::Error)
			{
				R.ErrorMessages.Add(Msg->ToText().ToString());
			}
		}
		TotalErrors += R.NumErrors;

		const TCHAR* Tag = (R.NumErrors == 0) ? TEXT("PASS") : TEXT("FAIL");
		UE_LOG(LogTemp, Display, TEXT("VerifyCallers: %s %s (errors=%d, warnings=%d)"),
			Tag, *R.Path, R.NumErrors, R.NumWarnings);
		for (const FString& E : R.ErrorMessages)
		{
			UE_LOG(LogTemp, Warning, TEXT("    %s"), *E);
		}

		Results.Add(R);
	}

	// JSON report
	if (!OutputPath.IsEmpty())
	{
		const TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
		Root->SetStringField(TEXT("target"), TargetPath);
		Root->SetNumberField(TEXT("callerCount"), Results.Num());
		Root->SetNumberField(TEXT("totalErrors"), TotalErrors);
		TArray<TSharedPtr<FJsonValue>> Arr;
		for (const FCallerResult& R : Results)
		{
			TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
			O->SetStringField(TEXT("caller"), R.Path);
			O->SetNumberField(TEXT("errors"), R.NumErrors);
			O->SetNumberField(TEXT("warnings"), R.NumWarnings);
			if (!R.Skipped.IsEmpty()) O->SetStringField(TEXT("skipped"), R.Skipped);
			TArray<TSharedPtr<FJsonValue>> Msgs;
			for (const FString& M : R.ErrorMessages)
			{
				Msgs.Add(MakeShared<FJsonValueString>(M));
			}
			O->SetArrayField(TEXT("errorMessages"), Msgs);
			Arr.Add(MakeShared<FJsonValueObject>(O));
		}
		Root->SetArrayField(TEXT("callers"), Arr);

		FString Json;
		const TSharedRef<TJsonWriter<>> W = TJsonWriterFactory<>::Create(&Json);
		FJsonSerializer::Serialize(Root, W);
		FFileHelper::SaveStringToFile(Json, *OutputPath);
		UE_LOG(LogTemp, Display, TEXT("VerifyCallers: report -> %s"), *OutputPath);
	}

	UE_LOG(LogTemp, Display, TEXT("VerifyCallers: done. %d caller(s), %d total error(s)."),
		Results.Num(), TotalErrors);
	return (TotalErrors == 0) ? 0 : 1;
}
