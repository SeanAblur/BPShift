// SPDX-License-Identifier: MIT
// Copyright (c) 2026 xkdlaldfjtnl

#include "CallFunctionRewriter.h"

#include "BPGraphWalk.h"
#include "Engine/Blueprint.h"
#include "EdGraph/EdGraph.h"
#include "EdGraph/EdGraphSchema.h"
#include "EdGraphSchema_K2.h"
#include "K2Node_CallFunction.h"
#include "K2Node_DynamicCast.h"
#include "Kismet2/BlueprintEditorUtils.h"
#include "UObject/Class.h"
#include "UObject/Package.h"

int32 FBPCallFunctionRewriter::Rewrite(
	UBlueprint* BP,
	UClass* OldFuncOwner, FName OldFuncName,
	UClass* NewFuncOwner, FName NewFuncName,
	const TMap<FName, FName>& ExplicitPinMap)
{
	if (!BP || !OldFuncOwner || !NewFuncOwner) return 0;
	UFunction* NewFn = NewFuncOwner->FindFunctionByName(NewFuncName);
	if (!NewFn)
	{
		UE_LOG(LogTemp, Error, TEXT("BPCallFunctionRewriter: new fn %s::%s not found"),
			*NewFuncOwner->GetName(), *NewFuncName.ToString());
		return 0;
	}

	// Walk uses the shared FBPGraphWalk helper -- same set of exec graphs as
	// DumpCallSites and DumpBPGraph, so a `truth = plan = rewrite` invariant
	// holds without any "must stay congruent" review-discipline overhead.
	TArray<UEdGraph*> AllGraphs;
	FBPGraphWalk::ForEachExecGraph(BP, [&](UEdGraph* G, const FString& /*Label*/)
	{
		AllGraphs.Add(G);
	});

	int32 ReplacedCount = 0;
	for (UEdGraph* Graph : AllGraphs)
	{
		if (!Graph) continue;
		TArray<UEdGraphNode*> NodesCopy = Graph->Nodes;
		for (UEdGraphNode* Node : NodesCopy)
		{
			UK2Node_CallFunction* CallNode = Cast<UK2Node_CallFunction>(Node);
			if (!CallNode) continue;
			// Match on FunctionReference (MemberName + parent class) rather
			// than the resolved UFunction* — cross-package UClass identity can
			// differ (REINST class, stale CDO) and produce false negatives.
			const FMemberReference& Ref = CallNode->FunctionReference;
			if (Ref.GetMemberName() != OldFuncName) continue;
			// SkeletonGeneratedClass is the only resolved self-scope during early
			// pre-compile reload; GeneratedClass can be null mid-recompile.
			UClass* SelfScope = BP->GeneratedClass ? BP->GeneratedClass : BP->SkeletonGeneratedClass;
			UClass* CurOwner = Ref.GetMemberParentClass(SelfScope);
			if (!CurOwner) continue;
			// Pointer-eq is the right answer in steady state. ClassPathName
			// fallback covers REINST / hot-reload (new pointer, same package
			// path); ClassPathName already encodes Outermost so leaf-name +
			// outermost is dominated by it.
			if (CurOwner != OldFuncOwner
			    && CurOwner->GetClassPathName() != OldFuncOwner->GetClassPathName())
			{
				continue;
			}

			UE_LOG(LogTemp, Display, TEXT("BPCallFunctionRewriter: matched %s in %s/%s"),
				*OldFuncName.ToString(), *BP->GetName(), *Graph->GetName());

			UK2Node_CallFunction* NewNode = NewObject<UK2Node_CallFunction>(Graph);
			NewNode->SetFromFunction(NewFn);
			Graph->AddNode(NewNode, false, false);
			NewNode->CreateNewGuid();
			NewNode->PostPlacedNewNode();
			NewNode->AllocateDefaultPins();
			NewNode->NodePosX = CallNode->NodePosX;
			NewNode->NodePosY = CallNode->NodePosY;

			for (UEdGraphPin* OldPin : CallNode->Pins)
			{
				FName OldPinName = OldPin->PinName;
				FName NewPinName = OldPinName;
				if (const FName* Mapped = ExplicitPinMap.Find(OldPinName))
				{
					if (Mapped->IsNone()) continue;
					NewPinName = *Mapped;
				}
				UEdGraphPin* NewPin = NewNode->FindPin(NewPinName);
				if (!NewPin) continue;

				TArray<UEdGraphPin*> LinkedCopy = OldPin->LinkedTo;
				for (UEdGraphPin* LinkedPin : LinkedCopy)
				{
					OldPin->BreakLinkTo(LinkedPin);
					if (NewPin->Direction == LinkedPin->Direction) continue;

					if (NewPin->PinType == LinkedPin->PinType)
					{
						NewPin->MakeLinkTo(LinkedPin);
						continue;
					}

					// Object pin compatibility: try IsChildOf both directions,
					// insert a pure DynamicCast for wide→narrow downcast.
					const bool bBothObjects =
						NewPin->PinType.PinCategory == LinkedPin->PinType.PinCategory &&
						(NewPin->PinType.PinCategory == UEdGraphSchema_K2::PC_Object ||
						 NewPin->PinType.PinCategory == UEdGraphSchema_K2::PC_Interface ||
						 NewPin->PinType.PinCategory == UEdGraphSchema_K2::PC_Class);
					if (bBothObjects && NewPin->Direction == EGPD_Output)
					{
						UClass* NewClass    = Cast<UClass>(NewPin->PinType.PinSubCategoryObject.Get());
						UClass* LinkedClass = Cast<UClass>(LinkedPin->PinType.PinSubCategoryObject.Get());
						if (NewClass && LinkedClass)
						{
							if (NewClass->IsChildOf(LinkedClass))
							{
								NewPin->MakeLinkTo(LinkedPin); // upcast — direct link
								continue;
							}
							if (LinkedClass->IsChildOf(NewClass))
							{
								// Wide→narrow: insert pure DynamicCast<LinkedClass>.
								// SetPurity(true) auto-ReconstructNodes per UE source.
								// Use schema TryCreateConnection so the link survives
								// reconcile + disk save/reload.
								UK2Node_DynamicCast* CastNode = NewObject<UK2Node_DynamicCast>(Graph);
								CastNode->TargetType = LinkedClass;
								Graph->AddNode(CastNode, false, false);
								CastNode->CreateNewGuid();
								CastNode->PostPlacedNewNode();
								CastNode->AllocateDefaultPins();
								CastNode->NodePosX = NewNode->NodePosX + 250;
								CastNode->NodePosY = NewNode->NodePosY;
								CastNode->SetPurity(true);

								UEdGraphPin* CastInput  = CastNode->GetCastSourcePin();
								UEdGraphPin* CastOutput = CastNode->GetCastResultPin();
								const UEdGraphSchema* Schema = Graph->GetSchema();
								bool bCastWired = false;
								if (CastInput && CastOutput && Schema)
								{
									const bool ok1 = Schema->TryCreateConnection(NewPin, CastInput);
									const bool ok2 = ok1 && Schema->TryCreateConnection(CastOutput, LinkedPin);
									if (ok1 && ok2)
									{
										bCastWired = true;
										UE_LOG(LogTemp, Display,
											TEXT("BPCallFunctionRewriter: inserted pure DynamicCast<%s> between %s.%s and %s.%s"),
											*LinkedClass->GetName(),
											*NewNode->GetName(), *NewPin->PinName.ToString(),
											*LinkedPin->GetOwningNode()->GetName(),
											*LinkedPin->PinName.ToString());
										continue;
									}
									// Partial wiring (only ok1) — break the half-link before
									// we drop the cast, so the graph is left consistent.
									if (ok1 && !ok2)
									{
										NewPin->BreakLinkTo(CastInput);
									}
								}
								// Cast inject failed; remove the orphan node so we don't leave
								// a half-wired DynamicCast in the graph that the user has to
								// clean up manually. The OldPin↔LinkedPin link is already
								// broken (by the BreakLinkTo at the top of this loop iteration),
								// so the final state is "both pins disconnected" — exactly what
								// the dropped-link warning below describes.
								if (!bCastWired)
								{
									Graph->RemoveNode(CastNode);
								}
							}
						}
					}

					UE_LOG(LogTemp, Warning,
						TEXT("BPCallFunctionRewriter: pin '%s' incompatible — link to %s.%s dropped"),
						*OldPin->PinName.ToString(),
						*LinkedPin->GetOwningNode()->GetName(),
						*LinkedPin->PinName.ToString());
				}

				// Default state copy: a self pin defaulting to a CDO (DefaultObject)
				// only survives the rewrite if we preserve DefaultObject too — without
				// this, callers whose own class isn't the BP class fail to compile on
				// fresh-load with "self is not a <Class>, therefore 'Target' must
				// have a connection".
				if (OldPin->PinType.PinCategory == NewPin->PinType.PinCategory)
				{
					if (!OldPin->DefaultValue.IsEmpty())
					{
						NewPin->DefaultValue = OldPin->DefaultValue;
					}
					if (OldPin->DefaultObject != nullptr && NewPin->DefaultObject == nullptr)
					{
						// Covariant safety: only copy the default object when its
						// class IsChildOf the new pin's expected class. Without this
						// gate, a CDO of OldFuncOwner (typical for the self pin)
						// would survive into a rewritten call against an UNRELATED
						// NewFuncOwner and surface as a type-mismatch compile error
						// at fresh-load time.
						UClass* NewExpected = Cast<UClass>(NewPin->PinType.PinSubCategoryObject.Get());
						UClass* OldDefaultClass = OldPin->DefaultObject->GetClass();
						if (!NewExpected || (OldDefaultClass && OldDefaultClass->IsChildOf(NewExpected)))
						{
							NewPin->DefaultObject = OldPin->DefaultObject;
						}
					}
					if (!OldPin->DefaultTextValue.IsEmpty())
					{
						NewPin->DefaultTextValue = OldPin->DefaultTextValue;
					}
				}
			}

			Graph->RemoveNode(CallNode);
			++ReplacedCount;
			UE_LOG(LogTemp, Display, TEXT("BPCallFunctionRewriter: %s::%s -> %s::%s in %s/%s"),
				*OldFuncOwner->GetName(), *OldFuncName.ToString(),
				*NewFuncOwner->GetName(), *NewFuncName.ToString(),
				*BP->GetName(), *Graph->GetName());
		}
	}
	if (ReplacedCount > 0)
	{
		FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(BP);
	}
	return ReplacedCount;
}

int32 FBPCallFunctionRewriter::RewriteByPath(
	UBlueprint* BP,
	const FString& OldOwnerPath, FName OldFuncName,
	const FString& NewOwnerPath, FName NewFuncName,
	const TMap<FName, FName>& ExplicitPinMap)
{
	UClass* OldOwner = FindObject<UClass>(nullptr, *OldOwnerPath);
	if (!OldOwner) OldOwner = LoadObject<UClass>(nullptr, *OldOwnerPath);
	UClass* NewOwner = FindObject<UClass>(nullptr, *NewOwnerPath);
	if (!NewOwner) NewOwner = LoadObject<UClass>(nullptr, *NewOwnerPath);
	if (!OldOwner || !NewOwner)
	{
		UE_LOG(LogTemp, Error, TEXT("BPCallFunctionRewriter: class lookup failed Old=%s New=%s"),
			*OldOwnerPath, *NewOwnerPath);
		return 0;
	}
	return Rewrite(BP, OldOwner, OldFuncName, NewOwner, NewFuncName, ExplicitPinMap);
}
