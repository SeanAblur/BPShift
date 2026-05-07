// SPDX-License-Identifier: MIT
// Copyright (c) 2026 xkdlaldfjtnl

#include "BPGraphWalk.h"

#include "Engine/Blueprint.h"
#include "EdGraph/EdGraph.h"

namespace
{
	void RecurseGraph(
		UEdGraph* G,
		const FString& Label,
		TSet<UEdGraph*>& Visited,
		FBPGraphWalk::FVisitor& Visitor)
	{
		if (!G || Visited.Contains(G)) return;
		Visited.Add(G);
		Visitor(G, Label);
		for (UEdGraph* Sub : G->SubGraphs)
		{
			RecurseGraph(Sub, Label, Visited, Visitor);
		}
	}
}

void FBPGraphWalk::ForEachExecGraph(UBlueprint* BP, FVisitor Visitor)
{
	if (!BP) return;
	TSet<UEdGraph*> Visited;
	for (UEdGraph* G : BP->UbergraphPages)
	{
		RecurseGraph(G, TEXT("EventGraph"), Visited, Visitor);
	}
	for (UEdGraph* G : BP->FunctionGraphs)
	{
		RecurseGraph(G, TEXT("Function"), Visited, Visitor);
	}
	for (UEdGraph* G : BP->MacroGraphs)
	{
		RecurseGraph(G, TEXT("Macro"), Visited, Visitor);
	}
	for (UEdGraph* G : BP->DelegateSignatureGraphs)
	{
		RecurseGraph(G, TEXT("DelegateSignature"), Visited, Visitor);
	}
	for (const FBPInterfaceDescription& Iface : BP->ImplementedInterfaces)
	{
		for (UEdGraph* G : Iface.Graphs)
		{
			RecurseGraph(G, TEXT("Interface"), Visited, Visitor);
		}
	}
}
