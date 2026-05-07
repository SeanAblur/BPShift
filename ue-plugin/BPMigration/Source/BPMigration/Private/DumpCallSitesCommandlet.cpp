// SPDX-License-Identifier: MIT
// Copyright (c) 2026 xkdlaldfjtnl

#include "DumpCallSitesCommandlet.h"

#include "BPGraphWalk.h"
#include "Engine/Blueprint.h"
#include "Engine/BlueprintGeneratedClass.h"
#include "EdGraph/EdGraph.h"
#include "EdGraph/EdGraphNode.h"
#include "K2Node_CallFunction.h"
#include "Misc/FileHelper.h"
#include "Misc/Parse.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "UObject/UObjectGlobals.h"
#include "UObject/Package.h"

namespace
{
	struct FCallSiteKey
	{
		FString Caller;
		FString TargetClass; // class path, e.g. "/Game/Path/Foo.Foo_C"
		FString Function;

		bool operator==(const FCallSiteKey& Other) const
		{
			return Caller == Other.Caller
				&& TargetClass == Other.TargetClass
				&& Function == Other.Function;
		}
	};

	uint32 GetTypeHash(const FCallSiteKey& K)
	{
		return HashCombine(HashCombine(GetTypeHash(K.Caller), GetTypeHash(K.TargetClass)),
		                   GetTypeHash(K.Function));
	}

	struct FCallSiteAgg
	{
		int32 Count = 0;
		TSet<FString> Graphs;
	};

	void WalkBlueprintCallSites(
		UBlueprint* BP,
		const FString& CallerPath,
		const FString& TargetFilterClassPath, // empty = no filter
		TMap<FCallSiteKey, FCallSiteAgg>& InOutAgg)
	{
		UClass* SelfScope = BP->GeneratedClass ? BP->GeneratedClass : BP->SkeletonGeneratedClass;

		// Walk uses the shared FBPGraphWalk helper so this commandlet, the
		// rewriter, and DumpBPGraph all see the exact same set of graphs --
		// any drift would surface as silent false-negatives in the closed-loop
		// verifier and silent under-rewrite at migrate time.
		FBPGraphWalk::ForEachExecGraph(BP, [&](UEdGraph* Graph, const FString& /*Label*/)
		{
			for (UEdGraphNode* Node : Graph->Nodes)
			{
				UK2Node_CallFunction* CF = Cast<UK2Node_CallFunction>(Node);
				if (!CF) continue;

				const FName FuncName = CF->FunctionReference.GetMemberName();
				if (FuncName.IsNone()) continue;

				UClass* Parent = CF->FunctionReference.GetMemberParentClass(SelfScope);
				if (!Parent) continue;

				const FString ParentClassPath = Parent->GetPathName();

				if (!TargetFilterClassPath.IsEmpty() && ParentClassPath != TargetFilterClassPath)
				{
					continue;
				}

				FCallSiteKey Key;
				Key.Caller = CallerPath;
				Key.TargetClass = ParentClassPath;
				Key.Function = FuncName.ToString();

				FCallSiteAgg& Agg = InOutAgg.FindOrAdd(Key);
				++Agg.Count;
				Agg.Graphs.Add(Graph->GetName());
			}
		});
	}

	// Resolve a Blueprint package path (`/Game/Path/Foo`) to its generated
	// class path (`/Game/Path/Foo.Foo_C`). Used so that `-target=` accepts
	// the friendly package path the user already types into the other CLI.
	FString ResolveTargetClassPath(const FString& TargetPackage)
	{
		if (TargetPackage.IsEmpty()) return FString();
		// If the user already gave us a `<package>.<class>` form, trust it.
		if (TargetPackage.Contains(TEXT(".")))
		{
			return TargetPackage;
		}
		UObject* Loaded = LoadObject<UObject>(nullptr, *TargetPackage);
		UBlueprint* BP = Cast<UBlueprint>(Loaded);
		if (BP && BP->GeneratedClass)
		{
			return BP->GeneratedClass->GetPathName();
		}
		// Fallback: append `.<leaf>_C` (UE convention).
		FString Leaf;
		TargetPackage.Split(TEXT("/"), nullptr, &Leaf, ESearchCase::IgnoreCase, ESearchDir::FromEnd);
		return TargetPackage + TEXT(".") + Leaf + TEXT("_C");
	}
}

