// SPDX-License-Identifier: MIT
// Copyright (c) 2026 xkdlaldfjtnl

#include "BPBehaviorSnapshot.h"

#include "Engine/World.h"
#include "Engine/Engine.h"
#include "Engine/Blueprint.h"
#include "GameFramework/Actor.h"
#include "Components/ActorComponent.h"
#include "Blueprint/UserWidget.h"
#include "JsonObjectWrapper.h"
#include "Misc/FileHelper.h"
#include "Misc/Guid.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonWriter.h"
#include "Serialization/JsonSerializer.h"
#include "UObject/UnrealType.h"
#include "UObject/TextProperty.h"

// -- Static members --

TMap<FGuid, int32> FBPBehaviorSnapshot::GuidPlaceholderMap;
int32 FBPBehaviorSnapshot::GuidPlaceholderCounter = 0;

// -- Instance creation --

UWorld* FBPBehaviorSnapshot::CreateTransientWorld()
{
	UWorld* World = UWorld::CreateWorld(EWorldType::Game, false, FName("BPSnapshotTestWorld"));
	FWorldContext& WorldContext = GEngine->CreateNewWorldContext(EWorldType::Game);
	WorldContext.SetCurrentWorld(World);
	World->InitializeActorsForPlay(FURL());
	World->BeginPlay();
	return World;
}

UObject* FBPBehaviorSnapshot::CreateTestInstance(UClass* Class, UWorld* World)
{
	if (!Class)
	{
		return nullptr;
	}

	// ActorComponent
	if (Class->IsChildOf(UActorComponent::StaticClass()))
	{
		// ActorComponents need an owner Actor; spawn a transient one.
		AActor* OwnerActor = World->SpawnActor<AActor>();
		UActorComponent* Comp = NewObject<UActorComponent>(OwnerActor, Class);
		Comp->RegisterComponent();
		return Comp;
	}

	// Actor
	if (Class->IsChildOf(AActor::StaticClass()))
	{
		FActorSpawnParameters SpawnParams;
		SpawnParams.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AlwaysSpawn;
		return World->SpawnActor(Class, nullptr, nullptr, SpawnParams);
	}

	// UserWidget
	if (Class->IsChildOf(UUserWidget::StaticClass()))
	{
		return CreateWidget<UUserWidget>(World, Class);
	}

	// FunctionLibrary or plain UObject
	if (Class->HasAnyClassFlags(CLASS_Abstract))
	{
		return Class->GetDefaultObject();
	}

	return NewObject<UObject>(GetTransientPackage(), Class);
}

// -- Property snapshot --

TSharedPtr<FJsonObject> FBPBehaviorSnapshot::SnapshotProperties(UObject* Object)
{
	TSharedPtr<FJsonObject> Result = MakeShareable(new FJsonObject);
	if (!Object)
	{
		return Result;
	}

	for (TFieldIterator<FProperty> PropIt(Object->GetClass()); PropIt; ++PropIt)
	{
		FProperty* Prop = *PropIt;

		// Skip framework-internal properties.
		if (Prop->HasAnyPropertyFlags(CPF_Transient | CPF_DuplicateTransient))
		{
			continue;
		}

		// Skip delegates (verified indirectly).
		if (CastField<FMulticastDelegateProperty>(Prop) || CastField<FDelegateProperty>(Prop))
		{
			continue;
		}

		// Skip BP-internal accounting fields that have no meaningful native-C++
		// equivalent. Comparing them always reports spurious `valueMismatch`
		// in BP -> C++ migration even when the migration is correct. The
		// components themselves are still compared via their named UPROPERTY
		// references.
		//   BlueprintCreatedComponents : SCS bookkeeping; native is empty.
		//   UCSSerializationIndex      : BP SCS sets this; native instance -1.
		//   bNetAddressable            : BP SCS sets true; native default false.
		//   CreationMethod             : SimpleConstructionScript vs Native is
		//                                intrinsic to BP-vs-native and never
		//                                converges -- comparing it always FAILs.
		//   StaticMeshImportVersion    : editor sets on .uasset import; runtime
		//                                instance is 0 unless that asset is on
		//                                disk being re-imported.
		static const TSet<FName> BPOnlyAccountingFields = {
			TEXT("BlueprintCreatedComponents"),
			TEXT("UCSSerializationIndex"),
			TEXT("bNetAddressable"),
			TEXT("CreationMethod"),
			TEXT("StaticMeshImportVersion"),
			TEXT("bVisualizeComponent"),  // editor-only billboard toggle
		};
		if (BPOnlyAccountingFields.Contains(Prop->GetFName()))
		{
			continue;
		}

		const FString PropName = Prop->GetName();
		const void* ValuePtr = Prop->ContainerPtrToValuePtr<void>(Object);
		TSharedPtr<FJsonValue> JsonValue = SerializeProperty(Prop, ValuePtr);

		if (JsonValue.IsValid())
		{
			Result->SetField(PropName, JsonValue);
		}
	}

	return Result;
}

