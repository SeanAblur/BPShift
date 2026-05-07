// SPDX-License-Identifier: MIT
// Copyright (c) 2026 xkdlaldfjtnl

#include "DumpBPGraphCommandlet.h"

#if WITH_EDITOR

#include "Engine/Blueprint.h"
#include "EdGraph/EdGraph.h"
#include "EdGraph/EdGraphNode.h"
#include "EdGraph/EdGraphPin.h"
#include "EdGraphSchema_K2.h"
#include "K2Node.h"
#include "K2Node_Event.h"
#include "K2Node_FunctionEntry.h"
#include "K2Node_FunctionResult.h"
#include "K2Node_CallFunction.h"
#include "K2Node_DynamicCast.h"
#include "K2Node_IfThenElse.h"
#include "K2Node_MacroInstance.h"
#include "K2Node_VariableGet.h"
#include "K2Node_VariableSet.h"
#include "K2Node_CustomEvent.h"
#include "K2Node_BaseMCDelegate.h"
#include "K2Node_CreateDelegate.h"
#include "K2Node_BaseAsyncTask.h"
#include "Misc/FileHelper.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "Engine/SimpleConstructionScript.h"
#include "Engine/SCS_Node.h"
#include "UObject/UnrealType.h"

int32 UDumpBPGraphCommandlet::Main(const FString& Params)
{
	TArray<FString> Tokens;
	TArray<FString> Switches;
	TMap<FString, FString> ParamsMap;
	ParseCommandLine(*Params, Tokens, Switches, ParamsMap);

	if (Tokens.Num() == 0)
	{
		UE_LOG(LogTemp, Error, TEXT("Usage: -run=DumpBPGraph /Game/Path/To/BP [-output=path.json]"));
		return 1;
	}

	const FString AssetPath = Tokens[0];
	FString OutputPath = ParamsMap.FindRef(TEXT("output"));
	if (OutputPath.IsEmpty())
	{
		FString AssetName = FPaths::GetBaseFilename(AssetPath);
		OutputPath = FPaths::Combine(FPlatformProcess::UserTempDir(), TEXT("BPShift"), AssetName + TEXT("_graph.json"));
	}

	UBlueprint* Blueprint = LoadObject<UBlueprint>(nullptr, *AssetPath);
	if (!Blueprint)
	{
		UE_LOG(LogTemp, Error, TEXT("Failed to load Blueprint: %s"), *AssetPath);
		return 1;
	}

	UE_LOG(LogTemp, Display, TEXT("Loaded: %s (Parent: %s)"), *Blueprint->GetName(),
		Blueprint->ParentClass ? *Blueprint->ParentClass->GetName() : TEXT("None"));

	// Set SelfScope for broken-reference detection in SerializeNode().
	// Prefer SkeletonGeneratedClass (always fresh after a recompile);
	// fall back to GeneratedClass when skeleton compilation has not run.
	SelfScope = Blueprint->SkeletonGeneratedClass
		? Blueprint->SkeletonGeneratedClass.Get()
		: Blueprint->GeneratedClass.Get();

	// Root JSON
	TSharedPtr<FJsonObject> Root = MakeShareable(new FJsonObject);
	Root->SetStringField(TEXT("Name"), Blueprint->GetName());
	Root->SetStringField(TEXT("ParentClass"), Blueprint->ParentClass ? Blueprint->ParentClass->GetName() : TEXT("None"));
	Root->SetStringField(TEXT("BlueprintType"), StaticEnum<EBlueprintType>()->GetNameStringByValue((int64)Blueprint->BlueprintType));

	// Variables
	TArray<TSharedPtr<FJsonValue>> VarsArray;
	for (const FBPVariableDescription& Var : Blueprint->NewVariables)
	{
		TSharedPtr<FJsonObject> VarObj = MakeShareable(new FJsonObject);
		VarObj->SetStringField(TEXT("Name"), Var.VarName.ToString());
		VarObj->SetStringField(TEXT("Type"), Var.VarType.PinCategory.ToString());
		VarObj->SetStringField(TEXT("SubType"), Var.VarType.PinSubCategoryObject.IsValid()
			? Var.VarType.PinSubCategoryObject->GetName() : TEXT(""));
		VarObj->SetStringField(TEXT("DefaultValue"), Var.DefaultValue);
		VarObj->SetStringField(TEXT("Category"), Var.Category.ToString());
		VarObj->SetBoolField(TEXT("InstanceEditable"), !!(Var.PropertyFlags & CPF_Edit));
		VarObj->SetBoolField(TEXT("BlueprintReadOnly"), !!(Var.PropertyFlags & CPF_BlueprintReadOnly));
		VarsArray.Add(MakeShareable(new FJsonValueObject(VarObj)));
	}
	Root->SetArrayField(TEXT("Variables"), VarsArray);

	// Graphs
	TArray<TSharedPtr<FJsonValue>> GraphsArray;

	for (UEdGraph* Graph : Blueprint->FunctionGraphs)
	{
		if (!Graph) continue;
		TSharedPtr<FJsonObject> G = SerializeGraph(Graph);
		G->SetStringField(TEXT("GraphType"), TEXT("Function"));
		GraphsArray.Add(MakeShareable(new FJsonValueObject(G)));
	}

	for (UEdGraph* Graph : Blueprint->UbergraphPages)
	{
		if (!Graph) continue;
		TSharedPtr<FJsonObject> G = SerializeGraph(Graph);
		G->SetStringField(TEXT("GraphType"), TEXT("EventGraph"));
		GraphsArray.Add(MakeShareable(new FJsonValueObject(G)));
	}

	for (UEdGraph* Graph : Blueprint->MacroGraphs)
	{
		if (!Graph) continue;
		TSharedPtr<FJsonObject> G = SerializeGraph(Graph);
		G->SetStringField(TEXT("GraphType"), TEXT("Macro"));
		GraphsArray.Add(MakeShareable(new FJsonValueObject(G)));
	}

	// Delegate graphs
	for (UEdGraph* Graph : Blueprint->DelegateSignatureGraphs)
	{
		if (!Graph) continue;
		TSharedPtr<FJsonObject> G = SerializeGraph(Graph);
		G->SetStringField(TEXT("GraphType"), TEXT("DelegateSignature"));
		GraphsArray.Add(MakeShareable(new FJsonValueObject(G)));
	}

	// Interface implementation graphs. Each implemented BPI has its own
	// per-method graphs that the BP overrides; without these the dump
	// misses the bulk of the BP's logic on interface-heavy BPs.
	for (const FBPInterfaceDescription& Iface : Blueprint->ImplementedInterfaces)
	{
		const FString IfaceName = Iface.Interface ? Iface.Interface->GetName() : TEXT("UnknownInterface");
		for (UEdGraph* Graph : Iface.Graphs)
		{
			if (!Graph) continue;
			TSharedPtr<FJsonObject> G = SerializeGraph(Graph);
			G->SetStringField(TEXT("GraphType"), TEXT("InterfaceImpl"));
			G->SetStringField(TEXT("Interface"), IfaceName);
			GraphsArray.Add(MakeShareable(new FJsonValueObject(G)));
		}
	}

	Root->SetArrayField(TEXT("Graphs"), GraphsArray);

	// Interfaces
	TArray<TSharedPtr<FJsonValue>> IfaceArray;
	for (const FBPInterfaceDescription& Iface : Blueprint->ImplementedInterfaces)
	{
		if (Iface.Interface)
		{
			IfaceArray.Add(MakeShareable(new FJsonValueString(Iface.Interface->GetName())));
		}
	}
	Root->SetArrayField(TEXT("Interfaces"), IfaceArray);

	// Components (SimpleConstructionScript node tree). Must be replicated
	// in the C++ constructor (CreateDefaultSubobject) — otherwise initial
	// state silently loses them.
	TArray<TSharedPtr<FJsonValue>> CompArray;
	if (USimpleConstructionScript* SCS = Blueprint->SimpleConstructionScript)
	{
		TFunction<TSharedPtr<FJsonObject>(USCS_Node*)> SerializeSCSNode;
		SerializeSCSNode = [&](USCS_Node* Node) -> TSharedPtr<FJsonObject>
		{
			TSharedPtr<FJsonObject> Obj = MakeShareable(new FJsonObject);
			Obj->SetStringField(TEXT("Name"), Node->GetVariableName().ToString());
			Obj->SetStringField(TEXT("ComponentClass"),
				Node->ComponentClass ? Node->ComponentClass->GetName() : TEXT("None"));
			Obj->SetStringField(TEXT("ParentNode"),
				Node->ParentComponentOrVariableName.ToString());
			Obj->SetBoolField(TEXT("ParentIsNative"), Node->bIsParentComponentNative);

			// Component template overrides: when the BP editor changes a
			// SceneComponent's relative transform on a SCS node, those
			// values live on the ComponentTemplate. Migrating only the
			// component class without these overrides loses runtime state.
			if (UActorComponent* CompTemplate = Node->ComponentTemplate)
			{
				UObject* CompArchetype = CompTemplate->GetArchetype();
				TArray<TSharedPtr<FJsonValue>> OverrideArr;
				for (TFieldIterator<FProperty> It(CompTemplate->GetClass()); It; ++It)
				{
					FProperty* P = *It;
					if (P->HasAnyPropertyFlags(CPF_Transient | CPF_DuplicateTransient))
						continue;
					const void* TVal = P->ContainerPtrToValuePtr<void>(CompTemplate);
					const void* AVal = CompArchetype ? P->ContainerPtrToValuePtr<void>(CompArchetype) : nullptr;
					if (AVal && P->Identical(TVal, AVal, PPF_None))
						continue;

					FString TStr;
					P->ExportTextItem_Direct(TStr, TVal, nullptr, CompTemplate, PPF_None);
					FString AStr;
					if (AVal)
					{
						P->ExportTextItem_Direct(AStr, AVal, nullptr, CompArchetype, PPF_None);
					}

					// Was 256 — too small for OverrideMaterials arrays (long-form
					// "/Script/Engine.MaterialInstanceConstant'\"/Game/...\"'" entries
					// hit ~130 chars each). Truncation broke deterministic codegen.
					// 8192 covers 50+ material entries; we still cap to bound JSON size.
					const int32 MaxLen = 8192;
					if (TStr.Len() > MaxLen) TStr = TStr.Left(MaxLen) + TEXT("...");
					if (AStr.Len() > MaxLen) AStr = AStr.Left(MaxLen) + TEXT("...");

					TSharedPtr<FJsonObject> OEntry = MakeShareable(new FJsonObject);
					OEntry->SetStringField(TEXT("Property"), P->GetName());
					OEntry->SetStringField(TEXT("Type"), P->GetClass()->GetName());
					OEntry->SetStringField(TEXT("OurValue"), TStr);
					OEntry->SetStringField(TEXT("ArchetypeDefault"), AStr);
					OverrideArr.Add(MakeShareable(new FJsonValueObject(OEntry)));
				}
				if (OverrideArr.Num() > 0)
				{
					Obj->SetArrayField(TEXT("DefaultOverrides"), OverrideArr);
				}
			}

			TArray<TSharedPtr<FJsonValue>> Children;
			for (USCS_Node* Child : Node->ChildNodes)
			{
				if (Child) Children.Add(MakeShareable(new FJsonValueObject(SerializeSCSNode(Child))));
			}
			Obj->SetArrayField(TEXT("Children"), Children);
			return Obj;
		};
		for (USCS_Node* RootNode : SCS->GetRootNodes())
		{
			if (RootNode) CompArray.Add(MakeShareable(new FJsonValueObject(SerializeSCSNode(RootNode))));
		}
	}
	Root->SetArrayField(TEXT("Components"), CompArray);

	// Class flags — must be reflected in C++ UCLASS specifier.
	if (Blueprint->GeneratedClass)
	{
		EClassFlags Flags = Blueprint->GeneratedClass->GetClassFlags();
		TArray<TSharedPtr<FJsonValue>> FlagsArr;
		if (Flags & CLASS_Abstract)        FlagsArr.Add(MakeShareable(new FJsonValueString(TEXT("Abstract"))));
		if (Flags & CLASS_NotPlaceable)    FlagsArr.Add(MakeShareable(new FJsonValueString(TEXT("NotPlaceable"))));
		if (Flags & CLASS_DefaultConfig)   FlagsArr.Add(MakeShareable(new FJsonValueString(TEXT("DefaultConfig"))));
		if (Flags & CLASS_Const)           FlagsArr.Add(MakeShareable(new FJsonValueString(TEXT("Const"))));
		if (Flags & CLASS_Hidden)          FlagsArr.Add(MakeShareable(new FJsonValueString(TEXT("Hidden"))));
		if (Flags & CLASS_Deprecated)      FlagsArr.Add(MakeShareable(new FJsonValueString(TEXT("Deprecated"))));
		Root->SetArrayField(TEXT("ClassFlags"), FlagsArr);
	}

	// Parent CDO overrides — inherited fields whose CDO value differs from
	// the parent class default. State the BP set in the editor's Class
	// Defaults panel that is NOT a NewVariable. Common: PrimaryActorTick,
	// RootComponent, replication flags, anim instance class. Migration
	// must propagate these in the C++ constructor or the runtime diverges.
	TArray<TSharedPtr<FJsonValue>> CDOArray;
	if (UClass* GenClass = Blueprint->GeneratedClass)
	{
		UObject* OurCDO = GenClass->GetDefaultObject();
		UClass* ParentClass = GenClass->GetSuperClass();
		UObject* ParentCDO = ParentClass ? ParentClass->GetDefaultObject() : nullptr;
		if (OurCDO && ParentCDO)
		{
			for (TFieldIterator<FProperty> It(ParentClass); It; ++It)
			{
				FProperty* Prop = *It;
				if (Prop->HasAnyPropertyFlags(CPF_Transient | CPF_DuplicateTransient))
					continue;
				const void* OurVal = Prop->ContainerPtrToValuePtr<void>(OurCDO);
				const void* ParentVal = Prop->ContainerPtrToValuePtr<void>(ParentCDO);
				if (Prop->Identical(OurVal, ParentVal, PPF_None))
					continue;

				FString OurStr;
				Prop->ExportTextItem_Direct(OurStr, OurVal, nullptr, OurCDO, PPF_None);
				FString ParentStr;
				Prop->ExportTextItem_Direct(ParentStr, ParentVal, nullptr, ParentCDO, PPF_None);
				// Same rationale as the SCS DefaultOverrides cap above:
				// 200 truncated multi-asset arrays / nested struct overrides.
				const int32 MaxLen = 8192;
				if (OurStr.Len() > MaxLen) OurStr = OurStr.Left(MaxLen) + TEXT("...");
				if (ParentStr.Len() > MaxLen) ParentStr = ParentStr.Left(MaxLen) + TEXT("...");

				TSharedPtr<FJsonObject> Obj = MakeShareable(new FJsonObject);
				Obj->SetStringField(TEXT("Property"), Prop->GetName());
				Obj->SetStringField(TEXT("Type"), Prop->GetClass()->GetName());
				Obj->SetStringField(TEXT("OurValue"), OurStr);
				Obj->SetStringField(TEXT("ParentDefault"), ParentStr);
				CDOArray.Add(MakeShareable(new FJsonValueObject(Obj)));
			}
		}
	}
	Root->SetArrayField(TEXT("ParentCDOOverrides"), CDOArray);

	// Write
	FString OutputString;
	auto Writer = TJsonWriterFactory<TCHAR, TPrettyJsonPrintPolicy<TCHAR>>::Create(&OutputString);
	FJsonSerializer::Serialize(Root.ToSharedRef(), Writer);

	IFileManager::Get().MakeDirectory(*FPaths::GetPath(OutputPath), true);
	if (FFileHelper::SaveStringToFile(OutputString, *OutputPath, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM))
	{
		UE_LOG(LogTemp, Display, TEXT("Written: %s (%d chars)"), *OutputPath, OutputString.Len());
	}
	else
	{
		UE_LOG(LogTemp, Error, TEXT("Failed to write: %s"), *OutputPath);
		return 1;
	}

	return 0;
}

