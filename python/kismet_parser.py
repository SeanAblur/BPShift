"""
kismet_parser.py - UAssetGUI JSON -> structured Blueprint data

Parses UAssetGUI's tojson output and extracts:
- Parent class, interfaces
- Variables (UPROPERTY declarations)
- Function graphs (entry points, call chains)
- C++ class/function references
- Delegate bindings
- Node graph structure (exec pin flow)
"""

import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BPVariable:
    name: str
    type_name: str
    flags: list[str] = field(default_factory=list)
    default_value: Optional[str] = None
    category: str = ""


@dataclass
class FunctionCall:
    node_index: int
    function_name: str
    target_class: str = ""
    is_pure: bool = False


@dataclass
class BPFunction:
    name: str
    export_index: int
    flags: list[str] = field(default_factory=list)
    parameters: list[dict] = field(default_factory=list)
    calls: list[FunctionCall] = field(default_factory=list)
    local_variables: list[str] = field(default_factory=list)
    nodes: list[dict] = field(default_factory=list)  # all K2Nodes in this function


@dataclass
class BPEvent:
    name: str
    function: BPFunction
    event_type: str = "Custom"  # Custom, BeginPlay, Tick, etc.
    # ComponentBoundEvent metadata: which widget's delegate this handler binds
    # to. The graph node name (event.name) can hold a stale alias after a widget
    # rename, so the actual binding is identified by the
    # ComponentPropertyName / DelegatePropertyName fields of
    # K2Node_ComponentBoundEvent.
    bound_component: Optional[str] = None
    bound_delegate: Optional[str] = None


@dataclass
class BPMacro:
    name: str
    export_index: int
    is_local: bool = True  # True=defined in this BP, False=engine/external
    source: str = ""  # e.g. "StandardMacros" for engine macros
    calls: list[FunctionCall] = field(default_factory=list)
    nodes: list[dict] = field(default_factory=list)


@dataclass
class BlueprintData:
    asset_name: str = ""
    parent_class: str = ""
    asset_type: str = "Blueprint"  # Blueprint, FunctionLibrary, Interface, etc.
    interfaces: list[str] = field(default_factory=list)
    variables: list[BPVariable] = field(default_factory=list)
    functions: list[BPFunction] = field(default_factory=list)
    events: list[BPEvent] = field(default_factory=list)
    macros: list[BPMacro] = field(default_factory=list)
    cpp_class_refs: list[str] = field(default_factory=list)
    cpp_function_refs: list[str] = field(default_factory=list)
    delegate_bindings: list[dict] = field(default_factory=list)
    raw_imports: list[dict] = field(default_factory=list)


