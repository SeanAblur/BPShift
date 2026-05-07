"""
format_migration.py - Format parsed Blueprint data for C++ migration

Produces concise, structured text optimized for LLM-driven migration.
Supports two detail levels:
  - summary: call chain + node types (fast overview)
  - bytecode: decompiled pseudocode per function (full logic)
"""

from kismet_parser import BlueprintData, BPFunction
from bytecode_decompiler import BytecodeDecompiler


def format_migration(
    bp: BlueprintData,
    max_lines: int = 0,
    bytecode_mode: bool = False,
    decompiler: "BytecodeDecompiler | None" = None,
    function_bytecodes: "dict | None" = None,
) -> str:
    """Format BlueprintData as migration-ready text.

    Args:
        bp: Parsed blueprint data
        max_lines: Max output lines (0=unlimited)
        bytecode_mode: If True, include decompiled bytecode per function
        decompiler: BytecodeDecompiler instance (required if bytecode_mode)
        function_bytecodes: {function_name: ScriptBytecode list} mapping
    """
    lines = []

    # Header
    lines.append(f"=== {bp.asset_name} ===")
    lines.append(f"Parent Class: {bp.parent_class}")
    lines.append(f"Asset Type: {bp.asset_type}")
    if bp.interfaces:
        lines.append(f"Interfaces: {', '.join(bp.interfaces)}")
    lines.append("")

    # Variables
    if bp.variables:
        lines.append(f"--- Variables ({len(bp.variables)}) ---")
        for var in bp.variables:
            flags_str = f" [{', '.join(var.flags)}]" if var.flags else ""
            default_str = f" = {var.default_value}" if var.default_value else ""
            lines.append(f"  {var.name}: {var.type_name}{default_str}{flags_str}")
        lines.append("")

    bc_map = function_bytecodes or {}

    # Events
    if bp.events:
        lines.append(f"--- Event Handlers ({len(bp.events)}) ---")
        for event in bp.events:
            event_label = (
                f" ({event.event_type})" if event.event_type != "Custom" else ""
            )
            # ComponentBoundEvent handlers can have a stale alias as the graph
            # node name after a widget rename, so display the actual
            # ComponentPropertyName/DelegatePropertyName instead.
            bind_label = ""
            if event.bound_component or event.bound_delegate:
                comp = event.bound_component or "?"
                delegate = event.bound_delegate or "?"
                bind_label = f" → {comp}.{delegate}"
            lines.append(f"{event.name}{event_label}{bind_label}:")
            _format_func(event.function, lines, bytecode_mode, decompiler, bc_map)
            lines.append("")

    # Functions
    if bp.functions:
        lines.append(f"--- Functions ({len(bp.functions)}) ---")
        for func in bp.functions:
            flags_str = ""
            notable = [
                f
                for f in func.flags
                if f
                in (
                    "FUNC_Static",
                    "FUNC_Const",
                    "FUNC_BlueprintPure",
                    "FUNC_BlueprintCallable",
                )
            ]
            if notable:
                flags_str = f" [{', '.join(notable)}]"

            params_str = ""
            if func.parameters:
                params = []
                for p in func.parameters:
                    if "ReturnParm" not in p.get("flags", ""):
                        params.append(p.get("name", "?"))
                params_str = f"({', '.join(params)})"

            lines.append(f"{func.name}{params_str}{flags_str}:")
            _format_func(func, lines, bytecode_mode, decompiler, bc_map)
            lines.append("")

    # Macros
    local_macros = [m for m in bp.macros if m.is_local]
    engine_macros = [m for m in bp.macros if not m.is_local]
    if local_macros:
        lines.append(f"--- Local Macros ({len(local_macros)}) ---")
        for macro in local_macros:
            lines.append(f"{macro.name}:")
            if macro.calls:
                for i, call in enumerate(macro.calls, 1):
                    pure_tag = " [Pure]" if call.is_pure else ""
                    class_tag = (
                        f" ({call.target_class})" if call.target_class else ""
                    )
                    lines.append(
                        f"  {i}. {call.function_name}{class_tag}{pure_tag}"
                    )
            if macro.nodes:
                non_call = [
                    n["name"]
                    for n in macro.nodes
                    if "CallFunction" not in n["name"]
                ]
                if non_call:
                    node_types = set()
                    for n in non_call:
                        if "K2Node_" in n:
                            ntype = n.split("K2Node_")[1].rsplit("_", 1)[0]
                            node_types.add(ntype)
                    if node_types:
                        lines.append(
                            f"  Flow: {', '.join(sorted(node_types))}"
                        )
            if not macro.calls and not macro.nodes:
                lines.append("  (empty)")
            lines.append("")

    if engine_macros:
        lines.append(f"--- Engine Macros ({len(engine_macros)}) ---")
        for macro in engine_macros:
            source_tag = f" (from {macro.source})" if macro.source else ""
            lines.append(f"  {macro.name}{source_tag}")
        lines.append("")

    # C++ References
    if bp.cpp_class_refs:
        lines.append("--- C++ Class References ---")
        lines.append(f"  {', '.join(bp.cpp_class_refs)}")
        lines.append("")

    if bp.cpp_function_refs:
        lines.append("--- C++ Function References ---")
        for ref in bp.cpp_function_refs:
            lines.append(f"  {ref}")
        lines.append("")

    # Delegate Bindings
    if bp.delegate_bindings:
        lines.append("--- Delegate Bindings ---")
        for binding in bp.delegate_bindings:
            lines.append(
                f"  {binding['delegate']} (in {binding['in_function']})"
            )
        lines.append("")

    result = "\n".join(lines)

    if max_lines > 0:
        result_lines = result.split("\n")
        if len(result_lines) > max_lines:
            result = "\n".join(result_lines[:max_lines])
            result += (
                f"\n\n... (truncated, {len(result_lines) - max_lines}"
                " lines omitted)"
            )

    return result


def _format_func(
    func: BPFunction,
    lines: list[str],
    bytecode_mode: bool,
    decompiler: "BytecodeDecompiler | None",
    bc_map: dict,
):
    """Format a single function body."""
    # Try bytecode decompilation first
    if bytecode_mode and decompiler and func.name in bc_map:
        bc = bc_map[func.name]
        if bc:
            text = decompiler.decompile_to_text(bc)
            if text.strip():
                for line in text.split("\n"):
                    lines.append(f"  {line}")
                return

    # Fallback: K2Node-based call chain
    if func.calls:
        for i, call in enumerate(func.calls, 1):
            pure_tag = " [Pure]" if call.is_pure else ""
            class_tag = (
                f" ({call.target_class})" if call.target_class else ""
            )
            lines.append(
                f"  {i}. {call.function_name}{class_tag}{pure_tag}"
            )
        return

    if func.nodes:
        node_types = set()
        for n in func.nodes:
            name = n["name"]
            if "K2Node_" in name:
                ntype = name.split("K2Node_")[1].rsplit("_", 1)[0]
                node_types.add(ntype)
        if node_types:
            lines.append(f"  Nodes: {', '.join(sorted(node_types))}")
        return

    lines.append("  (empty)")