TSharedPtr<FJsonObject> UDumpBPGraphCommandlet::SerializeGraph(UEdGraph* Graph)
{
	TSharedPtr<FJsonObject> Obj = MakeShareable(new FJsonObject);
	Obj->SetStringField(TEXT("Name"), Graph->GetName());

	TArray<TSharedPtr<FJsonValue>> NodesArr;
	for (UEdGraphNode* Node : Graph->Nodes)
	{
		if (Node) NodesArr.Add(MakeShareable(new FJsonValueObject(SerializeNode(Node))));
	}
	Obj->SetArrayField(TEXT("Nodes"), NodesArr);
	return Obj;
}

TSharedPtr<FJsonObject> UDumpBPGraphCommandlet::SerializeNode(UEdGraphNode* Node)
{
	TSharedPtr<FJsonObject> Obj = MakeShareable(new FJsonObject);
	Obj->SetStringField(TEXT("Class"), Node->GetClass()->GetName());
	Obj->SetStringField(TEXT("Title"), Node->GetNodeTitle(ENodeTitleType::FullTitle).ToString());
	Obj->SetStringField(TEXT("CompactTitle"), Node->GetNodeTitle(ENodeTitleType::ListView).ToString());
	Obj->SetNumberField(TEXT("PosX"), Node->NodePosX);
	Obj->SetNumberField(TEXT("PosY"), Node->NodePosY);
	Obj->SetStringField(TEXT("Comment"), Node->NodeComment);
	Obj->SetBoolField(TEXT("CommentBubbleVisible"), Node->bCommentBubbleVisible);
	Obj->SetStringField(TEXT("Guid"), Node->NodeGuid.ToString());

	// Type-specific info. Each K2Node branch also reports `Resolved` and an
	// `UnresolvedReason` so detect-gaps can catch references the Blueprint
	// editor would draw red after a reparent / member rename.
	if (auto* CF = Cast<UK2Node_CallFunction>(Node))
	{
		const FName FuncName = CF->FunctionReference.GetMemberName();
		Obj->SetStringField(TEXT("FunctionName"), FuncName.ToString());
		UClass* Parent = CF->FunctionReference.GetMemberParentClass();
		if (Parent) Obj->SetStringField(TEXT("TargetClass"), Parent->GetName());
		Obj->SetBoolField(TEXT("IsPure"), CF->IsNodePure());

		UFunction* Resolved = CF->GetTargetFunction();
		if (!Resolved && SelfScope)
		{
			Resolved = CF->FunctionReference.ResolveMember<UFunction>(SelfScope);
		}
		Obj->SetBoolField(TEXT("Resolved"), Resolved != nullptr);
		if (!Resolved)
		{
			Obj->SetStringField(TEXT("UnresolvedReason"),
				Parent
					? FString::Printf(TEXT("function '%s' not found in class '%s'"),
						*FuncName.ToString(), *Parent->GetName())
					: FString::Printf(TEXT("function '%s' has no parent class (member parent unresolved)"),
						*FuncName.ToString()));
		}
	}
	else if (auto* Ev = Cast<UK2Node_Event>(Node))
	{
		Obj->SetStringField(TEXT("EventName"), Ev->EventReference.GetMemberName().ToString());
		Obj->SetStringField(TEXT("NodeType"), TEXT("Event"));
		Obj->SetBoolField(TEXT("IsOverride"), Ev->bOverrideFunction);

		// Override events must resolve to a parent UFunction; non-override
		// (custom) events are self-contained and always Resolved.
		bool bResolved = !Ev->bOverrideFunction;
		if (Ev->bOverrideFunction && SelfScope)
		{
			UFunction* F = Ev->EventReference.ResolveMember<UFunction>(SelfScope);
			bResolved = (F != nullptr);
			if (!bResolved)
			{
				Obj->SetStringField(TEXT("UnresolvedReason"),
					FString::Printf(TEXT("override event '%s' not found in any parent class"),
						*Ev->EventReference.GetMemberName().ToString()));
			}
		}
		Obj->SetBoolField(TEXT("Resolved"), bResolved);
	}
	else if (auto* CE = Cast<UK2Node_CustomEvent>(Node))
	{
		Obj->SetStringField(TEXT("CustomEventName"), CE->CustomFunctionName.ToString());
		Obj->SetStringField(TEXT("NodeType"), TEXT("CustomEvent"));
		Obj->SetBoolField(TEXT("Resolved"), true);  // self-defined, always valid
	}
	else if (auto* MI = Cast<UK2Node_MacroInstance>(Node))
	{
		auto* MG = MI->GetMacroGraph();
		Obj->SetStringField(TEXT("MacroName"), MG ? MG->GetName() : TEXT("None"));
		if (MG && MG->GetOuter())
			Obj->SetStringField(TEXT("MacroSource"), MG->GetOuter()->GetName());
		Obj->SetStringField(TEXT("NodeType"), TEXT("MacroInstance"));
		Obj->SetBoolField(TEXT("Resolved"), MG != nullptr);
		if (!MG)
		{
			Obj->SetStringField(TEXT("UnresolvedReason"),
				TEXT("macro graph not found (referenced macro library deleted or moved)"));
		}
	}
	else if (auto* DC = Cast<UK2Node_DynamicCast>(Node))
	{
		Obj->SetStringField(TEXT("TargetType"), DC->TargetType ? DC->TargetType->GetName() : TEXT("None"));
		Obj->SetStringField(TEXT("NodeType"), TEXT("DynamicCast"));
		Obj->SetBoolField(TEXT("Resolved"), DC->TargetType != nullptr);
		if (!DC->TargetType)
		{
			Obj->SetStringField(TEXT("UnresolvedReason"),
				TEXT("Cast target class is null (deleted or moved class)"));
		}
	}
	else if (Cast<UK2Node_IfThenElse>(Node))
	{
		Obj->SetStringField(TEXT("NodeType"), TEXT("Branch"));
	}
	else if (auto* VG = Cast<UK2Node_VariableGet>(Node))
	{
		const FName VarName = VG->GetVarName();
		Obj->SetStringField(TEXT("VariableName"), VarName.ToString());
		Obj->SetStringField(TEXT("NodeType"), TEXT("VariableGet"));
		UClass* Parent = VG->VariableReference.GetMemberParentClass();
		if (Parent) Obj->SetStringField(TEXT("TargetClass"), Parent->GetName());

		FProperty* Resolved = VG->GetPropertyForVariable();
		if (!Resolved && SelfScope)
		{
			Resolved = VG->VariableReference.ResolveMember<FProperty>(SelfScope);
		}
		Obj->SetBoolField(TEXT("Resolved"), Resolved != nullptr);
		if (!Resolved)
		{
			Obj->SetStringField(TEXT("UnresolvedReason"),
				Parent
					? FString::Printf(TEXT("variable '%s' not found in class '%s'"),
						*VarName.ToString(), *Parent->GetName())
					: FString::Printf(TEXT("variable '%s' has no parent class (member parent unresolved)"),
						*VarName.ToString()));
		}
	}
	else if (auto* VS = Cast<UK2Node_VariableSet>(Node))
	{
		const FName VarName = VS->GetVarName();
		Obj->SetStringField(TEXT("VariableName"), VarName.ToString());
		Obj->SetStringField(TEXT("NodeType"), TEXT("VariableSet"));
		UClass* Parent = VS->VariableReference.GetMemberParentClass();
		if (Parent) Obj->SetStringField(TEXT("TargetClass"), Parent->GetName());

		FProperty* Resolved = VS->GetPropertyForVariable();
		if (!Resolved && SelfScope)
		{
			Resolved = VS->VariableReference.ResolveMember<FProperty>(SelfScope);
		}
		Obj->SetBoolField(TEXT("Resolved"), Resolved != nullptr);
		if (!Resolved)
		{
			Obj->SetStringField(TEXT("UnresolvedReason"),
				Parent
					? FString::Printf(TEXT("variable '%s' not found in class '%s'"),
						*VarName.ToString(), *Parent->GetName())
					: FString::Printf(TEXT("variable '%s' has no parent class (member parent unresolved)"),
						*VarName.ToString()));
		}
	}
	else if (auto* MCD = Cast<UK2Node_BaseMCDelegate>(Node))
	{
		// Covers Add / Remove / Clear / Call / Assign delegate operations.
		// Reparent commonly invalidates dispatcher bindings -- this branch
		// is the only way detect-gaps catches them.
		const FName DelegateName = MCD->GetPropertyName();
		Obj->SetStringField(TEXT("DelegateName"), DelegateName.ToString());
		UClass* Parent = MCD->DelegateReference.GetMemberParentClass();
		if (Parent) Obj->SetStringField(TEXT("TargetClass"), Parent->GetName());
		Obj->SetStringField(TEXT("NodeType"), TEXT("DelegateOp"));

		// UE5.2 has no `GetTargetDelegateProperty()` accessor; resolve via
		// the member reference directly (same code path the K2Node uses
		// internally, see K2Node_BaseMCDelegate.h).
		FMulticastDelegateProperty* Resolved = nullptr;
		if (SelfScope)
		{
			Resolved = MCD->DelegateReference.ResolveMember<FMulticastDelegateProperty>(SelfScope);
		}
		Obj->SetBoolField(TEXT("Resolved"), Resolved != nullptr);
		if (!Resolved)
		{
			Obj->SetStringField(TEXT("UnresolvedReason"),
				Parent
					? FString::Printf(TEXT("multicast delegate '%s' not found in class '%s'"),
						*DelegateName.ToString(), *Parent->GetName())
					: FString::Printf(TEXT("multicast delegate '%s' has no parent class (member parent unresolved)"),
						*DelegateName.ToString()));
		}
	}
	else if (auto* CD = Cast<UK2Node_CreateDelegate>(Node))
	{
		const FName SelectedFunc = CD->GetFunctionName();
		Obj->SetStringField(TEXT("FunctionName"), SelectedFunc.ToString());
		Obj->SetStringField(TEXT("NodeType"), TEXT("CreateDelegate"));

		UFunction* Resolved = CD->GetDelegateSignature();
		Obj->SetBoolField(TEXT("Resolved"), Resolved != nullptr && !SelectedFunc.IsNone());
		if (!Resolved || SelectedFunc.IsNone())
		{
			Obj->SetStringField(TEXT("UnresolvedReason"),
				FString::Printf(TEXT("CreateDelegate target function '%s' could not be resolved"),
					*SelectedFunc.ToString()));
		}
	}
	else if (auto* AT = Cast<UK2Node_BaseAsyncTask>(Node))
	{
		// AsyncAction / latent. ProxyClass + ProxyFactoryFunctionName are
		// `protected` on UE5.2 -- read via FProperty reflection so we don't
		// have to subclass or befriend the K2Node.
		FName ProxyFunc = NAME_None;
		UClass* ProxyClass = nullptr;
		if (FNameProperty* PFP = CastField<FNameProperty>(
			AT->GetClass()->FindPropertyByName(TEXT("ProxyFactoryFunctionName"))))
		{
			ProxyFunc = PFP->GetPropertyValue_InContainer(AT);
		}
		if (FObjectProperty* PCP = CastField<FObjectProperty>(
			AT->GetClass()->FindPropertyByName(TEXT("ProxyClass"))))
		{
			ProxyClass = Cast<UClass>(PCP->GetObjectPropertyValue_InContainer(AT));
		}
		Obj->SetStringField(TEXT("ProxyFunction"), ProxyFunc.ToString());
		Obj->SetStringField(TEXT("ProxyClass"), ProxyClass ? ProxyClass->GetName() : TEXT("None"));
		Obj->SetStringField(TEXT("NodeType"), TEXT("AsyncTask"));

		bool bResolved = ProxyClass != nullptr;
		if (bResolved)
		{
			UFunction* Found = ProxyClass->FindFunctionByName(ProxyFunc);
			bResolved = (Found != nullptr);
		}
		Obj->SetBoolField(TEXT("Resolved"), bResolved);
		if (!bResolved)
		{
			Obj->SetStringField(TEXT("UnresolvedReason"),
				ProxyClass
					? FString::Printf(TEXT("async proxy function '%s' not found on class '%s'"),
						*ProxyFunc.ToString(), *ProxyClass->GetName())
					: FString::Printf(TEXT("async proxy class is null for function '%s'"),
						*ProxyFunc.ToString()));
		}
	}
	else if (Cast<UK2Node_FunctionEntry>(Node))
	{
		Obj->SetStringField(TEXT("NodeType"), TEXT("FunctionEntry"));
	}
	else if (Cast<UK2Node_FunctionResult>(Node))
	{
		Obj->SetStringField(TEXT("NodeType"), TEXT("FunctionResult"));
	}

	// Comment nodes
	if (Node->GetClass()->GetName() == TEXT("EdGraphNode_Comment"))
	{
		Obj->SetStringField(TEXT("NodeType"), TEXT("Comment"));
		Obj->SetNumberField(TEXT("Width"), Node->NodeWidth);
		Obj->SetNumberField(TEXT("Height"), Node->NodeHeight);
	}

	// Pins
	TArray<TSharedPtr<FJsonValue>> PinsArr;
	for (UEdGraphPin* Pin : Node->Pins)
	{
		if (Pin) PinsArr.Add(MakeShareable(new FJsonValueObject(SerializePin(Pin))));
	}
	Obj->SetArrayField(TEXT("Pins"), PinsArr);

	return Obj;
}

