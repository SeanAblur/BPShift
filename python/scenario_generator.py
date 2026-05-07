#!/usr/bin/env python3
"""
scenario_generator.py - Auto-generate test scenarios from bytecode + graph dump

Combines two sources:
- UAssetGUI JSON (bytecode): function characteristics (state change, branching, delegates)
- Commandlet graph dump: precise parameter names/types

Usage:
  python scenario_generator.py <uassetgui_json> --graph <graph_dump_json> [--output scenario.json]
"""

import json
import sys
import os
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@dataclass
class ParamInfo:
    name: str
    pin_type: str  # exec, bool, int, real, string, struct, object, delegate...
    sub_type: str  # JsonObjectWrapper, Guid, etc.
    is_array: bool = False


@dataclass
class FunctionAnalysis:
    name: str
    is_pure: bool = False
    modifies_state: bool = False
    has_branches: bool = False
    broadcasts_delegate: bool = False
    is_ubergraph_entry: bool = False  # Event that jumps into the UberGraph
    param_count: int = 0
    params: list[ParamInfo] = field(default_factory=list)
    bytecode_size: int = 0
    calls_other_funcs: list[str] = field(default_factory=list)
    importance: str = "smoke"


# -- Bytecode analysis --

def analyze_bytecode(bc_ops: list, func_name: str, imports: list) -> FunctionAnalysis:
    analysis = FunctionAnalysis(name=func_name, bytecode_size=len(bc_ops))

    for op in bc_ops:
        ot = _op_type(op)

        if ot == "EX_LetValueOnPersistentFrame":
            analysis.modifies_state = True

        if ot in ("EX_Let", "EX_LetBool", "EX_LetObj",
                  "EX_LetWeakObjPtr", "EX_LetDelegate", "EX_LetMulticastDelegate"):
            # EX_Let uses "Variable" key; EX_LetBool/Obj/etc use "VariableExpression".
            var_expr = op.get("Variable") or op.get("VariableExpression") or {}
            if isinstance(var_expr, dict) and "InstanceVariable" in var_expr.get("$type", ""):
                analysis.modifies_state = True

        if ot in ("EX_JumpIfNot", "EX_ComputedJump"):
            analysis.has_branches = True

        if ot == "EX_CallMulticastDelegate":
            analysis.broadcasts_delegate = True

        if ot in ("EX_FinalFunction", "EX_LocalFinalFunction"):
            sn = op.get("StackNode", 0)
            if isinstance(sn, int) and sn < 0 and abs(sn) <= len(imports):
                analysis.calls_other_funcs.append(imports[abs(sn) - 1].get("ObjectName", "?"))
            elif isinstance(sn, int) and sn > 0:
                # Positive StackNode = local function call (could be UberGraph)
                pass

        if ot in ("EX_VirtualFunction", "EX_LocalVirtualFunction"):
            analysis.calls_other_funcs.append(op.get("VirtualFunctionName", "?"))

    return analysis


def analyze_ubergraph(exports: list, imports: list) -> dict[str, FunctionAnalysis]:
    """Analyze the UberGraph to extract per-event behavior characteristics.

    Returns: {event_name: FunctionAnalysis with ubergraph context}
    """
    ubergraph_analysis = {}

    # Locate the UberGraph FunctionExport
    ubergraph_bc = None
    for exp in exports:
        name = exp.get("ObjectName", "")
        if name.startswith("ExecuteUbergraph") and "FunctionExport" in exp.get("$type", ""):
            ubergraph_bc = exp.get("ScriptBytecode", [])
            break

    if not ubergraph_bc:
        return ubergraph_analysis

    # Analyze the entire UberGraph -- what patterns are present
    full_analysis = analyze_bytecode(ubergraph_bc, "UberGraph", imports)

    # For each event FunctionExport, check if it calls into the UberGraph
    for exp in exports:
        if "FunctionExport" not in exp.get("$type", ""):
            continue
        name = exp.get("ObjectName", "")
        if name.startswith("ExecuteUbergraph"):
            continue

        bc = exp.get("ScriptBytecode", [])
        for op in bc:
            ot = _op_type(op)
            # Pattern: LocalFinalFunction call into the UberGraph
            if ot == "EX_LocalFinalFunction":
                sn = op.get("StackNode", 0)
                if isinstance(sn, int) and sn > 0:
                    # Positive index = export reference
                    target_idx = sn - 1
                    if target_idx < len(exports):
                        target_name = exports[target_idx].get("ObjectName", "")
                        if target_name.startswith("ExecuteUbergraph"):
                            # This event has logic in the UberGraph
                            ubergraph_analysis[name] = FunctionAnalysis(
                                name=name,
                                is_ubergraph_entry=True,
                                modifies_state=full_analysis.modifies_state,
                                has_branches=full_analysis.has_branches,
                                broadcasts_delegate=full_analysis.broadcasts_delegate,
                            )

    return ubergraph_analysis