class KismetParser:
    def __init__(self, json_path: str):
        with open(json_path, encoding="utf-8") as f:
            self.data = json.load(f)
        self.exports = self.data.get("Exports", [])
        self.imports = self.data.get("Imports", [])
        self.name_map = self.data.get("NameMap", [])
        self._build_index()

    def _build_index(self):
        """Build lookup tables for exports and imports."""
        self.export_by_index = {}  # 1-based positive index -> export
        self.import_by_index = {}  # 1-based negative index -> import
        for i, exp in enumerate(self.exports):
            self.export_by_index[i + 1] = exp
        for i, imp in enumerate(self.imports):
            self.import_by_index[-(i + 1)] = imp

    def _resolve_index(self, idx: int) -> Optional[dict]:
        """Resolve a package index (positive=export, negative=import, 0=null)."""
        if idx == 0:
            return None
        if idx > 0:
            return self.export_by_index.get(idx)
        return self.import_by_index.get(idx)

    def _get_object_name(self, idx: int) -> str:
        obj = self._resolve_index(idx)
        if obj:
            return obj.get("ObjectName", "Unknown")
        return "None"

    def parse(self) -> BlueprintData:
        bp = BlueprintData()
        bp.asset_name = self._parse_asset_name()
        bp.parent_class, bp.asset_type = self._parse_parent_class()
        bp.interfaces = self._parse_interfaces()
        bp.variables = self._parse_variables()
        bp.raw_imports = self._parse_imports()
        bp.cpp_class_refs = self._extract_cpp_class_refs()
        bp.cpp_function_refs = self._extract_cpp_function_refs()

        # Build function/event graph mapping
        func_exports, node_to_func = self._map_nodes_to_functions()
        bp.functions, bp.events = self._parse_functions(func_exports, node_to_func)
        bp.delegate_bindings = self._parse_delegate_bindings(node_to_func)
        bp.macros = self._parse_macros()

        return bp

    def _parse_asset_name(self) -> str:
        folder = self.data.get("FolderName", "")
        if folder:
            return folder.rsplit("/", 1)[-1]
        # fallback: find BlueprintGeneratedClass export
        for exp in self.exports:
            if "ClassExport" in exp.get("$type", ""):
                return exp.get("ObjectName", "").replace("_C", "")
        return "Unknown"

    def _parse_parent_class(self) -> tuple[str, str]:
        """Returns (parent_class_name, asset_type).

        Checks ClassExport first, then falls back to RawExport/NormalExport
        with _C suffix (BlueprintGeneratedClass pattern).
        """
        # Try ClassExport first
        for exp in self.exports:
            if "ClassExport" in exp.get("$type", ""):
                super_idx = exp.get("SuperIndex", 0)
                parent = self._get_object_name(super_idx)
                return parent, self._classify_asset_type(parent)

        # Fallback: find _C export (BlueprintGeneratedClass)
        for exp in self.exports:
            obj_name = exp.get("ObjectName", "")
            if obj_name.endswith("_C") and not obj_name.startswith("Default__"):
                super_idx = exp.get("SuperIndex", 0)
                parent = self._get_object_name(super_idx)
                return parent, self._classify_asset_type(parent)

        return "Unknown", "Blueprint"

    def _classify_asset_type(self, parent: str) -> str:
        if parent == "BlueprintFunctionLibrary":
            return "FunctionLibrary"
        elif "Interface" in parent:
            return "Interface"
        elif parent == "Actor":
            return "ActorBlueprint"
        elif "GameInstance" in parent:
            return "GameInstanceBlueprint"
        elif "Character" in parent or "Pawn" in parent:
            return "CharacterBlueprint"
        elif "Component" in parent:
            return "ComponentBlueprint"
        return "Blueprint"

    def _parse_interfaces(self) -> list[str]:
        """Extract implemented interfaces from ClassExport."""
        interfaces = []
        for exp in self.exports:
            if "ClassExport" in exp.get("$type", ""):
                for d in exp.get("Data", []):
                    if d.get("Name") == "ImplementedInterfaces":
                        for iface in d.get("Value", []):
                            for prop in iface.get("Value", []):
                                if prop.get("Name") == "Interface":
                                    idx = prop.get("Value", 0)
                                    interfaces.append(self._get_object_name(idx))
        return interfaces

    def _parse_variables(self) -> list[BPVariable]:
        """Extract BP variables from the default object and class properties."""
        variables = []
        seen = set()

        # Find the Default__ export (CDO - Class Default Object)
        for exp in self.exports:
            obj_name = exp.get("ObjectName", "")
            if obj_name.startswith("Default__"):
                for d in exp.get("Data", []):
                    name = d.get("Name", "")
                    if name in seen or name in (
                        "None", "UberGraphFrame", "DefaultSceneRoot",
                        "bCanEverTick", "PrimaryComponentTick",
                    ):
                        continue
                    seen.add(name)

                    var = BPVariable(
                        name=name,
                        type_name=self._infer_type_from_property(d),
                        default_value=self._extract_default_value(d),
                    )
                    variables.append(var)

        # Also extract from ClassExport's loaded properties for type info
        for exp in self.exports:
            if "ClassExport" in exp.get("$type", ""):
                for child_idx in exp.get("ChildProperties", []):
                    child = self._resolve_index(child_idx)
                    if child:
                        name = child.get("ObjectName", "")
                        flags = self._parse_property_flags(
                            child.get("PropertyFlags", "")
                        )
                        # Update existing variable with flags
                        for var in variables:
                            if var.name == name:
                                var.flags = flags
                                break

        return variables

    def _infer_type_from_property(self, prop: dict) -> str:
        """Infer UE type from property data."""
        ptype = prop.get("$type", "")
        if "BoolProperty" in ptype:
            return "bool"
        elif "IntProperty" in ptype:
            return "int32"
        elif "FloatProperty" in ptype:
            return "float"
        elif "StrProperty" in ptype:
            return "FString"
        elif "NameProperty" in ptype:
            return "FName"
        elif "TextProperty" in ptype:
            return "FText"
        elif "ObjectProperty" in ptype:
            return "UObject*"
        elif "SoftObjectProperty" in ptype:
            return "TSoftObjectPtr"
        elif "ArrayProperty" in ptype:
            return "TArray"
        elif "MapProperty" in ptype:
            return "TMap"
        elif "SetProperty" in ptype:
            return "TSet"
        elif "StructProperty" in ptype:
            stype = prop.get("StructType", "")
            return f"F{stype}" if stype else "FStruct"
        elif "EnumProperty" in ptype:
            return prop.get("EnumType", "EEnum")
        elif "ByteProperty" in ptype:
            return "uint8"
        return "Unknown"

    def _extract_default_value(self, prop: dict) -> Optional[str]:
        """Extract default value from property data."""
        val = prop.get("Value")
        if val is None:
            return None
        if isinstance(val, (bool, int, float, str)):
            return str(val)
        if isinstance(val, list) and len(val) == 0:
            return "[]"
        return None  # complex value, skip

    def _parse_property_flags(self, flags_str: str) -> list[str]:
        """Parse CPF_ flags into readable list."""
        if not flags_str or flags_str == "CPF_None":
            return []
        readable = {
            "CPF_BlueprintVisible": "BlueprintVisible",
            "CPF_BlueprintReadOnly": "BlueprintReadOnly",
            "CPF_Edit": "EditAnywhere",
            "CPF_EditConst": "EditConst",
            "CPF_DisableEditOnInstance": "EditDefaultsOnly",
            "CPF_DisableEditOnTemplate": "EditInstanceOnly",
            "CPF_Net": "Replicated",
            "CPF_SaveGame": "SaveGame",
            "CPF_BlueprintCallable": "BlueprintCallable",
            "CPF_ExposeOnSpawn": "ExposeOnSpawn",
            "CPF_Parm": "Param",
            "CPF_OutParm": "OutParam",
            "CPF_ReturnParm": "ReturnParam",
        }
        result = []
        for flag in flags_str.split(", "):
            flag = flag.strip()
            if flag in readable:
                result.append(readable[flag])
        return result

    def _parse_imports(self) -> list[dict]:
        """Parse all imports for reference tracking."""
        result = []
        for imp in self.imports:
            result.append({
                "class": imp.get("ClassName", ""),
                "name": imp.get("ObjectName", ""),
                "outer": imp.get("OuterIndex", 0),
            })
        return result

    def _extract_cpp_class_refs(self) -> list[str]:
        """Extract C++ class references from imports."""
        classes = set()
        skip = {
            "EdGraphSchema_K2", "EdGraph", "MetaData", "Object",
            "Blueprint", "BlueprintGeneratedClass", "Package",
        }
        # K2Node classes to skip
        k2_prefixes = ("K2Node_",)

        for imp in self.imports:
            if imp.get("ClassName") == "Class":
                name = imp.get("ObjectName", "")
                if name not in skip and not any(
                    name.startswith(p) for p in k2_prefixes
                ):
                    classes.add(name)
        return sorted(classes)

    def _extract_cpp_function_refs(self) -> list[str]:
        """Extract C++ function references from imports."""
        functions = []
        for imp in self.imports:
            if imp.get("ClassName") == "Function":
                name = imp.get("ObjectName", "")
                # Find parent class
                outer_idx = imp.get("OuterIndex", 0)
                parent = self._get_object_name(outer_idx)
                functions.append(f"{parent}::{name}")
        return sorted(functions)

    def _get_graph_names(self, array_name: str) -> set[str]:
        """Get export names referenced by a named graph array (FunctionGraphs, MacroGraphs, etc.)."""
        names = set()
        for exp in self.exports:
            if not isinstance(exp, dict) or "Data" not in exp:
                continue
            for d in exp.get("Data", []):
                if not isinstance(d, dict) or d.get("Name") != array_name:
                    continue
                for entry in d.get("Value", []):
                    if isinstance(entry, dict) and "Value" in entry:
                        idx = entry["Value"]
                        if 0 <= idx < len(self.exports):
                            names.add(self.exports[idx].get("ObjectName", ""))
        return names

    def _map_nodes_to_functions(self) -> tuple[dict, dict]:
        """Map K2Node exports to their parent function.

        Node OuterIndex chain: K2Node -> EdGraph(NormalExport) -> BP default.
        EdGraph and FunctionExport share the same name but are separate exports.
        We match by name: EdGraph.ObjectName == FunctionExport.ObjectName.

        Returns:
            func_exports: {export_index: FunctionExport}
            node_to_func: {node_export_index: func_export_index}
        """
        func_exports = {}
        node_to_func = {}

        # Build FunctionExport lookup by name
        func_by_name: dict[str, int] = {}
        for i, exp in enumerate(self.exports):
            etype = exp.get("$type", "")
            if "FunctionExport" in etype:
                func_exports[i] = exp
                func_by_name[exp.get("ObjectName", "")] = i

        # Map nodes: K2Node -> EdGraph (via OuterIndex) -> match by name to FunctionExport
        for i, exp in enumerate(self.exports):
            obj_name = exp.get("ObjectName", "")
            if "K2Node" in obj_name:
                outer = exp.get("OuterIndex", 0)
                if outer > 0:
                    graph_idx = outer - 1
                    if graph_idx < len(self.exports):
                        graph_name = self.exports[graph_idx].get("ObjectName", "")
                        if graph_name in func_by_name:
                            node_to_func[i] = func_by_name[graph_name]

        return func_exports, node_to_func

    def _parse_functions(
        self, func_exports: dict, node_to_func: dict
    ) -> tuple[list[BPFunction], list[BPEvent]]:
        """Parse function and event graphs.

        Classification uses the BP's FunctionGraphs array as ground truth:
        - Names in FunctionGraphs = user-defined functions (even if flagged BlueprintEvent)
        - Names NOT in FunctionGraphs with BlueprintEvent flag = events
        - UbergraphFunction = internal, skip from user-facing output
        This avoids misclassifying BP-defined functions as events, since UE marks
        all BP-defined callables with FUNC_BlueprintEvent.
        """
        functions = []
        events = []

        # Build the ComponentBoundEvent metadata map in one scan (consulted when classifying BPEvents)
        bound_event_map = self._build_component_bound_event_map()

        # Get authoritative function/macro graph names from BP main export
        function_graph_names = self._get_graph_names("FunctionGraphs")
        macro_graph_names = self._get_graph_names("MacroGraphs")

        # Names that are definitely user-defined functions (from FunctionGraphs)
        # Exclude: EventGraph, UberGraph, EdGraphNode_Comment, macro graphs
        skip_names = {"EventGraph", ""}
        func_names = set()
        for gname in function_graph_names:
            if (gname not in skip_names
                    and not gname.startswith("EdGraphNode_Comment")
                    and gname not in macro_graph_names):
                func_names.add(gname)

        builtin_events = {
            "ReceiveBeginPlay": "BeginPlay",
            "ReceiveTick": "Tick",
            "ReceiveEndPlay": "EndPlay",
            "ReceiveDestroyed": "Destroyed",
        }

        for idx, exp in func_exports.items():
            name = exp.get("ObjectName", "")
            flags_str = exp.get("FunctionFlags", "")
            flags = [f.strip() for f in flags_str.split(",") if f.strip()]

            # Skip UberGraph internal function
            if "FUNC_UbergraphFunction" in flags_str:
                # Still include it so bytecode is available, but mark it
                func = BPFunction(
                    name=name, export_index=idx, flags=flags,
                )
                functions.append(func)
                continue

            func = BPFunction(
                name=name,
                export_index=idx,
                flags=flags,
            )

            # Parse parameters from LoadedProperties
            for prop in exp.get("LoadedProperties", []):
                pflags = prop.get("PropertyFlags", "")
                if "CPF_Parm" in pflags:
                    param = {
                        "name": prop.get("SerializedType", ""),
                        "type": prop.get("$type", "").split(".")[-1].replace(
                            ", UAssetAPI", ""
                        ),
                        "flags": pflags,
                    }
                    func.parameters.append(param)

            # Collect calls from K2Nodes belonging to this function
            for node_idx, func_idx in node_to_func.items():
                if func_idx != idx:
                    continue
                node = self.exports[node_idx]
                node_name = node.get("ObjectName", "")
                func.nodes.append({"index": node_idx, "name": node_name})

                if "K2Node_CallFunction" in node_name:
                    call = self._parse_call_function_node(node, node_idx)
                    if call:
                        func.calls.append(call)

            # Classify: function vs event
            # Priority: FunctionGraphs array > flag-based heuristic
            if name in func_names:
                # Listed in FunctionGraphs = user-defined function
                functions.append(func)
            elif name in builtin_events:
                comp, delegate = bound_event_map.get(name, (None, None))
                events.append(BPEvent(
                    name=name, function=func,
                    event_type=builtin_events[name],
                    bound_component=comp, bound_delegate=delegate,
                ))
            elif "FUNC_BlueprintEvent" in flags_str and not any(
                f in flags_str for f in ("FUNC_Static",)
            ):
                comp, delegate = bound_event_map.get(name, (None, None))
                events.append(BPEvent(
                    name=name, function=func, event_type="Custom",
                    bound_component=comp, bound_delegate=delegate,
                ))
            else:
                functions.append(func)

        return functions, events

    def _parse_call_function_node(
        self, node: dict, node_idx: int
    ) -> Optional[FunctionCall]:
        """Extract function call info from a K2Node_CallFunction."""
        func_name = ""
        target_class = ""
        is_pure = False

        for d in node.get("Data", []):
            dname = d.get("Name", "")
            if dname == "bIsPureFunc":
                is_pure = d.get("Value", False)
            elif dname == "FunctionReference":
                for v in d.get("Value", []):
                    vname = v.get("Name", "")
                    if vname == "MemberName":
                        func_name = v.get("Value", "")
                    elif vname == "MemberParent":
                        idx = v.get("Value", 0)
                        target_class = self._get_object_name(idx)

        if func_name:
            return FunctionCall(
                node_index=node_idx,
                function_name=func_name,
                target_class=target_class,
                is_pure=is_pure,
            )
        return None

    def _build_component_bound_event_map(
        self,
    ) -> dict[str, tuple[Optional[str], Optional[str]]]:
        """Map every K2Node_ComponentBoundEvent node to its handler-function metadata.

        Returns: {CustomFunctionName: (ComponentPropertyName, DelegatePropertyName)}.

        ComponentBoundEvent nodes live in the EventGraph (Ubergraph), so they
        are not mapped to a function's `nodes` list directly. The node's
        CustomFunctionName matches the handler function name, so we key the
        map by that. The graph node name can hold a stale alias after a widget
        rename, so ComponentPropertyName must be read directly to be accurate.
        """
        result: dict[str, tuple[Optional[str], Optional[str]]] = {}
        for exp in self.exports:
            name = exp.get("ObjectName", "")
            if "K2Node_ComponentBoundEvent" not in name:
                continue
            comp: Optional[str] = None
            delegate: Optional[str] = None
            custom_fn: Optional[str] = None
            for d in exp.get("Data", []):
                dn = d.get("Name", "")
                if dn == "ComponentPropertyName":
                    comp = d.get("Value")
                elif dn == "DelegatePropertyName":
                    delegate = d.get("Value")
                elif dn == "CustomFunctionName":
                    custom_fn = d.get("Value")
            if custom_fn and (comp or delegate):
                result[custom_fn] = (comp, delegate)
        return result

    def _parse_delegate_bindings(self, node_to_func: dict) -> list[dict]:
        """Extract delegate binding patterns from nodes."""
        bindings = []
        for node_idx, func_idx in node_to_func.items():
            node = self.exports[node_idx]
            node_name = node.get("ObjectName", "")
            if "CallDelegate" in node_name or "AddDelegate" in node_name:
                for d in node.get("Data", []):
                    if d.get("Name") == "DelegateReference":
                        for v in d.get("Value", []):
                            if v.get("Name") == "MemberName":
                                bindings.append({
                                    "delegate": v.get("Value", ""),
                                    "in_function": self.exports[func_idx].get(
                                        "ObjectName", ""
                                    ),
                                })
        return bindings

    def _parse_macros(self) -> list["BPMacro"]:
        """Extract macro definitions.

        Uses two sources and merges them:
        1. MacroGraphs array from BP main export (authoritative for local macros)
        2. K2Node_MacroInstance references (catches engine/external macros)

        For local macros, we parse their internal K2Nodes the same way as
        functions -- by finding nodes whose OuterIndex points to the macro graph.
        """
        macros = []
        seen = set()

        # Source 1: MacroGraphs array (authoritative for local macros)
        function_graph_names = self._get_graph_names("FunctionGraphs")
        macro_graph_names = self._get_graph_names("MacroGraphs")
        # Build name -> export index mapping for local macro graphs
        macro_name_to_export = {}
        for exp in self.exports:
            if not isinstance(exp, dict) or "Data" not in exp:
                continue
            for d in exp.get("Data", []):
                if not isinstance(d, dict) or d.get("Name") != "MacroGraphs":
                    continue
                for entry in d.get("Value", []):
                    if isinstance(entry, dict) and "Value" in entry:
                        idx = entry["Value"]
                        if 0 <= idx < len(self.exports):
                            ename = self.exports[idx].get("ObjectName", "")
                            macro_name_to_export[ename] = idx

        for name, export_idx in macro_name_to_export.items():
            if name in seen:
                continue
            seen.add(name)

            macro = BPMacro(
                name=name,
                export_index=export_idx,
                is_local=True,
            )

            # Find K2Nodes whose OuterIndex points to this macro graph
            for i, node_exp in enumerate(self.exports):
                node_name = node_exp.get("ObjectName", "")
                if "K2Node" not in node_name:
                    continue
                outer = node_exp.get("OuterIndex", 0)
                if outer - 1 == export_idx:
                    macro.nodes.append({"index": i, "name": node_name})
                    if "CallFunction" in node_name:
                        call = self._parse_call_function_node(node_exp, i)
                        if call:
                            macro.calls.append(call)

            macros.append(macro)

        # Source 2: K2Node_MacroInstance references (for engine/external macros)
        for exp in self.exports:
            if "MacroInstance" not in exp.get("ObjectName", ""):
                continue
            for d in exp.get("Data", []):
                if d.get("Name") != "MacroGraphReference":
                    continue
                for v in d.get("Value", []):
                    if v.get("Name") == "MacroGraph":
                        idx = v.get("Value", 0)
                        if idx == 0:
                            continue
                        name = self._get_object_name(idx)
                        if name in seen:
                            continue
                        seen.add(name)

                        if idx < 0:
                            # External/engine macro
                            imp = self._resolve_index(idx)
                            source = ""
                            if imp:
                                outer_idx = imp.get("OuterIndex", 0)
                                source = self._get_object_name(outer_idx)
                            macros.append(BPMacro(
                                name=name,
                                export_index=-1,
                                is_local=False,
                                source=source,
                            ))
                        elif name not in macro_name_to_export and name not in function_graph_names:
                            # Local macro not in MacroGraphs and not in FunctionGraphs (fallback)
                            export_idx = idx - 1
                            macro = BPMacro(
                                name=name,
                                export_index=export_idx,
                                is_local=True,
                            )
                            for i, node_exp in enumerate(self.exports):
                                node_name = node_exp.get("ObjectName", "")
                                if "K2Node" not in node_name:
                                    continue
                                outer = node_exp.get("OuterIndex", 0)
                                if outer - 1 == export_idx:
                                    macro.nodes.append({"index": i, "name": node_name})
                                    if "CallFunction" in node_name:
                                        call = self._parse_call_function_node(node_exp, i)
                                        if call:
                                            macro.calls.append(call)
                            macros.append(macro)

        return macros