TSharedPtr<FJsonValue> FBPBehaviorSnapshot::SerializeProperty(FProperty* Prop, const void* ValuePtr, int32 Depth)
{
	if (!Prop || !ValuePtr)
	{
		return MakeShareable(new FJsonValueNull());
	}

	// Bool
	if (const FBoolProperty* BoolProp = CastField<FBoolProperty>(Prop))
	{
		return MakeShareable(new FJsonValueBoolean(BoolProp->GetPropertyValue(ValuePtr)));
	}

	// Int
	if (const FIntProperty* IntProp = CastField<FIntProperty>(Prop))
	{
		return MakeShareable(new FJsonValueNumber(IntProp->GetPropertyValue(ValuePtr)));
	}

	// Float
	if (const FFloatProperty* FloatProp = CastField<FFloatProperty>(Prop))
	{
		return MakeShareable(new FJsonValueNumber(FloatProp->GetPropertyValue(ValuePtr)));
	}

	// Double
	if (const FDoubleProperty* DoubleProp = CastField<FDoubleProperty>(Prop))
	{
		return MakeShareable(new FJsonValueNumber(DoubleProp->GetPropertyValue(ValuePtr)));
	}

	// String
	if (const FStrProperty* StrProp = CastField<FStrProperty>(Prop))
	{
		return MakeShareable(new FJsonValueString(StrProp->GetPropertyValue(ValuePtr)));
	}

	// Name
	if (const FNameProperty* NameProp = CastField<FNameProperty>(Prop))
	{
		return MakeShareable(new FJsonValueString(NameProp->GetPropertyValue(ValuePtr).ToString()));
	}

	// Text
	if (const FTextProperty* TextProp = CastField<FTextProperty>(Prop))
	{
		return MakeShareable(new FJsonValueString(TextProp->GetPropertyValue(ValuePtr).ToString()));
	}

	// Byte / Enum
	if (const FByteProperty* ByteProp = CastField<FByteProperty>(Prop))
	{
		if (ByteProp->Enum)
		{
			uint8 Value = ByteProp->GetPropertyValue(ValuePtr);
			return MakeShareable(new FJsonValueString(ByteProp->Enum->GetNameStringByValue(Value)));
		}
		return MakeShareable(new FJsonValueNumber(ByteProp->GetPropertyValue(ValuePtr)));
	}

	if (const FEnumProperty* EnumProp = CastField<FEnumProperty>(Prop))
	{
		FNumericProperty* UnderlyingProp = EnumProp->GetUnderlyingProperty();
		int64 Value = UnderlyingProp->GetSignedIntPropertyValue(ValuePtr);
		return MakeShareable(new FJsonValueString(EnumProp->GetEnum()->GetNameStringByValue(Value)));
	}

	// Object
	if (const FObjectProperty* ObjProp = CastField<FObjectProperty>(Prop))
	{
		UObject* Obj = ObjProp->GetObjectPropertyValue(ValuePtr);
		if (!Obj)
		{
			return MakeShareable(new FJsonValueString(TEXT("null")));
		}

		// (B) ActorComponent: recurse into the component's UPROPERTYs at
		// Depth+1 so nested state (transform, asset refs, BP-set defaults)
		// is captured. Limited to one level of recursion to avoid cycles
		// through AttachParent/Owner. (A SceneComponent is also an
		// ActorComponent, so this branch covers both.)
		if (UActorComponent* Comp = Cast<UActorComponent>(Obj))
		{
			TSharedPtr<FJsonObject> CompObj = MakeShareable(new FJsonObject);
			CompObj->SetStringField(TEXT("class"), Comp->GetClass()->GetPathName());

			if (Depth < 1)
			{
				static const TSet<FName> SkipPropNames = {
					TEXT("AttachParent"), TEXT("AttachChildren"), TEXT("AttachSocketName"),
					TEXT("Owner"),        TEXT("World"),          TEXT("AttachedToComponent"),
					// BP-internal accounting fields populated by SCS but not by
					// native CreateDefaultSubobject (same intent as the top-level
					// BPOnlyAccountingFields above; nested-component path needs
					// its own copy because SerializeProperty doesn't share that
					// filter when it recurses into FObjectProperty values).
					TEXT("UCSSerializationIndex"),
					TEXT("bNetAddressable"),
					TEXT("CreationMethod"),
					TEXT("bVisualizeComponent"),
					TEXT("StaticMeshImportVersion"),
				};
				for (TFieldIterator<FProperty> It(Comp->GetClass()); It; ++It)
				{
					FProperty* P = *It;
					if (P->HasAnyPropertyFlags(CPF_Transient | CPF_DuplicateTransient))
						continue;
					if (CastField<FMulticastDelegateProperty>(P) || CastField<FDelegateProperty>(P))
						continue;
					if (SkipPropNames.Contains(P->GetFName()))
						continue;

					const void* PV = P->ContainerPtrToValuePtr<void>(Comp);
					TSharedPtr<FJsonValue> V = SerializeProperty(P, PV, Depth + 1);
					if (V.IsValid())
					{
						CompObj->SetField(P->GetName(), V);
					}
				}
				if (USceneComponent* Sc = Cast<USceneComponent>(Comp))
				{
					auto VecToObj = [](const FVector& V)
					{
						TSharedPtr<FJsonObject> O = MakeShareable(new FJsonObject);
						O->SetNumberField(TEXT("X"), V.X);
						O->SetNumberField(TEXT("Y"), V.Y);
						O->SetNumberField(TEXT("Z"), V.Z);
						return O;
					};
					auto RotToObj = [](const FRotator& R)
					{
						TSharedPtr<FJsonObject> O = MakeShareable(new FJsonObject);
						O->SetNumberField(TEXT("Pitch"), R.Pitch);
						O->SetNumberField(TEXT("Yaw"),   R.Yaw);
						O->SetNumberField(TEXT("Roll"),  R.Roll);
						return O;
					};
					CompObj->SetObjectField(TEXT("relativeLocation"), VecToObj(Sc->GetRelativeLocation()));
					CompObj->SetObjectField(TEXT("relativeRotation"), RotToObj(Sc->GetRelativeRotation()));
					CompObj->SetObjectField(TEXT("relativeScale3D"),  VecToObj(Sc->GetRelativeScale3D()));
				}
			}
			return MakeShareable(new FJsonValueObject(CompObj));
		}

		// (C) Asset reference: when the object is an asset, output the
		// asset's full path -- not the class. Distinguishes "BP set
		// SM_Cube vs SM_Sphere" cases that would otherwise share a class.
		if (Obj->IsAsset())
		{
			return MakeShareable(new FJsonValueString(Obj->GetPathName()));
		}

		// Plain object reference: class path string only.
		return MakeShareable(new FJsonValueString(Obj->GetClass()->GetPathName()));
	}

	// Struct
	if (const FStructProperty* StructProp = CastField<FStructProperty>(Prop))
	{
		UScriptStruct* Struct = StructProp->Struct;

		// FGuid -> deterministic placeholder
		if (Struct == TBaseStructure<FGuid>::Get())
		{
			const FGuid& Guid = *static_cast<const FGuid*>(ValuePtr);
			return MakeShareable(new FJsonValueString(GuidToPlaceholder(Guid)));
		}

		// FJsonObjectWrapper -> inline the inner JSON
		static UScriptStruct* JsonWrapperStruct = FindObject<UScriptStruct>(nullptr, TEXT("/Script/JsonUtilities.JsonObjectWrapper"));
		if (Struct == JsonWrapperStruct)
		{
			const FJsonObjectWrapper* Wrapper = static_cast<const FJsonObjectWrapper*>(ValuePtr);
			if (Wrapper->JsonObject.IsValid())
			{
				return MakeShareable(new FJsonValueObject(Wrapper->JsonObject));
			}
			return MakeShareable(new FJsonValueNull());
		}

		// General struct -> recurse
		TSharedPtr<FJsonObject> StructObj = MakeShareable(new FJsonObject);
		for (TFieldIterator<FProperty> It(Struct); It; ++It)
		{
			const void* MemberPtr = It->ContainerPtrToValuePtr<void>(ValuePtr);
			TSharedPtr<FJsonValue> MemberValue = SerializeProperty(*It, MemberPtr);
			if (MemberValue.IsValid())
			{
				StructObj->SetField(It->GetName(), MemberValue);
			}
		}
		return MakeShareable(new FJsonValueObject(StructObj));
	}

	// Array
	if (const FArrayProperty* ArrayProp = CastField<FArrayProperty>(Prop))
	{
		TArray<TSharedPtr<FJsonValue>> JsonArray;
		FScriptArrayHelper ArrayHelper(ArrayProp, ValuePtr);
		for (int32 i = 0; i < ArrayHelper.Num(); ++i)
		{
			TSharedPtr<FJsonValue> ElemValue = SerializeProperty(ArrayProp->Inner, ArrayHelper.GetRawPtr(i));
			JsonArray.Add(ElemValue);
		}
		return MakeShareable(new FJsonValueArray(JsonArray));
	}

	// Map
	if (const FMapProperty* MapProp = CastField<FMapProperty>(Prop))
	{
		TSharedPtr<FJsonObject> MapObj = MakeShareable(new FJsonObject);
		FScriptMapHelper MapHelper(MapProp, ValuePtr);
		TArray<FString> SortedKeys;

		for (int32 i = 0; i < MapHelper.GetMaxIndex(); ++i)
		{
			if (MapHelper.IsValidIndex(i))
			{
				// Convert key to string
				FString KeyStr;
				MapProp->KeyProp->ExportTextItem_Direct(KeyStr, MapHelper.GetKeyPtr(i), nullptr, nullptr, PPF_None);
				SortedKeys.Add(KeyStr);
			}
		}

		SortedKeys.Sort();
		for (const FString& Key : SortedKeys)
		{
			// Look up value by key
			for (int32 i = 0; i < MapHelper.GetMaxIndex(); ++i)
			{
				if (MapHelper.IsValidIndex(i))
				{
					FString ThisKey;
					MapProp->KeyProp->ExportTextItem_Direct(ThisKey, MapHelper.GetKeyPtr(i), nullptr, nullptr, PPF_None);
					if (ThisKey == Key)
					{
						TSharedPtr<FJsonValue> ValJson = SerializeProperty(MapProp->ValueProp, MapHelper.GetValuePtr(i));
						MapObj->SetField(Key, ValJson);
						break;
					}
				}
			}
		}
		return MakeShareable(new FJsonValueObject(MapObj));
	}

	// Set
	if (const FSetProperty* SetProp = CastField<FSetProperty>(Prop))
	{
		TArray<TSharedPtr<FJsonValue>> JsonArray;
		FScriptSetHelper SetHelper(SetProp, ValuePtr);
		for (int32 i = 0; i < SetHelper.GetMaxIndex(); ++i)
		{
			if (SetHelper.IsValidIndex(i))
			{
				TSharedPtr<FJsonValue> ElemValue = SerializeProperty(SetProp->ElementProp, SetHelper.GetElementPtr(i));
				JsonArray.Add(ElemValue);
			}
		}
		// Sort for determinism
		JsonArray.Sort([](const TSharedPtr<FJsonValue>& A, const TSharedPtr<FJsonValue>& B) {
			FString StrA, StrB;
			A->TryGetString(StrA);
			B->TryGetString(StrB);
			return StrA < StrB;
		});
		return MakeShareable(new FJsonValueArray(JsonArray));
	}

	// Fallback: ExportText
	FString ExportedValue;
	Prop->ExportTextItem_Direct(ExportedValue, ValuePtr, nullptr, nullptr, PPF_None);
	return MakeShareable(new FJsonValueString(ExportedValue));
}