# -- Parameter extraction from graph dump --

def extract_params_from_graph(graph_data: dict) -> dict[str, list[ParamInfo]]:
    """Extract per-function parameter names/types from the commandlet graph dump."""
    func_params = {}

    for g in graph_data.get("Graphs", []):
        for n in g.get("Nodes", []):
            ntype = n.get("NodeType", n.get("Class", ""))
            title = n.get("Title", "")

            if ntype not in ("CustomEvent", "FunctionEntry", "Event"):
                continue

            # Resolve event/function name
            # Title may look like "RegisterActions\nCustom Event" -> use the part before \n
            func_name = n.get("CustomEventName", "") or n.get("EventName", "")
            if not func_name or func_name == "None":
                title_str = n.get("Title", "")
                if "\n" in title_str:
                    func_name = title_str.split("\n")[0].strip()
                else:
                    func_name = title_str or g.get("Name", "")

            # Extract parameters from Output pins (skip exec)
            params = []
            for p in n.get("Pins", []):
                if p["Direction"] != "Output" or p["Type"] == "exec" or p.get("Hidden", False):
                    continue
                # Skip delegate types (e.g. OutputDelegate)
                if p["Type"] == "delegate":
                    continue
                params.append(ParamInfo(
                    name=p["Name"],
                    pin_type=p["Type"],
                    sub_type=p.get("SubType", ""),
                    is_array=p.get("IsArray", False),
                ))

            if params:
                func_params[func_name] = params
                # Also register under the graph name (FunctionEntry title may differ from graph name)
                graph_name = g.get("Name", "")
                if graph_name and graph_name != func_name:
                    func_params[graph_name] = params

    return func_params


# -- Importance classification --

SKIP_FUNCTIONS = {
    "BeginPlay", "EndPlay", "Tick", "ReceiveBeginPlay", "ReceiveTick",
    "ReceiveEndPlay", "Construct", "PreConstruct", "OnInitialized",
    "UserConstructionScript", "DuplicateActorWithTag",
    "GetDataWithTag", "ReturnDataByRequestWithTag",
    "Take Object Data From Actor", "Take Objects with Tag",
}


def classify_importance(analysis: FunctionAnalysis) -> str:
    if analysis.name in SKIP_FUNCTIONS or analysis.name.startswith("ExecuteUbergraph"):
        return "skip"
    # Skip delegate-signature functions
    if "__DelegateSignature" in analysis.name:
        return "skip"

    if analysis.is_ubergraph_entry:
        return "thorough"
    if analysis.modifies_state and analysis.has_branches:
        return "thorough"
    if analysis.broadcasts_delegate:
        return "thorough"
    if analysis.modifies_state:
        return "thorough"
    if analysis.has_branches:
        return "thorough"
    if analysis.is_pure:
        return "smoke"
    return "smoke"


# -- Test value generation --

def generate_test_value(param: ParamInfo, variant: int = 0) -> object:
    """Generate a test value matching the parameter type. Use variant to vary values."""
    t = param.pin_type
    st = param.sub_type

    if t == "bool":
        return True if variant == 0 else False
    if t == "int":
        return variant + 1
    if t in ("real", "float", "double"):
        return float(variant + 1)
    if t == "string":
        return f"test_value_{variant}" if variant > 0 else "test_value"
    if t == "name":
        return f"TestName_{variant}" if variant > 0 else "TestName"
    if t == "text":
        return f"Test Text {variant}" if variant > 0 else "Test Text"
    if t == "byte":
        return variant

    if t == "struct":
        if st == "JsonObjectWrapper" or "Json" in st:
            if variant == 0:
                return {"objects": [{"id": "obj1", "type": "test"}]}
            elif variant == 1:
                return {"objects": [{"id": "obj2", "type": "test2"}]}
            else:
                return {"objects": []}
        if st == "Guid" or "Guid" in st:
            return f"00000000-0000-0000-0000-00000000000{variant}"
        if "Vector" in st:
            v = float(variant + 1)
            return {"X": v, "Y": v, "Z": v}
        if "Rotator" in st:
            v = float(variant * 90)
            return {"Pitch": v, "Yaw": v, "Roll": 0}
        if "Transform" in st:
            return {"Translation": {"X": 0, "Y": 0, "Z": 0},
                    "Rotation": {"X": 0, "Y": 0, "Z": 0, "W": 1},
                    "Scale3D": {"X": 1, "Y": 1, "Z": 1}}
        # General struct
        return {"test_field": f"value_{variant}"}

    if t == "object":
        return None  # null at runtime

    if param.is_array:
        return []

    return None


