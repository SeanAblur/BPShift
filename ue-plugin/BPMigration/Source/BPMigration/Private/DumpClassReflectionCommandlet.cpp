// SPDX-License-Identifier: MIT
// Copyright (c) 2026 xkdlaldfjtnl

#include "DumpClassReflectionCommandlet.h"

#if WITH_EDITOR

#include "Engine/Blueprint.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "HAL/PlatformProcess.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "UObject/UnrealType.h"
#include "UObject/Class.h"
#include "UObject/Package.h"

namespace
{
	FString PropertyTypeString(FProperty* P)
	{
		if (!P) return TEXT("?");
		// FProperty's class name (FBoolProperty / FObjectProperty / ...).
		// Caller can use this to reason about kind without UE link.
		return P->GetClass()->GetName();
	}

	FString PropertyExtraTarget(FProperty* P)
	{
		// For object/class/struct/enum: the resolved target name (e.g. "Actor",
		// "Vector", "EMyEnum"). Empty otherwise. Surfacing this lets the
		// classifier say "type matches" without a separate reflection trip.
		if (!P) return TEXT("");
		if (auto* Obj = CastField<FObjectProperty>(P))
		{
			return Obj->PropertyClass ? Obj->PropertyClass->GetName() : TEXT("");
		}
		if (auto* Cls = CastField<FClassProperty>(P))
		{
			return Cls->MetaClass ? Cls->MetaClass->GetName() : TEXT("");
		}
		if (auto* Str = CastField<FStructProperty>(P))
		{
			return Str->Struct ? Str->Struct->GetName() : TEXT("");
		}
		if (auto* En = CastField<FEnumProperty>(P))
		{
			return En->GetEnum() ? En->GetEnum()->GetName() : TEXT("");
		}
		if (auto* Byte = CastField<FByteProperty>(P))
		{
			return Byte->Enum ? Byte->Enum->GetName() : TEXT("");
		}
		return TEXT("");
	}
}