// -- FGuid deterministic placeholders --

FString FBPBehaviorSnapshot::GuidToPlaceholder(const FGuid& Guid)
{
	if (!Guid.IsValid())
	{
		return TEXT("GUID_INVALID");
	}

	if (int32* Existing = GuidPlaceholderMap.Find(Guid))
	{
		return FString::Printf(TEXT("GUID_%d"), *Existing);
	}

	int32 Index = GuidPlaceholderCounter++;
	GuidPlaceholderMap.Add(Guid, Index);
	return FString::Printf(TEXT("GUID_%d"), Index);
}

void FBPBehaviorSnapshot::ResetGuidPlaceholders()
{
	GuidPlaceholderMap.Empty();
	GuidPlaceholderCounter = 0;
}

// -- Function invocation --

TSharedPtr<FJsonObject> FBPBehaviorSnapshot::CallFunction(
	UObject* Object,
	const FString& FunctionName,
	const TSharedPtr<FJsonObject>& Params,
	UWorld* WorldContext)
{
	if (!Object)
	{
		UE_LOG(LogTemp, Error, TEXT("[BPSnapshot] CallFunction: Object is null"));
		return nullptr;
	}

	UFunction* Func = Object->GetClass()->FindFunctionByName(FName(*FunctionName));
	if (!Func)
	{
		// BP function names can contain spaces / hyphens (e.g. "Set Sun Direction").
		// C++ migrations sanitize those out (-> "SetSunDirection"). When the trace
		// captures the BP-internal name verbatim and the C++ class uses the
		// sanitized form, look up by the sanitized variant before giving up.
		FString Sanitized = FunctionName;
		Sanitized.ReplaceInline(TEXT(" "), TEXT(""));
		Sanitized.ReplaceInline(TEXT("-"), TEXT(""));
		Sanitized.ReplaceInline(TEXT("\t"), TEXT(""));
		if (Sanitized != FunctionName)
		{
			Func = Object->GetClass()->FindFunctionByName(FName(*Sanitized));
		}
	}
	if (!Func)
	{
		UE_LOG(LogTemp, Error, TEXT("[BPSnapshot] Function not found: %s"), *FunctionName);
		return nullptr;
	}

	// Allocate the parameter buffer
	uint8* ParamBuffer = (uint8*)FMemory_Alloca(Func->ParmsSize);
	FMemory::Memzero(ParamBuffer, Func->ParmsSize);

	for (TFieldIterator<FProperty> It(Func); It && (It->PropertyFlags & CPF_Parm); ++It)
	{
		It->InitializeValue_InContainer(ParamBuffer);
	}

	// Look up a JSON field for a C++ param name, deterministically tolerating
	// the four documented BP <-> C++ rename patterns:
	//
	//   BP param        -> C++ param
	//   ----------------------------------------
	//   "azimuth"          "Azimuth"     (BP lowercase, C++ PascalCase)
	//   "Foo"              "Foo"         (already matching)
	//   "azimuth"          "InAzimuth"   (C++ added In-prefix to avoid
	//                                     member shadowing -- UHT rule)
	//   "Set Sun Direction" parameters of a function whose own name was
	//                       sanitized -- params themselves are unaffected
	//                       and fall back to one of the rules above.
	//
	// Order: exact -> case-insensitive -> In-prefix strip -> lowercase-first
	// of stripped. Every fallback is a documented pattern; new patterns
	// require an explicit entry here, never silent magic.
	auto FindParamField = [&Params](const FString& CppName) -> TSharedPtr<FJsonValue>
	{
		if (!Params.IsValid()) return nullptr;
		if (Params->HasField(CppName))
		{
			return Params->TryGetField(CppName);
		}
		// Case-insensitive scan
		for (const auto& Pair : Params->Values)
		{
			if (Pair.Key.Equals(CppName, ESearchCase::IgnoreCase))
			{
				return Pair.Value;
			}
		}
		// In-prefix strip (UHT member-shadow workaround)
		if (CppName.StartsWith(TEXT("In")) && CppName.Len() > 2 && FChar::IsUpper(CppName[2]))
		{
			FString Stripped = CppName.RightChop(2);
			if (Params->HasField(Stripped))
			{
				return Params->TryGetField(Stripped);
			}
			for (const auto& Pair : Params->Values)
			{
				if (Pair.Key.Equals(Stripped, ESearchCase::IgnoreCase))
				{
					return Pair.Value;
				}
			}
		}
		return nullptr;
	};

	// Set input parameters
	for (TFieldIterator<FProperty> It(Func); It && (It->PropertyFlags & CPF_Parm); ++It)
	{
		if (It->PropertyFlags & CPF_OutParm)
		{
			continue; // Skip outputs
		}

		FString ParamName = It->GetName();

		// Auto-fill __WorldContext
		if (ParamName == TEXT("__WorldContext") && WorldContext)
		{
			CastField<FObjectProperty>(*It)->SetObjectPropertyValue_InContainer(ParamBuffer, WorldContext);
			continue;
		}

		// Read input value from JSON (with sanitize-fallback lookup)
		TSharedPtr<FJsonValue> Value = FindParamField(ParamName);
		if (Value.IsValid())
		{
			DeserializeProperty(*It, It->ContainerPtrToValuePtr<void>(ParamBuffer), Value);
		}
	}

	// Call
	Object->ProcessEvent(Func, ParamBuffer);

	// Read output parameters
	TSharedPtr<FJsonObject> Outputs = MakeShareable(new FJsonObject);
	bool bHasOutputs = false;

	for (TFieldIterator<FProperty> It(Func); It && (It->PropertyFlags & CPF_Parm); ++It)
	{
		if (It->PropertyFlags & (CPF_OutParm | CPF_ReturnParm))
		{
			const void* OutPtr = It->ContainerPtrToValuePtr<void>(ParamBuffer);
			TSharedPtr<FJsonValue> OutValue = SerializeProperty(*It, OutPtr);
			Outputs->SetField(It->GetName(), OutValue);
			bHasOutputs = true;
		}
	}

	// Cleanup
	for (TFieldIterator<FProperty> It(Func); It && (It->PropertyFlags & CPF_Parm); ++It)
	{
		It->DestroyValue_InContainer(ParamBuffer);
	}

	return bHasOutputs ? Outputs : nullptr;
}