# -- Scenario builder --

def build_scenario(
    analyses: list[FunctionAnalysis],
    func_params: dict[str, list[ParamInfo]],
    class_path: str,
) -> dict:
    """Build a scenario in a meaningful order based on dependencies.

    Principles:
    - Call state-producing functions (e.g. RegisterActions) first
    - Call state-dependent functions (e.g. UndoEvent, GetLastAction) afterwards
    - Call cleanup functions (e.g. ClearStack) last
    """
    scenario = {
        "schema": "scenario_v1",
        "targetClass": class_path,
        "setup": {"properties": {}},
        "steps": [],
    }

    thorough = {a.name: a for a in analyses if a.importance == "thorough"}
    smoke = {a.name: a for a in analyses if a.importance == "smoke"}

    # -- Ordering: producers -> consumers -> cleaners --
    # Primary producers: functions that build the main state (e.g. add to a collection)
    # Secondary producers: mutate existing state (e.g. AddToLastAction) -- run after primary
    # Consumers: read or mutate state (no params, state-modifying)
    # Cleaners: ClearStack, ResetUnsavedCounter, etc.

    primary_producers = []   # e.g. RegisterActions (creates main state)
    secondary_producers = [] # e.g. AddToLastAction (mutates existing state)
    consumers = []           # e.g. UndoEvent, RedoEvent, CutStack
    cleaners = []            # ClearStack, ResetUnsavedCounter

    cleaner_names = {"ClearStack", "ResetUnsavedCounter", "Debug"}
    # Secondary producers: name matches Add/Append/Modify/Update and mutates existing state
    secondary_patterns = {"AddTo", "Append", "Modify", "Update", "Set"}

    for name, analysis in thorough.items():
        if name in cleaner_names:
            cleaners.append(analysis)
        elif any(name.startswith(p) or p in name for p in secondary_patterns):
            secondary_producers.append(analysis)
        elif analysis.param_count > 0:
            primary_producers.append(analysis)
        else:
            consumers.append(analysis)

    # producers = primary first, secondary after
    producers = primary_producers + secondary_producers

    def _make_step(analysis: FunctionAnalysis, variant: int, suffix: str, level: str) -> dict:
        params_list = func_params.get(analysis.name, [])
        params = {}
        for p in params_list:
            val = generate_test_value(p, variant=variant)
            if val is not None:
                params[p.name] = val
        return {
            "name": f"{_safe_name(analysis.name)}_{suffix}",
            "function": analysis.name,
            "params": params,
            "testLevel": level,
        }

    # -- Phase 1: build state (call producers) --
    for analysis in producers:
        scenario["steps"].append(_make_step(analysis, 0, "1", "thorough"))
        if analysis.modifies_state:
            scenario["steps"].append(_make_step(analysis, 1, "2", "thorough"))

    # -- Phase 2: smoke tests (state-confirming getters) --
    # Verify the state produced by producers via getters
    for name, analysis in smoke.items():
        scenario["steps"].append(_make_step(analysis, 0, "after_setup", "smoke"))

    # -- Phase 3: call consumers (state-dependent functions) --
    for analysis in consumers:
        scenario["steps"].append(_make_step(analysis, 0, "1", "thorough"))
        if analysis.modifies_state:
            scenario["steps"].append(_make_step(analysis, 1, "2", "thorough"))

    # -- Phase 4: smoke after consumers (verify state mutation) --
    for name, analysis in smoke.items():
        scenario["steps"].append(_make_step(analysis, 0, "after_consume", "smoke"))

    # -- Phase 5: edge-case tests --
    # Rebuild state with producers, then exercise edge values
    for analysis in producers:
        if analysis.has_branches:
            scenario["steps"].append(_make_step(analysis, 2, "edge", "thorough_edge"))
    for analysis in consumers:
        if analysis.has_branches:
            scenario["steps"].append(_make_step(analysis, 2, "edge", "thorough_edge"))

    # -- Phase 6: cleanup functions --
    for analysis in cleaners:
        scenario["steps"].append(_make_step(analysis, 0, "cleanup", "thorough"))

    # -- Phase 7: smoke after cleanup (confirm initial state is restored) --
    for name, analysis in smoke.items():
        scenario["steps"].append(_make_step(analysis, 0, "after_cleanup", "smoke"))

    return scenario


