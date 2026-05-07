// SPDX-License-Identifier: MIT
// Copyright (c) 2026 xkdlaldfjtnl

using UnrealBuildTool;

public class BPMigration : ModuleRules
{
	public BPMigration(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;

		PublicDependencyModuleNames.AddRange(new string[]
		{
			"Core",
			"CoreUObject",
			"Engine",
			"Json",
			"JsonUtilities",
		});

		PrivateDependencyModuleNames.AddRange(new string[]
		{
			"UMG",
		});

		// BlueprintGraph + UnrealEd are editor-only; the commandlets are
		// editor commandlets so we only need them in editor builds.
		// AssetRegistry is required by VerifyCallersCommandlet for referencer
		// auto-discovery (FAssetRegistryModule + IAssetRegistry::GetReferencers).
		if (Target.bBuildEditor)
		{
			PrivateDependencyModuleNames.AddRange(new string[]
			{
				"UnrealEd",
				"BlueprintGraph",
				"Kismet",
				"AssetRegistry",
			});
		}
	}
}