// -- Deserialization (JSON -> FProperty) --

void FBPBehaviorSnapshot::DeserializeProperty(FProperty* Prop, void* ValuePtr, const TSharedPtr<FJsonValue>& JsonValue)
{
	if (!Prop || !ValuePtr || !JsonValue.IsValid())
	{
		return;
	}

	if (FBoolProperty* BoolProp = CastField<FBoolProperty>(Prop))
	{
		bool bVal = false;
		JsonValue->TryGetBool(bVal);
		BoolProp->SetPropertyValue(ValuePtr, bVal);
	}
	else if (FIntProperty* IntProp = CastField<FIntProperty>(Prop))
	{
		int32 Val = 0;
		double DVal = 0;
		if (JsonValue->TryGetNumber(DVal)) Val = (int32)DVal;
		IntProp->SetPropertyValue(ValuePtr, Val);
	}
	else if (FFloatProperty* FloatProp = CastField<FFloatProperty>(Prop))
	{
		double Val = 0;
		JsonValue->TryGetNumber(Val);
		FloatProp->SetPropertyValue(ValuePtr, (float)Val);
	}
	else if (FDoubleProperty* DoubleProp = CastField<FDoubleProperty>(Prop))
	{
		double Val = 0;
		JsonValue->TryGetNumber(Val);
		DoubleProp->SetPropertyValue(ValuePtr, Val);
	}
	else if (FStrProperty* StrProp = CastField<FStrProperty>(Prop))
	{
		FString Val;
		JsonValue->TryGetString(Val);
		StrProp->SetPropertyValue(ValuePtr, Val);
	}
	else if (FNameProperty* NameProp = CastField<FNameProperty>(Prop))
	{
		FString Val;
		JsonValue->TryGetString(Val);
		NameProp->SetPropertyValue(ValuePtr, FName(*Val));
	}
	else if (const FStructProperty* StructProp = CastField<FStructProperty>(Prop))
	{
		// FJsonObjectWrapper special-case
		static UScriptStruct* JsonWrapperStruct = FindObject<UScriptStruct>(nullptr, TEXT("/Script/JsonUtilities.JsonObjectWrapper"));
		if (StructProp->Struct == JsonWrapperStruct)
		{
			FJsonObjectWrapper* Wrapper = static_cast<FJsonObjectWrapper*>(ValuePtr);
			const TSharedPtr<FJsonObject>* ObjVal = nullptr;
			if (JsonValue->TryGetObject(ObjVal) && ObjVal)
			{
				Wrapper->JsonObject = MakeShareable(new FJsonObject(**ObjVal));
			}
			return;
		}

		// General struct: recurse
		const TSharedPtr<FJsonObject>* ObjVal = nullptr;
		if (JsonValue->TryGetObject(ObjVal) && ObjVal)
		{
			for (TFieldIterator<FProperty> It(StructProp->Struct); It; ++It)
			{
				if ((*ObjVal)->HasField(It->GetName()))
				{
					void* MemberPtr = It->ContainerPtrToValuePtr<void>(ValuePtr);
					DeserializeProperty(*It, MemberPtr, (*ObjVal)->TryGetField(It->GetName()));
				}
			}
		}
	}
	else if (const FArrayProperty* ArrayProp = CastField<FArrayProperty>(Prop))
	{
		const TArray<TSharedPtr<FJsonValue>>* ArrayVal = nullptr;
		if (JsonValue->TryGetArray(ArrayVal))
		{
			FScriptArrayHelper ArrayHelper(ArrayProp, ValuePtr);
			ArrayHelper.Resize(ArrayVal->Num());
			for (int32 i = 0; i < ArrayVal->Num(); ++i)
			{
				DeserializeProperty(ArrayProp->Inner, ArrayHelper.GetRawPtr(i), (*ArrayVal)[i]);
			}
		}
	}
}

