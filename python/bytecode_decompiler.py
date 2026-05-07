"""
bytecode_decompiler.py - Kismet ScriptBytecode → pseudocode

Converts UAssetGUI's ScriptBytecode JSON into human-readable pseudocode
with actual execution order, branching, literal values, and parameters.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DecompiledLine:
    text: str
    indent: int = 0
    is_branch: bool = False
    is_jump: bool = False
    offset: int = 0  # bytecode offset for jump targets


class BytecodeDecompiler:
    def __init__(self, imports: list[dict]):
        self.imports = imports

    def resolve_stack_node(self, sn) -> str:
        """Resolve StackNode index to Class::Function name."""
        if isinstance(sn, int) and sn < 0:
            imp = self.imports[abs(sn) - 1]
            fname = imp.get("ObjectName", "?")
            outer_idx = imp.get("OuterIndex", 0)
            if outer_idx < 0:
                parent = self.imports[abs(outer_idx) - 1].get("ObjectName", "?")
                return f"{parent}::{fname}"
            return fname
        return str(sn)

    def decompile_function(self, bytecode: list[dict]) -> list[DecompiledLine]:
        """Decompile a ScriptBytecode array into pseudocode lines."""
        lines = []
        for op in bytecode:
            result = self._decompile_op(op)
            if result is not None:
                lines.append(DecompiledLine(text=result))
        return lines

    def decompile_to_text(self, bytecode: list[dict]) -> str:
        """Decompile and return as formatted text string."""
        lines = self.decompile_function(bytecode)
        # Post-process: add indentation for branches
        result = []
        indent = 0
        for line in lines:
            if line.text.startswith("} "):
                indent = max(0, indent - 1)
            result.append("  " * indent + line.text)
            if line.text.startswith("IF ") or line.text.startswith("ELSE"):
                indent += 1
        return "\n".join(result)

    def _decompile_op(self, op: dict) -> Optional[str]:
        """Decompile a single bytecode operation."""
        t = self._op_type(op)

        # Skip noise
        if t in (
            "EX_Tracepoint", "EX_WireTracepoint", "EX_Nothing",
            "EX_EndOfScript", "EX_Breakpoint",
        ):
            return None

        # Variable assignment.
        # EX_Let uses Value(KismetPropertyPointer) + Expression.
        # EX_LetBool/EX_LetObj/EX_LetWeakObjPtr/EX_LetDelegate/EX_LetMulticastDelegate
        # use VariableExpression(an EX_LocalVariable expression) + AssignmentExpression.
        if t in ("EX_Let", "EX_LetBool", "EX_LetObj",
                 "EX_LetWeakObjPtr", "EX_LetDelegate", "EX_LetMulticastDelegate"):
            if t == "EX_Let":
                raw = self._extract_path(op.get("Value", {}))
                varname = self._clean_var(raw) if raw else ""
            else:
                varname = self._expr(op.get("VariableExpression", {}))
                if varname == "?":
                    varname = ""
            expr_key = "Expression" if t == "EX_Let" else "AssignmentExpression"
            expr = self._expr(op.get(expr_key, {}))
            if varname and expr and expr != "?":
                return f"{varname} = {expr}"
            if varname:
                return f"{varname} = ..."
            return None

        if t == "EX_LetValueOnPersistentFrame":
            varname = self._extract_path(op.get("DestinationProperty", {}))
            expr = self._expr(op.get("AssignmentExpression", {}))
            varname = self._clean_var(varname or "?")
            return f"{varname} = {expr or '...'}"

        # Function calls (standalone, not in assignment)
        if t in ("EX_FinalFunction", "EX_CallMath", "EX_LocalFinalFunction"):
            return self._expr(op)

        # Context call (object.Method())
        if t == "EX_Context":
            return self._expr(op)

        # Virtual function calls
        if t in ("EX_VirtualFunction", "EX_LocalVirtualFunction"):
            return self._expr(op)

        # Branching
        if t == "EX_JumpIfNot":
            cond = self._expr(op.get("BooleanExpression", {}))
            return f"IF NOT ({cond})"

        if t == "EX_Jump":
            offset = op.get("CodeOffset", 0)
            return f"JUMP → {offset}"

        if t == "EX_ComputedJump":
            return "SWITCH"

        # Return
        if t == "EX_Return":
            ret_expr = op.get("ReturnExpression")
            if ret_expr:
                return f"RETURN {self._expr(ret_expr)}"
            return "RETURN"

        # Delegate operations
        if t == "EX_BindDelegate":
            fname = op.get("FunctionName", "?")
            delegate = self._expr(op.get("Delegate", {})) or "?"
            obj = self._expr(op.get("ObjectTerm", {})) or "self"
            return f"Bind {delegate} → {obj}.{fname}()"

        if t == "EX_AddMulticastDelegate":
            delegate = self._expr(op.get("Delegate", {})) or "?"
            handler = self._expr(op.get("DelegateToAdd", {})) or "?"
            return f"AddDelegate {delegate} += {handler}"

        if t == "EX_CallMulticastDelegate":
            fname = self.resolve_stack_node(op.get("StackNode", 0))
            params = self._params(op.get("Parameters", []))
            return f"Broadcast {fname}({params})"

        # Collection operations
        if t == "EX_SetArray":
            return "SetArray{...}"

        if t == "EX_SetMap":
            return "SetMap{...}"

        return None

    def _expr(self, op: dict) -> str:
        """Recursively convert an expression to string."""
        if not op or not isinstance(op, dict):
            return "?"
        t = self._op_type(op)

        # Function calls
        if t in ("EX_FinalFunction", "EX_CallMath", "EX_LocalFinalFunction"):
            fname = self.resolve_stack_node(op.get("StackNode", 0))
            params = self._params(op.get("Parameters", []))
            # Simplify known math/string library calls
            fname = self._simplify_func(fname)
            return f"{fname}({params})"

        if t in ("EX_VirtualFunction", "EX_LocalVirtualFunction"):
            vfn = op.get("VirtualFunctionName", "?")
            params = self._params(op.get("Parameters", []))
            return f"{vfn}({params})"

        # Context: object.Method()
        if t == "EX_Context":
            obj = self._expr(op.get("ObjectExpression", {}))
            ctx = self._expr(op.get("ContextExpression", {}))
            return f"{obj}.{ctx}"

        # Variables
        if t in ("EX_LocalVariable", "EX_LocalOutVariable"):
            path = op.get("Variable", {}).get("New", {}).get("Path", [])
            return self._clean_var(path[0]) if path else "?"

        if t == "EX_InstanceVariable":
            path = op.get("Variable", {}).get("New", {}).get("Path", [])
            return self._clean_var(path[0]) if path else "?"

        if t == "EX_StructMemberContext":
            member = op.get("StructMemberExpression", {}).get("New", {}).get("Path", [])
            inner = self._expr(op.get("StructExpression", {}))
            mname = member[0] if member else "?"
            return f"{inner}.{mname}"

        # Literals
        if t == "EX_Self":
            return "self"
        if t == "EX_True":
            return "true"
        if t == "EX_False":
            return "false"
        if t == "EX_IntConst":
            return str(op.get("Value", 0))
        if t in ("EX_DoubleConst", "EX_FloatConst"):
            return str(op.get("Value", 0.0))
        if t == "EX_StringConst":
            return f'"{op.get("Value", "")}"'
        if t == "EX_NameConst":
            return f'FName("{op.get("Value", "")}")'
        if t == "EX_ByteConst":
            return str(op.get("Value", 0))
        if t == "EX_NoObject":
            return "nullptr"
        if t == "EX_ObjectConst":
            return str(op.get("Value", "?"))
        if t == "EX_Nothing":
            return ""
        if t == "EX_ArrayConst":
            return "[...]"
        if t == "EX_StructConst":
            return "Struct{...}"
        if t == "EX_PrimitiveCast":
            inner = self._expr(op.get("Target", {}))
            return f"Cast({inner})"
        if t == "EX_InstanceDelegate":
            return f"Delegate({op.get('FunctionName', '?')})"

        return f"<{t}>"

    def _params(self, params: list) -> str:
        """Convert parameters list to comma-separated string."""
        parts = []
        for p in params:
            e = self._expr(p)
            if e and e != "?" and e != "":
                parts.append(e)
        return ", ".join(parts)

    def _extract_path(self, obj: dict) -> Optional[str]:
        """Extract Path from a KismetPropertyPointer."""
        if isinstance(obj, dict):
            new = obj.get("New", {})
            if isinstance(new, dict):
                path = new.get("Path", [])
                if path:
                    return path[0]
        return None

    def _clean_var(self, name: str) -> str:
        """Clean up auto-generated variable names."""
        # Remove CallFunc_ prefix and _ReturnValue suffix for readability
        if name.startswith("CallFunc_") and "_ReturnValue" in name:
            # e.g. CallFunc_GetAppVersion_ReturnValue -> GetAppVersion_result
            mid = name[len("CallFunc_"):]
            mid = mid.replace("_ReturnValue", "_result")
            return mid
        if name.startswith("Temp_") and "_Variable" in name:
            return name.replace("Temp_", "tmp_").replace("_Variable", "")
        return name

    def _simplify_func(self, fname: str) -> str:
        """Simplify well-known library function names."""
        simplify_map = {
            "KismetMathLibrary::Add_IntInt": "int_add",
            "KismetMathLibrary::Less_IntInt": "int_less",
            "KismetMathLibrary::Greater_IntInt": "int_greater",
            "KismetMathLibrary::GreaterEqual_IntInt": "int_gte",
            "KismetMathLibrary::EqualEqual_IntInt": "int_eq",
            "KismetMathLibrary::NotEqual_IntInt": "int_neq",
            "KismetMathLibrary::Not_PreBool": "NOT",
            "KismetMathLibrary::BooleanAND": "AND",
            "KismetMathLibrary::BooleanOR": "OR",
            "KismetMathLibrary::SelectInt": "select_int",
            "KismetMathLibrary::SelectFloat": "select_float",
            "KismetMathLibrary::FTrunc": "trunc",
            "KismetMathLibrary::Conv_ByteToInt": "byte_to_int",
            "KismetStringLibrary::Conv_IntToString": "int_to_str",
            "KismetStringLibrary::Conv_BoolToString": "bool_to_str",
            "KismetStringLibrary::Concat_StrStr": "str_concat",
            "KismetStringLibrary::EqualEqual_StrStr": "str_eq",
            "KismetStringLibrary::NotEqual_StriStri": "str_neq_ci",
            "KismetStringLibrary::IsEmpty": "str_empty",
            "KismetStringLibrary::EndsWith": "str_endswith",
            "KismetStringLibrary::LeftChop": "str_leftchop",
            "KismetTextLibrary::Conv_StringToText": "str_to_text",
            "KismetSystemLibrary::IsValid": "IsValid",
            "KismetSystemLibrary::PrintString": "PrintString",
            "GameplayStatics::GetActorOfClass": "GetActorOfClass",
            "GameplayStatics::GetAllActorsOfClass": "GetAllActorsOfClass",
        }
        return simplify_map.get(fname, fname)

    @staticmethod
    def _op_type(op: dict) -> str:
        return op.get("$type", "").split(".")[-1].replace(", UAssetAPI", "")