TSharedPtr<FJsonObject> UDumpBPGraphCommandlet::SerializePin(UEdGraphPin* Pin)
{
	TSharedPtr<FJsonObject> Obj = MakeShareable(new FJsonObject);
	Obj->SetStringField(TEXT("Name"), Pin->PinName.ToString());
	Obj->SetStringField(TEXT("FriendlyName"), Pin->PinFriendlyName.IsEmpty() ? TEXT("") : Pin->PinFriendlyName.ToString());
	Obj->SetStringField(TEXT("Direction"), Pin->Direction == EGPD_Input ? TEXT("Input") : TEXT("Output"));
	Obj->SetStringField(TEXT("Type"), Pin->PinType.PinCategory.ToString());
	Obj->SetStringField(TEXT("SubType"), Pin->PinType.PinSubCategoryObject.IsValid()
		? Pin->PinType.PinSubCategoryObject->GetName() : TEXT(""));
	Obj->SetStringField(TEXT("SubCategory"), Pin->PinType.PinSubCategory.ToString());
	Obj->SetBoolField(TEXT("IsArray"), Pin->PinType.IsArray());
	Obj->SetBoolField(TEXT("IsMap"), Pin->PinType.IsMap());
	Obj->SetBoolField(TEXT("IsSet"), Pin->PinType.IsSet());
	Obj->SetBoolField(TEXT("IsReference"), Pin->PinType.bIsReference);
	Obj->SetBoolField(TEXT("IsConst"), Pin->PinType.bIsConst);
	Obj->SetStringField(TEXT("DefaultValue"), Pin->DefaultValue);
	Obj->SetStringField(TEXT("DefaultObject"), Pin->DefaultObject ? Pin->DefaultObject->GetPathName() : TEXT(""));
	Obj->SetStringField(TEXT("AutoDefaultValue"), Pin->AutogeneratedDefaultValue);
	Obj->SetStringField(TEXT("Guid"), Pin->PinId.ToString());
	Obj->SetBoolField(TEXT("Hidden"), Pin->bHidden);
	Obj->SetBoolField(TEXT("Orphan"), Pin->bOrphanedPin);
	Obj->SetBoolField(TEXT("Advanced"), Pin->bAdvancedView);

	// Connected pins
	TArray<TSharedPtr<FJsonValue>> Links;
	for (UEdGraphPin* Linked : Pin->LinkedTo)
	{
		if (!Linked || !Linked->GetOwningNode()) continue;

		TSharedPtr<FJsonObject> Link = MakeShareable(new FJsonObject);
		Link->SetStringField(TEXT("NodeGuid"), Linked->GetOwningNode()->NodeGuid.ToString());
		Link->SetStringField(TEXT("NodeTitle"), Linked->GetOwningNode()->GetNodeTitle(ENodeTitleType::FullTitle).ToString());
		Link->SetStringField(TEXT("PinName"), Linked->PinName.ToString());
		Link->SetStringField(TEXT("PinGuid"), Linked->PinId.ToString());
		Links.Add(MakeShareable(new FJsonValueObject(Link)));
	}
	Obj->SetArrayField(TEXT("LinkedTo"), Links);

	return Obj;
}

#else // !WITH_EDITOR

int32 UDumpBPGraphCommandlet::Main(const FString& Params)
{
	UE_LOG(LogTemp, Error, TEXT("DumpBPGraph commandlet is only available in editor builds."));
	return 1;
}

TSharedPtr<FJsonObject> UDumpBPGraphCommandlet::SerializeGraph(UEdGraph* Graph) { return nullptr; }
TSharedPtr<FJsonObject> UDumpBPGraphCommandlet::SerializeNode(UEdGraphNode* Node) { return nullptr; }
TSharedPtr<FJsonObject> UDumpBPGraphCommandlet::SerializePin(UEdGraphPin* Pin) { return nullptr; }

#endif // WITH_EDITOR