// -- Scenario execution --

TArray<FBPBehaviorSnapshot::FStepResult> FBPBehaviorSnapshot::RunScenario(
	UObject* Instance,
	const TSharedPtr<FJsonObject>& Scenario,
	UWorld* WorldContext)
{
	TArray<FStepResult> Results;
	ResetGuidPlaceholders();

	if (!Instance || !Scenario.IsValid())
	{
		return Results;
	}

	// Initial property setup (setup.properties)
	const TSharedPtr<FJsonObject>* SetupObj = nullptr;
	if (Scenario->TryGetObjectField(TEXT("setup"), SetupObj))
	{
		const TSharedPtr<FJsonObject>* PropsObj = nullptr;
		if ((*SetupObj)->TryGetObjectField(TEXT("properties"), PropsObj))
		{
			for (const auto& Pair : (*PropsObj)->Values)
			{
				FProperty* Prop = Instance->GetClass()->FindPropertyByName(FName(*Pair.Key));
				if (Prop)
				{
					void* ValPtr = Prop->ContainerPtrToValuePtr<void>(Instance);
					DeserializeProperty(Prop, ValPtr, Pair.Value);
				}
			}
		}
	}

	// Execute steps
	const TArray<TSharedPtr<FJsonValue>>* Steps = nullptr;
	if (!Scenario->TryGetArrayField(TEXT("steps"), Steps))
	{
		return Results;
	}

	for (int32 i = 0; i < Steps->Num(); ++i)
	{
		const TSharedPtr<FJsonObject>* StepObj = nullptr;
		if (!(*Steps)[i]->TryGetObject(StepObj))
		{
			continue;
		}

		FStepResult StepResult;
		StepResult.Name = (*StepObj)->GetStringField(TEXT("name"));
		StepResult.FunctionName = (*StepObj)->GetStringField(TEXT("function"));

		const TSharedPtr<FJsonObject>* ParamsObj = nullptr;
		TSharedPtr<FJsonObject> Params;
		if ((*StepObj)->TryGetObjectField(TEXT("params"), ParamsObj))
		{
			Params = *ParamsObj;
		}

		// Call function
		StepResult.Outputs = CallFunction(Instance, StepResult.FunctionName, Params, WorldContext);

		// State snapshot
		StepResult.StateAfter = SnapshotProperties(Instance);

		Results.Add(StepResult);

		UE_LOG(LogTemp, Display, TEXT("[BPSnapshot] Step %d/%d: %s::%s"),
			i + 1, Steps->Num(), *StepResult.Name, *StepResult.FunctionName);
	}

	return Results;
}

