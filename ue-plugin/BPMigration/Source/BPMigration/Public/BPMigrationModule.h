// SPDX-License-Identifier: MIT
// Copyright (c) 2026 xkdlaldfjtnl

#pragma once

#include "CoreMinimal.h"
#include "Modules/ModuleManager.h"

class FBPMigrationModule : public IModuleInterface
{
public:
	virtual void StartupModule() override;
	virtual void ShutdownModule() override;
};