int32 UDumpCallSitesCommandlet::Main(const FString& Params)
{
	FString CallerList, TargetSpec, OutputPath;
	if (!FParse::Value(*Params, TEXT("callers="), CallerList))
	{
		UE_LOG(LogTemp, Error, TEXT("DumpCallSites: -callers=A,B,... is required."));
		return 1;
	}
	FParse::Value(*Params, TEXT("target="), TargetSpec);
	FParse::Value(*Params, TEXT("output="), OutputPath);
	if (OutputPath.IsEmpty())
	{
		UE_LOG(LogTemp, Error, TEXT("DumpCallSites: -output=<path>.json is required."));
		return 1;
	}

	TArray<FString> CallerPaths;
	CallerList.ParseIntoArray(CallerPaths, TEXT(","), true);
	for (FString& P : CallerPaths) { P = P.TrimStartAndEnd(); }

	const FString TargetClassPath = ResolveTargetClassPath(TargetSpec);
	UE_LOG(LogTemp, Display, TEXT("DumpCallSites: target=%s (class=%s) callers=%d"),
		*TargetSpec, *TargetClassPath, CallerPaths.Num());

	TMap<FCallSiteKey, FCallSiteAgg> Agg;
	TArray<FString> Loaded;
	TArray<FString> Failed;
	for (const FString& CP : CallerPaths)
	{
		UObject* L = LoadObject<UObject>(nullptr, *CP);
		UBlueprint* BP = Cast<UBlueprint>(L);
		if (!BP)
		{
			UE_LOG(LogTemp, Warning, TEXT("DumpCallSites: %s is not a Blueprint -- skipping"), *CP);
			Failed.Add(CP);
			continue;
		}
		Loaded.Add(CP);
		WalkBlueprintCallSites(BP, CP, TargetClassPath, Agg);
	}

	// Stable sort: by caller, then function name. Deterministic JSON output
	// so two runs against the same inputs produce byte-identical files.
	TArray<FCallSiteKey> Keys;
	Agg.GenerateKeyArray(Keys);
	Keys.Sort([](const FCallSiteKey& A, const FCallSiteKey& B)
	{
		if (A.Caller != B.Caller) return A.Caller < B.Caller;
		if (A.TargetClass != B.TargetClass) return A.TargetClass < B.TargetClass;
		return A.Function < B.Function;
	});

	TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
	Root->SetStringField(TEXT("schema"), TEXT("callsites_v1"));
	Root->SetStringField(TEXT("target"), TargetSpec);
	Root->SetStringField(TEXT("target_class"), TargetClassPath);

	auto MakeStrArray = [](const TArray<FString>& In)
	{
		TArray<TSharedPtr<FJsonValue>> Out;
		for (const FString& S : In) { Out.Add(MakeShared<FJsonValueString>(S)); }
		return Out;
	};
	Root->SetArrayField(TEXT("callers_requested"), MakeStrArray(CallerPaths));
	Root->SetArrayField(TEXT("callers_loaded"),    MakeStrArray(Loaded));
	Root->SetArrayField(TEXT("callers_failed"),    MakeStrArray(Failed));

	TArray<TSharedPtr<FJsonValue>> Sites;
	for (const FCallSiteKey& K : Keys)
	{
		const FCallSiteAgg& A = Agg[K];
		TSharedRef<FJsonObject> Site = MakeShared<FJsonObject>();
		Site->SetStringField(TEXT("caller"), K.Caller);
		Site->SetStringField(TEXT("target_class"), K.TargetClass);
		Site->SetStringField(TEXT("function"), K.Function);
		Site->SetNumberField(TEXT("count"), A.Count);

		TArray<FString> SortedGraphs = A.Graphs.Array();
		SortedGraphs.Sort();
		Site->SetArrayField(TEXT("graphs"), MakeStrArray(SortedGraphs));

		Sites.Add(MakeShared<FJsonValueObject>(Site));
	}
	Root->SetArrayField(TEXT("callsites"), Sites);

	FString OutText;
	TSharedRef<TJsonWriter<>> W = TJsonWriterFactory<>::Create(&OutText);
	FJsonSerializer::Serialize(Root, W);
	if (!FFileHelper::SaveStringToFile(OutText, *OutputPath))
	{
		UE_LOG(LogTemp, Error, TEXT("DumpCallSites: failed to write %s"), *OutputPath);
		return 2;
	}

	UE_LOG(LogTemp, Display, TEXT("DumpCallSites: wrote %d call-site rows to %s (%d callers loaded, %d failed)"),
		Sites.Num(), *OutputPath, Loaded.Num(), Failed.Num());
	// Non-zero exit on any caller load failure: an "all-failed" run currently
	// reports 0 call-sites + exit 0, which a downstream `verify-plan-accuracy`
	// would read as "PASS over an empty set" -- silently masking the fact that
	// nothing was actually inspected.
	return Failed.Num() > 0 ? 3 : 0;
}