// -- Auto-generated scenarios --

TSharedPtr<FJsonObject> FBPBehaviorSnapshot::AutoGenerateScenario(UClass* Class)
{
	TSharedPtr<FJsonObject> Scenario = MakeShareable(new FJsonObject);
	Scenario->SetStringField(TEXT("schema"), TEXT("scenario_v1"));
	Scenario->SetStringField(TEXT("targetClass"), Class->GetPathName());

	TArray<TSharedPtr<FJsonValue>> Steps;

	for (TFieldIterator<UFunction> FuncIt(Class, EFieldIteratorFlags::ExcludeSuper); FuncIt; ++FuncIt)
	{
		UFunction* Func = *FuncIt;

		if (!Func->HasAnyFunctionFlags(FUNC_BlueprintCallable))
		{
			continue;
		}

		// Skip framework functions
		static const TSet<FString> SkipFunctions = {
			TEXT("BeginPlay"), TEXT("EndPlay"), TEXT("Tick"),
			TEXT("ReceiveBeginPlay"), TEXT("ReceiveTick"), TEXT("ReceiveEndPlay"),
			TEXT("ExecuteUbergraph"),
		};
		FString FuncName = Func->GetName();
		if (SkipFunctions.Contains(FuncName) || FuncName.StartsWith(TEXT("ExecuteUbergraph")))
		{
			continue;
		}

		TSharedPtr<FJsonObject> Step = MakeShareable(new FJsonObject);
		Step->SetStringField(TEXT("name"), FuncName.ToLower());
		Step->SetStringField(TEXT("function"), FuncName);
		Step->SetObjectField(TEXT("params"), MakeShareable(new FJsonObject));
		Steps.Add(MakeShareable(new FJsonValueObject(Step)));
	}

	Scenario->SetArrayField(TEXT("steps"), Steps);
	return Scenario;
}

