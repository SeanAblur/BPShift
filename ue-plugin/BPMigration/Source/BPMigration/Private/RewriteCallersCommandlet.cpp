// SPDX-License-Identifier: MIT
// Copyright (c) 2026 xkdlaldfjtnl

#include "RewriteCallersCommandlet.h"

#include "CallFunctionRewriter.h"
#include "Engine/Blueprint.h"
#include "Kismet2/KismetEditorUtilities.h"
#include "Misc/CommandLine.h"
#include "Misc/Parse.h"
#include "UObject/Package.h"
#include "UObject/SavePackage.h"

namespace
{
	bool SplitClassDotFunction(const FString& In, FString& OutClassPath, FName& OutFn)
	{
		int32 DotIdx;
		if (!In.FindLastChar(TEXT('.'), DotIdx)) return false;
		OutClassPath = In.Left(DotIdx);
		OutFn = FName(*In.Mid(DotIdx + 1));
		return !OutClassPath.IsEmpty() && !OutFn.IsNone();
	}

	TMap<FName, FName> ParsePinMap(const FString& Spec)
	{
		// Form: "Old1=New1,Old2=New2".
		// New = empty / "None" / "NAME_None" -> NAME_None, which signals
		// "drop this pin entirely" to FBPCallFunctionRewriter (it skips
		// link copy + default copy for that old pin).
		TMap<FName, FName> Out;
		TArray<FString> Pairs;
		Spec.ParseIntoArray(Pairs, TEXT(","), true);
		for (const FString& P : Pairs)
		{
			FString OldName, NewName;
			if (P.Split(TEXT("="), &OldName, &NewName))
			{
				const FString TrimmedOld = OldName.TrimStartAndEnd();
				const FString TrimmedNew = NewName.TrimStartAndEnd();
				const FName NewFName =
					(TrimmedNew.IsEmpty()
					 || TrimmedNew.Equals(TEXT("None"), ESearchCase::IgnoreCase)
					 || TrimmedNew.Equals(TEXT("NAME_None"), ESearchCase::IgnoreCase))
					? NAME_None
					: FName(*TrimmedNew);
				Out.Add(FName(*TrimmedOld), NewFName);
			}
		}
		return Out;
	}
}

int32 URewriteCallersCommandlet::Main(const FString& Params)
{
	FString CallerList, OldSpec, NewSpec, PinMapSpec;
	const bool bSave = FParse::Param(*Params, TEXT("save"));
	if (!FParse::Value(*Params, TEXT("callers="), CallerList) ||
		!FParse::Value(*Params, TEXT("old="), OldSpec, false) ||
		!FParse::Value(*Params, TEXT("new="), NewSpec, false))
	{
		UE_LOG(LogTemp, Error, TEXT("RewriteCallers: missing required arg. Need -callers=, -old=, -new=."));
		return 1;
	}
	FParse::Value(*Params, TEXT("pinmap="), PinMapSpec, false);

	FString OldClassPath, NewClassPath;
	FName OldFn, NewFn;
	if (!SplitClassDotFunction(OldSpec, OldClassPath, OldFn) ||
		!SplitClassDotFunction(NewSpec, NewClassPath, NewFn))
	{
		UE_LOG(LogTemp, Error, TEXT("RewriteCallers: -old and -new must be of form <ClassPath>.<FuncName>"));
		return 1;
	}
	const TMap<FName, FName> PinMap = ParsePinMap(PinMapSpec);

	TArray<FString> CallerPaths;
	CallerList.ParseIntoArray(CallerPaths, TEXT(","), true);

	int32 TotalReplaced = 0;
	int32 ProcessedBPs = 0;
	int32 LoadFailures = 0;
	int32 SaveFailures = 0;
	for (const FString& CP : CallerPaths)
	{
		const FString Trimmed = CP.TrimStartAndEnd();
		UObject* Loaded = LoadObject<UObject>(nullptr, *Trimmed);
		UBlueprint* BP = Cast<UBlueprint>(Loaded);
		if (!BP)
		{
			// LoadObject returning nullptr / non-Blueprint is a *user error*
			// (typo in --callers, deleted asset, asset is a Map / DataAsset)
			// not a soft skip. Track it so the exit code reflects it.
			UE_LOG(LogTemp, Warning, TEXT("RewriteCallers: %s is not a Blueprint (loaded=%s) -- skipping"),
				*Trimmed, *GetNameSafe(Loaded));
			++LoadFailures;
			continue;
		}

		const int32 N = FBPCallFunctionRewriter::RewriteByPath(
			BP, OldClassPath, OldFn, NewClassPath, NewFn, PinMap);
		TotalReplaced += N;
		++ProcessedBPs;
		UE_LOG(LogTemp, Display, TEXT("RewriteCallers: %s -> %d call(s) replaced"), *Trimmed, N);

		if (bSave && N > 0)
		{
			FKismetEditorUtilities::CompileBlueprint(BP);
			UPackage* Pkg = BP->GetOutermost();
			if (Pkg)
			{
				FSavePackageArgs SaveArgs;
				SaveArgs.TopLevelFlags = RF_Standalone | RF_Public;
				// Do NOT pass SAVE_NoError -- silent save failure (e.g. P4
				// read-only file, disk full, permission denied) would leave
				// the editor with a dirty in-memory package while the user
				// thinks the rewrite was persisted.
				const FString FileName = FPackageName::LongPackageNameToFilename(Pkg->GetName(), FPackageName::GetAssetPackageExtension());
				const FSavePackageResultStruct Result = UPackage::Save(Pkg, BP, *FileName, SaveArgs);
				if (Result.Result != ESavePackageResult::Success)
				{
					UE_LOG(LogTemp, Error,
						TEXT("RewriteCallers: SavePackage FAILED for %s (result=%d) -- check write permissions / VCS lock state"),
						*Pkg->GetName(), static_cast<int32>(Result.Result));
					++SaveFailures;
				}
			}
		}
	}

	UE_LOG(LogTemp, Display,
		TEXT("RewriteCallers: done. %d BPs processed, %d total replacements, %d load failures, %d save failures."),
		ProcessedBPs, TotalReplaced, LoadFailures, SaveFailures);
	// Exit-code semantics: 0 only if every requested caller loaded AND
	// every save succeeded. Caller can `verify-callers` for compile state.
	return (LoadFailures + SaveFailures);
}