# -- Utilities --

def _op_type(op: dict) -> str:
    return op.get("$type", "").split(".")[-1].replace(", UAssetAPI", "")


def _safe_name(name: str) -> str:
    return name.lower().replace(" ", "_")


# -- Main --

def run(json_path: str, graph: str | None = None, output: str | None = None) -> int:
    """Generate test scenarios from a UAssetGUI JSON dump (+ optional graph dump).

    Importable entry point so `bpmigrate scenario` can call directly without
    a subprocess re-exec. Returns 0 on success.
    """
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    imports = data.get("Imports", [])
    exports = data.get("Exports", [])
    class_path = data.get("FolderName", "Unknown")

    func_params: dict[str, list[ParamInfo]] = {}
    if graph:
        with open(graph, encoding="utf-8") as f:
            graph_data = json.load(f)
        func_params = extract_params_from_graph(graph_data)

    ubergraph_map = analyze_ubergraph(exports, imports)

    analyses = []
    for exp in exports:
        if "FunctionExport" not in exp.get("$type", ""):
            continue
        name = exp.get("ObjectName", "")
        flags = exp.get("FunctionFlags", "")
        bc = exp.get("ScriptBytecode", [])
        analysis = analyze_bytecode(bc, name, imports)
        analysis.is_pure = "FUNC_BlueprintPure" in flags
        if name in ubergraph_map:
            ug = ubergraph_map[name]
            analysis.is_ubergraph_entry = True
            analysis.modifies_state = analysis.modifies_state or ug.modifies_state
            analysis.has_branches = analysis.has_branches or ug.has_branches
            analysis.broadcasts_delegate = analysis.broadcasts_delegate or ug.broadcasts_delegate
        if name in func_params:
            analysis.params = func_params[name]
            analysis.param_count = len(func_params[name])
        analysis.importance = classify_importance(analysis)
        if analysis.importance != "skip":
            analyses.append(analysis)

    print("=== Function Analysis ===", file=sys.stderr)
    for a in analyses:
        flags = []
        if a.modifies_state:        flags.append("STATE")
        if a.has_branches:          flags.append("BRANCH")
        if a.broadcasts_delegate:   flags.append("BROADCAST")
        if a.is_pure:               flags.append("PURE")
        if a.is_ubergraph_entry:    flags.append("UBERGRAPH")
        flags_str = f" [{', '.join(flags)}]" if flags else ""
        params_str = ", ".join(f"{p.name}:{p.pin_type}" for p in a.params) if a.params else ""
        print(f"  [{a.importance:8s}] {a.name}({params_str}){flags_str}", file=sys.stderr)

    scenario = build_scenario(analyses, func_params, class_path)
    thorough_count = sum(1 for s in scenario["steps"] if s["testLevel"].startswith("thorough"))
    smoke_count = sum(1 for s in scenario["steps"] if s["testLevel"] == "smoke")
    print(f"\nGenerated {len(scenario['steps'])} steps ({thorough_count} thorough, {smoke_count} smoke)",
          file=sys.stderr)

    out_text = json.dumps(scenario, indent=2, ensure_ascii=False)
    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(out_text)
        print(f"Written to {output}", file=sys.stderr)
    else:
        print(out_text)
    return 0


def main():
    """Standalone CLI entry. `bpmigrate scenario` imports `run` directly."""
    import argparse
    parser = argparse.ArgumentParser(
        description="Auto-generate test scenarios from bytecode + graph dump"
    )
    parser.add_argument("json_path", help="Path to UAssetGUI JSON")
    parser.add_argument("--graph", help="Path to commandlet graph dump JSON (precise parameter names)")
    parser.add_argument("--output", "-o", help="Output path (default: stdout)")
    args = parser.parse_args()
    return run(args.json_path, args.graph, args.output)


if __name__ == "__main__":
    sys.exit(main() or 0)