// -- Comparison --

TArray<FBPBehaviorSnapshot::FDiff> FBPBehaviorSnapshot::CompareSnapshots(
	const TSharedPtr<FJsonObject>& Expected,
	const TSharedPtr<FJsonObject>& Actual,
	const FString& PathPrefix)
{
	TArray<FDiff> Diffs;

	if (!Expected.IsValid() || !Actual.IsValid())
	{
		if (Expected.IsValid() != Actual.IsValid())
		{
			Diffs.Add({ PathPrefix.IsEmpty() ? TEXT("(root)") : PathPrefix,
				Expected.IsValid() ? TEXT("<present>") : TEXT("<null>"),
				Actual.IsValid() ? TEXT("<present>") : TEXT("<null>") });
		}
		return Diffs;
	}

	// Inspect every key in Expected
	for (const auto& Pair : Expected->Values)
	{
		FString FieldPath = PathPrefix.IsEmpty() ? Pair.Key : FString::Printf(TEXT("%s.%s"), *PathPrefix, *Pair.Key);

		if (!Actual->HasField(Pair.Key))
		{
			Diffs.Add({ FieldPath, TEXT("<present>"), TEXT("<missing>") });
			continue;
		}

		const TSharedPtr<FJsonValue>& ExpVal = Pair.Value;
		const TSharedPtr<FJsonValue>& ActVal = Actual->Values[Pair.Key];

		// Type-aware compare
		if (ExpVal->Type != ActVal->Type)
		{
			FString ExpStr, ActStr;
			ExpVal->TryGetString(ExpStr);
			ActVal->TryGetString(ActStr);
			Diffs.Add({ FieldPath, ExpStr, ActStr });
			continue;
		}

		switch (ExpVal->Type)
		{
		case EJson::Object:
		{
			TArray<FDiff> SubDiffs = CompareSnapshots(
				ExpVal->AsObject(), ActVal->AsObject(), FieldPath);
			Diffs.Append(SubDiffs);
			break;
		}
		case EJson::Array:
		{
			const TArray<TSharedPtr<FJsonValue>>& ExpArr = ExpVal->AsArray();
			const TArray<TSharedPtr<FJsonValue>>& ActArr = ActVal->AsArray();
			if (ExpArr.Num() != ActArr.Num())
			{
				Diffs.Add({ FieldPath + TEXT(".length"),
					FString::FromInt(ExpArr.Num()),
					FString::FromInt(ActArr.Num()) });
			}
			int32 CompareCount = FMath::Min(ExpArr.Num(), ActArr.Num());
			for (int32 i = 0; i < CompareCount; ++i)
			{
				FString ElemPath = FString::Printf(TEXT("%s[%d]"), *FieldPath, i);
				if (ExpArr[i]->Type == EJson::Object && ActArr[i]->Type == EJson::Object)
				{
					TArray<FDiff> SubDiffs = CompareSnapshots(
						ExpArr[i]->AsObject(), ActArr[i]->AsObject(), ElemPath);
					Diffs.Append(SubDiffs);
				}
				else
				{
					FString ExpStr, ActStr;
					ExpArr[i]->TryGetString(ExpStr);
					ActArr[i]->TryGetString(ActStr);
					if (ExpStr != ActStr)
					{
						double ExpNum, ActNum;
						if (ExpArr[i]->TryGetNumber(ExpNum) && ActArr[i]->TryGetNumber(ActNum))
						{
							if (!FMath::IsNearlyEqual(ExpNum, ActNum, 0.001))
								Diffs.Add({ ElemPath, FString::SanitizeFloat(ExpNum), FString::SanitizeFloat(ActNum) });
						}
						else
						{
							Diffs.Add({ ElemPath, ExpStr, ActStr });
						}
					}
				}
			}
			break;
		}
		case EJson::Number:
		{
			double ExpNum = ExpVal->AsNumber();
			double ActNum = ActVal->AsNumber();
			if (!FMath::IsNearlyEqual(ExpNum, ActNum, 0.001))
			{
				Diffs.Add({ FieldPath, FString::SanitizeFloat(ExpNum), FString::SanitizeFloat(ActNum) });
			}
			break;
		}
		default:
		{
			FString ExpStr, ActStr;
			ExpVal->TryGetString(ExpStr);
			ActVal->TryGetString(ActStr);
			if (ExpStr != ActStr)
			{
				Diffs.Add({ FieldPath, ExpStr, ActStr });
			}
			break;
		}
		}
	}

	// Keys present only in Actual (missing from Expected)
	for (const auto& Pair : Actual->Values)
	{
		if (!Expected->HasField(Pair.Key))
		{
			FString FieldPath = PathPrefix.IsEmpty() ? Pair.Key : FString::Printf(TEXT("%s.%s"), *PathPrefix, *Pair.Key);
			Diffs.Add({ FieldPath, TEXT("<missing>"), TEXT("<present>") });
		}
	}

	return Diffs;
}