int32 UDumpClassReflectionCommandlet::Main(const FString& Params)
{
	TArray<FString> Tokens;
	TArray<FString> Switches;
	TMap<FString, FString> ParamsMap;
	UCommandlet::ParseCommandLine(*Params, Tokens, Switches, ParamsMap);

	if (Tokens.Num() < 1)
	{
		UE_LOG(LogTemp, Error, TEXT("Usage: -run=DumpClassReflection <ClassPath> [-output=path.json]"));
		UE_LOG(LogTemp, Error, TEXT("       ClassPath examples: /Script/Engine.Actor, /Game/Path/BP_Foo"));
		return 1;
	}

	const FString ClassPath = Tokens[0];

	// Resolve the class. /Script/... -> direct LoadClass. /Game/... -> Blueprint then GeneratedClass.
	UClass* TargetClass = nullptr;
	if (ClassPath.StartsWith(TEXT("/Script/")))
	{
		TargetClass = LoadClass<UObject>(nullptr, *ClassPath);
	}
	else
	{
		// Treat as a Blueprint asset path.
		if (UBlueprint* BP = LoadObject<UBlueprint>(nullptr, *ClassPath))
		{
			TargetClass = BP->SkeletonGeneratedClass
				? BP->SkeletonGeneratedClass.Get()
				: BP->GeneratedClass.Get();
		}
	}

	if (!TargetClass)
	{
		UE_LOG(LogTemp, Error, TEXT("Failed to resolve class: %s"), *ClassPath);
		return 1;
	}

	UE_LOG(LogTemp, Display, TEXT("Resolved class: %s (Parent: %s)"),
		*TargetClass->GetName(),
		TargetClass->GetSuperClass() ? *TargetClass->GetSuperClass()->GetName() : TEXT("None"));

	TSharedPtr<FJsonObject> Root = MakeShareable(new FJsonObject);
	Root->SetStringField(TEXT("schema"), TEXT("class_reflection_v1"));
	Root->SetStringField(TEXT("classPath"), ClassPath);
	Root->SetStringField(TEXT("className"), TargetClass->GetName());
	Root->SetStringField(TEXT("parentClass"),
		TargetClass->GetSuperClass() ? TargetClass->GetSuperClass()->GetName() : TEXT(""));

	// Functions (own + inherited) — every UFunction visible on the class.
	TArray<TSharedPtr<FJsonValue>> FuncsArr;
	for (TFieldIterator<UFunction> It(TargetClass); It; ++It)
	{
		UFunction* F = *It;
		if (!F) continue;

		TSharedPtr<FJsonObject> FObj = MakeShareable(new FJsonObject);
		FObj->SetStringField(TEXT("name"), F->GetName());
		FObj->SetStringField(TEXT("ownerClass"),
			F->GetOwnerClass() ? F->GetOwnerClass()->GetName() : TEXT(""));

		// Parameters (in declaration order). Return parameter is included
		// with isReturn=true so the classifier sees full signature shape.
		TArray<TSharedPtr<FJsonValue>> ParamsArr;
		for (TFieldIterator<FProperty> PIt(F); PIt && (PIt->PropertyFlags & CPF_Parm); ++PIt)
		{
			FProperty* P = *PIt;
			TSharedPtr<FJsonObject> PObj = MakeShareable(new FJsonObject);
			PObj->SetStringField(TEXT("name"), P->GetName());
			PObj->SetStringField(TEXT("type"), PropertyTypeString(P));
			const FString ExtraTarget = PropertyExtraTarget(P);
			if (!ExtraTarget.IsEmpty())
			{
				PObj->SetStringField(TEXT("targetType"), ExtraTarget);
			}
			PObj->SetBoolField(TEXT("isReturn"),
				(P->PropertyFlags & CPF_ReturnParm) != 0);
			PObj->SetBoolField(TEXT("isOut"),
				(P->PropertyFlags & CPF_OutParm) != 0
				&& (P->PropertyFlags & CPF_ReturnParm) == 0);
			ParamsArr.Add(MakeShareable(new FJsonValueObject(PObj)));
		}
		FObj->SetArrayField(TEXT("params"), ParamsArr);

		// Flags hint (BlueprintCallable / BlueprintImplementableEvent /
		// BlueprintNativeEvent / Pure) for classifier rationale strings.
		FObj->SetBoolField(TEXT("isBlueprintCallable"),
			!!(F->FunctionFlags & FUNC_BlueprintCallable));
		FObj->SetBoolField(TEXT("isBlueprintEvent"),
			!!(F->FunctionFlags & FUNC_BlueprintEvent));
		FObj->SetBoolField(TEXT("isPure"),
			!!(F->FunctionFlags & FUNC_BlueprintPure));
		FObj->SetBoolField(TEXT("isNative"),
			!!(F->FunctionFlags & FUNC_Native));

		FuncsArr.Add(MakeShareable(new FJsonValueObject(FObj)));
	}
	Root->SetArrayField(TEXT("functions"), FuncsArr);

	// Properties (own + inherited).
	TArray<TSharedPtr<FJsonValue>> PropsArr;
	for (TFieldIterator<FProperty> It(TargetClass); It; ++It)
	{
		FProperty* P = *It;
		if (!P) continue;
		TSharedPtr<FJsonObject> PObj = MakeShareable(new FJsonObject);
		PObj->SetStringField(TEXT("name"), P->GetName());
		PObj->SetStringField(TEXT("type"), PropertyTypeString(P));
		const FString ExtraTarget = PropertyExtraTarget(P);
		if (!ExtraTarget.IsEmpty())
		{
			PObj->SetStringField(TEXT("targetType"), ExtraTarget);
		}
		PObj->SetStringField(TEXT("ownerClass"),
			P->GetOwnerClass() ? P->GetOwnerClass()->GetName() : TEXT(""));
		PObj->SetBoolField(TEXT("isBlueprintReadable"),
			!!(P->PropertyFlags & CPF_BlueprintVisible));
		PObj->SetBoolField(TEXT("isBlueprintWritable"),
			(P->PropertyFlags & CPF_BlueprintVisible)
			&& !(P->PropertyFlags & CPF_BlueprintReadOnly));
		PropsArr.Add(MakeShareable(new FJsonValueObject(PObj)));
	}
	Root->SetArrayField(TEXT("properties"), PropsArr);

	FString OutputPath = ParamsMap.FindRef(TEXT("output"));
	if (OutputPath.IsEmpty())
	{
		FString SafeName = TargetClass->GetName();
		OutputPath = FPaths::Combine(FPlatformProcess::UserTempDir(),
			TEXT("migrate-bp"), SafeName + TEXT("_reflection.json"));
	}

	FString OutputString;
	auto Writer = TJsonWriterFactory<TCHAR, TPrettyJsonPrintPolicy<TCHAR>>::Create(&OutputString);
	FJsonSerializer::Serialize(Root.ToSharedRef(), Writer);

	IFileManager::Get().MakeDirectory(*FPaths::GetPath(OutputPath), true);
	if (FFileHelper::SaveStringToFile(OutputString, *OutputPath,
		FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM))
	{
		UE_LOG(LogTemp, Display, TEXT("Written: %s (%d chars, %d functions, %d properties)"),
			*OutputPath, OutputString.Len(), FuncsArr.Num(), PropsArr.Num());
		return 0;
	}

	UE_LOG(LogTemp, Error, TEXT("Failed to write: %s"), *OutputPath);
	return 1;
}

#else  // !WITH_EDITOR

int32 UDumpClassReflectionCommandlet::Main(const FString& Params)
{
	UE_LOG(LogTemp, Error, TEXT("DumpClassReflection requires WITH_EDITOR (editor build only)."));
	return 1;
}

#endif