// -- JSON I/O --

bool FBPBehaviorSnapshot::SaveJsonToFile(const TSharedPtr<FJsonObject>& Json, const FString& FilePath)
{
	FString OutputString;
	auto Writer = TJsonWriterFactory<TCHAR, TPrettyJsonPrintPolicy<TCHAR>>::Create(&OutputString);
	FJsonSerializer::Serialize(Json.ToSharedRef(), Writer);

	IFileManager::Get().MakeDirectory(*FPaths::GetPath(FilePath), true);
	return FFileHelper::SaveStringToFile(OutputString, *FilePath, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM);
}

TSharedPtr<FJsonObject> FBPBehaviorSnapshot::LoadJsonFromFile(const FString& FilePath)
{
	FString JsonString;
	if (!FFileHelper::LoadFileToString(JsonString, *FilePath))
	{
		UE_LOG(LogTemp, Error, TEXT("[BPSnapshot] Failed to load: %s"), *FilePath);
		return nullptr;
	}

	TSharedPtr<FJsonObject> JsonObject;
	TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(JsonString);
	if (!FJsonSerializer::Deserialize(Reader, JsonObject))
	{
		UE_LOG(LogTemp, Error, TEXT("[BPSnapshot] Failed to parse JSON: %s"), *FilePath);
		return nullptr;
	}

	return JsonObject;
}
