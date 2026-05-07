#!/usr/bin/env python3
"""
bpmigrate - Cross-platform CLI for the BP migration toolchain.

Subcommands (grouped by stage):

  -- Discovery / read-only --
  find-bp            Locate a Blueprint .uasset by name in the configured Content root.
  inspect            Full read-only pipeline: find -> tojson -> summarize -> detect gaps.
  uasset-tojson      Convert a .uasset to JSON via the bundled UAssetGUI.
  summarize          Format a UAssetGUI JSON into a readable Blueprint summary.
  detect-gaps        Static detection of missing FunctionExports, refs-vs-body gaps,
                     orphan pins, cook-ref candidates, broken references, etc.

  -- Editor commandlet wrappers (require the UE plugin) --
  dump-graph             Invoke DumpBPGraph -- nodes, pins, components, CDO overrides.
  dump-class-reflection  Invoke DumpClassReflection -- UClass functions + properties.
  snapshot               Invoke SnapshotBPBehavior to capture a Blueprint behavior trace.
  verify                 Invoke VerifyMigration to diff a C++ class against a trace.
  rewrite-callers        Invoke RewriteCallers -- swap caller K2Node CallFunction targets,
                         preserves DefaultObject (CDO) and inserts pure DynamicCast as needed.
  verify-callers         Invoke VerifyCallers -- force-unload + fresh-load + recompile every
                         caller of a target BP and report PASS/FAIL via FCompilerResultsLog.

  -- Migration analysis (drive the recipe, no LLM in the loop) --
  analyze-candidate      6-criteria suitability matrix for a target BP (SCS overlap /
                         BP-defined Interface / Variables / Functions / Caller count /
                         Reload-validation) + a one-line proceed/skip recommendation.
  plan-rewrite-callers   Discover every caller of a target BP via AssetRegistry, then
                         enumerate every K2Node_CallFunction call-site (caller, fn,
                         count) via the DumpCallSites editor commandlet. Output is
                         a markdown table + ranked non-interface migration candidates.

  -- Reparent broken-ref pipeline (Layer 1 -> 2 -> 3) --
  map-broken-refs        Classify broken refs vs new parent's reflection (auto / user_required / reject).
  apply-fix-mapping      Emit per-node editor steps from a mapping (dry-run; never modifies .uasset).

  -- Deterministic codegen primitives (no LLM in the loop) --
  emit-class-flags           graph dump  -> `UCLASS(<specifiers>)` line.
  emit-dispatcher-delegates  UAssetGUI   -> DECLARE_DYNAMIC_MULTICAST_DELEGATE_*Param macros.
  emit-variable-defaults     UAssetGUI   -> UPROPERTY declarations + initializers (Rule 10/11).
  emit-component-overrides   graph dump  -> constructor body (CreateDefaultSubobject, setters, BodyInstance).

  -- Misc --
  scenario           Generate a default test scenario from UAssetGUI JSON + graph dump.

Configuration via env vars (CLI flags override):
  BPMIGRATION_PROJECT_ROOT    Project root (parent of Content/).
  BPMIGRATION_CONTENT_ROOT    Content directory (default: $PROJECT_ROOT/Content).
  BPMIGRATION_UPROJECT        Path to .uproject file.
  BPMIGRATION_UE_CMD          Path to UnrealEditor-Cmd executable.
  BPMIGRATION_UASSETGUI       Path to UAssetGUI.exe (default: bundled tools/UAssetGUI/UAssetGUI.exe).
  BPMIGRATION_UE_VERSION      UE version for UAssetGUI tojson (default: UE5_2).
  BPMIGRATION_TMPDIR          Temp directory for intermediates (default: <system temp>/BPShift).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Optional

# Make sibling modules importable regardless of how bpmigrate.py is invoked.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))


# ============================================================================
# Argv / env-var path normalization for Git Bash / MSYS / Cygwin on Windows.
#
# Two failure modes that hit every new Windows user on Git Bash:
#
#   1) MSYS argv mangling: the shell rewrites `/Game/Foo` -> `C:/Program
#      Files/Git/Game/Foo` BEFORE Python sees argv. Setting
#      MSYS_NO_PATHCONV=1 inside Python is too late. We detect the prefix
#      and reverse it.
#
#   2) Cygwin paths post-MSYS_NO_PATHCONV: once the shell stops translating,
#      `/c/Users/foo` reaches Python literally and `Path.exists()` fails.
#      We translate `/c/foo` -> `C:/foo`.
#
# Both are emitted as a one-line warning so the user understands why their
# arg shape was tweaked. False-positive guard: the MSYS prefix is only
# matched when followed by `/Game/`, `/Script/`, `/Engine/`, or `/Plugins/`
# -- those are UE asset path roots that NEVER appear under a real Git
# install dir. The Cygwin pattern only fires for lowercase single-letter
# drive (Cygwin convention); uppercase like `/G/` is left alone.
# ============================================================================

_MSYS_MANGLE_RE = re.compile(
    r"^C:/Program Files( \(x86\))?/Git/(Game|Script|Engine|Plugins)/(.+)$"
)
_CYGWIN_DRIVE_RE = re.compile(r"^/([a-z])/([^/].*)$")


def _normalize_winshell_path(p: str) -> tuple[str, Optional[str]]:
    """Return (normalized, warning_msg_or_None).

    Conservative: only rewrites the two known mangling patterns. Returns
    `p` unchanged for everything else, including macOS / Linux paths.
    """
    if sys.platform != "win32":
        return p, None
    m = _MSYS_MANGLE_RE.match(p)
    if m:
        fixed = f"/{m.group(2)}/{m.group(3)}"
        return fixed, f"argv MSYS-mangled: {p!r} -> {fixed!r}"
    m = _CYGWIN_DRIVE_RE.match(p)
    if m:
        fixed = f"{m.group(1).upper()}:/{m.group(2)}"
        return fixed, f"argv Cygwin path: {p!r} -> {fixed!r}"
    return p, None


def _autofix_argv_and_env() -> None:
    """Apply `_normalize_winshell_path` to argv and to the well-known
    BPMIGRATION_* env vars that hold filesystem / asset paths. Idempotent;
    skipped on non-Windows.
    """
    if sys.platform != "win32":
        return
    for i, arg in enumerate(sys.argv[1:], start=1):
        fixed, msg = _normalize_winshell_path(arg)
        if msg:
            print(f"bpmigrate: {msg}", file=sys.stderr)
            sys.argv[i] = fixed
    for var in (
        "BPMIGRATION_PROJECT_ROOT",
        "BPMIGRATION_CONTENT_ROOT",
        "BPMIGRATION_UPROJECT",
        "BPMIGRATION_UE_CMD",
        "BPMIGRATION_UASSETGUI",
        "BPMIGRATION_TMPDIR",
    ):
        v = os.environ.get(var)
        if not v:
            continue
        fixed, msg = _normalize_winshell_path(v)
        if msg:
            print(f"bpmigrate: env {var}: {msg}", file=sys.stderr)
            os.environ[var] = fixed


# Reconfigure stdout/stderr UP-FRONT so the import-time autofix warnings
# below (which print non-ASCII path fragments through sys.stderr on Windows
# cp949 consoles) don't crash before main() has a chance to set this up.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError):
        pass

_autofix_argv_and_env()


# ---------- shared output helper ----------


def _emit_to_output(text: str, output: Optional[str]) -> None:
    """Write `text` to file (if output is set) or stdout. Always trailing newline.

    Used by every emit-* / map-broken-refs / apply-fix-mapping / detect-gaps
    sub-command -- the convention is "stdout when no -o, file when -o".
    Centralizing prevents drift (e.g. one site forgetting the newline,
    another using Windows-CRLF, another not respecting UTF-8).
    """
    if not text.endswith("\n"):
        text = text + "\n"
    if output:
        Path(output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)

# Disable MSYS / Git-Bash automatic path conversion for subprocess args.
# Without this, "/Game/Foo/Bar" gets rewritten to "C:/Program Files/Git/Game/Foo/Bar"
# before reaching UnrealEditor-Cmd.
os.environ.setdefault("MSYS_NO_PATHCONV", "1")
os.environ.setdefault("MSYS2_ARG_CONV_EXCL", "*")

# These are imported lazily inside the subcommands that need them so a
# missing optional dependency does not break unrelated commands.


# ---------- configuration ----------


class Config:
    """Resolved runtime configuration.

    Precedence (highest to lowest):
      1. CLI flag (e.g. --uproject)
      2. Environment variable (BPMIGRATION_*)
      3. Config file [tool.bpmigrate] section (TOML)
      4. Built-in default

    Config file discovery (when --config not given):
      - <CWD>/.bpmigrate.toml
      - <CWD>/bpmigrate.toml
      - <CWD>/pyproject.toml (under [tool.bpmigrate])
      - <PROJECT_ROOT>/.bpmigrate.toml (when PROJECT_ROOT is set)
    """

    def __init__(self, args: argparse.Namespace):
        cfg_file = _load_config_file(getattr(args, "config", None))

        def resolve(arg_name: str, env_name: str, cfg_key: str) -> Optional[str]:
            return (
                getattr(args, arg_name, None)
                or os.environ.get(env_name)
                or cfg_file.get(cfg_key)
            )

        self.project_root: Optional[Path] = _resolve_path(
            resolve("project_root", "BPMIGRATION_PROJECT_ROOT", "project_root")
        )
        self.uproject: Optional[Path] = _resolve_path(
            resolve("uproject", "BPMIGRATION_UPROJECT", "uproject")
        )
        self.ue_cmd: Optional[Path] = _resolve_path(
            resolve("ue_cmd", "BPMIGRATION_UE_CMD", "ue_cmd")
        )
        self.uassetgui: Optional[Path] = _resolve_path(
            resolve("uassetgui", "BPMIGRATION_UASSETGUI", "uassetgui")
        ) or _bundled_uassetgui()
        self.ue_version: str = (
            resolve("ue_version", "BPMIGRATION_UE_VERSION", "ue_version") or "UE5_2"
        )

        content_arg = resolve("content_root", "BPMIGRATION_CONTENT_ROOT", "content_root")
        if content_arg:
            self.content_root: Optional[Path] = _resolve_path(content_arg)
        elif self.project_root:
            self.content_root = self.project_root / "Content"
        else:
            self.content_root = None

        tmp_arg = resolve("tmpdir", "BPMIGRATION_TMPDIR", "tmpdir")
        if tmp_arg:
            self.tmpdir: Path = Path(tmp_arg)
        else:
            self.tmpdir = Path(tempfile.gettempdir()) / "BPShift"
        self.tmpdir.mkdir(parents=True, exist_ok=True)


    # -- assertions used by subcommands --

    def require_uassetgui(self) -> Path:
        if not self.uassetgui or not self.uassetgui.exists():
            _die(
                "UAssetGUI not found. Set BPMIGRATION_UASSETGUI or pass --uassetgui. "
                f"(checked: {self.uassetgui or '<none>'})"
            )
        return self.uassetgui  # type: ignore[return-value]

    def require_ue_cmd(self) -> Path:
        if not self.ue_cmd or not self.ue_cmd.exists():
            _die(
                "UnrealEditor-Cmd not found. Set BPMIGRATION_UE_CMD or pass --ue-cmd."
            )
        return self.ue_cmd  # type: ignore[return-value]

    def require_uproject(self) -> Path:
        if not self.uproject or not self.uproject.exists():
            _die(
                "Project file not found. Set BPMIGRATION_UPROJECT or pass --uproject."
            )
        return self.uproject  # type: ignore[return-value]

    def require_content_root(self) -> Path:
        if not self.content_root or not self.content_root.exists():
            _die(
                "Content root not found. Set BPMIGRATION_PROJECT_ROOT (its Content/ "
                "is used by default) or BPMIGRATION_CONTENT_ROOT directly."
            )
        return self.content_root  # type: ignore[return-value]


def _load_config_file(explicit_path: Optional[str]) -> dict:
    """Load TOML config; return [tool.bpmigrate] section or {}.

    Discovery order (when explicit_path is None):
      1. <CWD>/.bpmigrate.toml
      2. <CWD>/bpmigrate.toml
      3. <CWD>/pyproject.toml (under [tool.bpmigrate])
      4. <BPMIGRATION_PROJECT_ROOT>/.bpmigrate.toml
    """
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return {}

    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path).expanduser())
    else:
        cwd = Path.cwd()
        candidates += [
            cwd / ".bpmigrate.toml",
            cwd / "bpmigrate.toml",
            cwd / "pyproject.toml",
        ]
        env_root = os.environ.get("BPMIGRATION_PROJECT_ROOT")
        if env_root:
            candidates.append(Path(env_root) / ".bpmigrate.toml")

    for cand in candidates:
        if not cand.is_file():
            continue
        try:
            with open(cand, "rb") as f:
                doc = tomllib.load(f)
        except Exception:
            continue
        section = doc.get("tool", {}).get("bpmigrate", doc)
        if isinstance(section, dict):
            return section
    return {}


def _resolve_path(s: Optional[str]) -> Optional[Path]:
    if not s:
        return None
    return Path(s).expanduser()


def _bundled_uassetgui() -> Optional[Path]:
    """Auto-detect bundled UAssetGUI binary by walking up from this file."""
    for parent in (_HERE, *_HERE.parents):
        candidate = parent / "tools" / "UAssetGUI" / "UAssetGUI.exe"
        if candidate.exists():
            return candidate
    return None


def _stage_writable(src: Path, dst: Path) -> None:
    """Copy src to dst with write permission, replacing any existing read-only file.

    Source-control systems (Perforce, Git LFS) often mark working-tree files
    read-only; preserving that into our temp copy makes the next run fail.
    """
    import stat
    if dst.exists():
        try:
            dst.chmod(stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        dst.unlink()
    shutil.copyfile(src, dst)
    try:
        dst.chmod(stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def _die(msg: str, code: int = 2) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------- subprocess helpers ----------


def _run(cmd: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a subprocess. Echoes the command on stderr for traceability."""
    print("+ " + " ".join(_quote(c) for c in cmd), file=sys.stderr)
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True, check=False)
    return subprocess.run(cmd, check=False)


def _quote(s: str) -> str:
    if any(c in s for c in (" ", "\t")):
        return f'"{s}"'
    return s


# ---------- subcommand: uasset-tojson ----------


def cmd_uasset_tojson(args: argparse.Namespace, cfg: Config) -> int:
    uagui = cfg.require_uassetgui()
    src = Path(args.uasset).resolve()
    if not src.exists():
        _die(f"Source .uasset not found: {src}")

    dst = Path(args.output).resolve() if args.output else (
        cfg.tmpdir / (src.stem + ".json")
    )
    dst.parent.mkdir(parents=True, exist_ok=True)

    # UAssetGUI cannot read files locked by an open editor; copy to tmp first.
    staged = cfg.tmpdir / src.name
    _stage_writable(src, staged)

    res = _run([str(uagui), "tojson", str(staged), str(dst), cfg.ue_version])
    if res.returncode != 0:
        _die(f"UAssetGUI tojson failed (exit {res.returncode})", code=res.returncode)
    print(str(dst))
    return 0


# ---------- subcommand: summarize ----------


def cmd_summarize(args: argparse.Namespace, cfg: Config) -> int:
    from kismet_parser import KismetParser
    from format_migration import format_migration
    from bytecode_decompiler import BytecodeDecompiler

    json_path = Path(args.json_path).resolve()
    if not json_path.exists():
        _die(f"JSON not found: {json_path}")

    kp = KismetParser(str(json_path))
    bp_data = kp.parse()

    decompiler = None
    bc_map: dict = {}
    if args.bytecode:
        with open(json_path, encoding="utf-8") as f:
            raw = json.load(f)
        decompiler = BytecodeDecompiler(raw.get("Imports", []))
        for exp in raw.get("Exports", []):
            if "FunctionExport" in exp.get("$type", ""):
                name = exp.get("ObjectName", "")
                bc = exp.get("ScriptBytecode", [])
                if name and bc:
                    bc_map[name] = bc

    out = format_migration(
        bp_data,
        max_lines=args.max_lines,
        bytecode_mode=args.bytecode,
        decompiler=decompiler,
        function_bytecodes=bc_map,
    )

    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(out)
    return 0


# ---------- subcommand: scenario ----------


def cmd_scenario(args: argparse.Namespace, cfg: Config) -> int:
    # Direct import-call: scenario_generator lives in the same package, so a
    # subprocess re-exec is pure overhead.
    import scenario_generator
    return scenario_generator.run(args.json_path, args.graph, args.output)


# ---------- subcommand: detect-gaps ----------


def cmd_detect_gaps(args: argparse.Namespace, cfg: Config) -> int:
    """Static detection. Emits a JSON report on stdout."""
    json_path = Path(args.json_path).resolve()
    if not json_path.exists():
        _die(f"JSON not found: {json_path}")

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    summary: Optional[str] = None
    if args.summary_text:
        with open(args.summary_text, encoding="utf-8") as f:
            summary = f.read()

    # Pre-load graph data if --graph is provided so cook-ref detection
    # can consult clean Type/SubType.
    graph_data: Optional[dict] = None
    if args.graph:
        gp = Path(args.graph).resolve()
        if not gp.exists():
            _die(f"Graph dump not found: {gp}")
        with open(gp, encoding="utf-8") as f:
            graph_data = json.load(f)

    report: dict = {
        "schema": "bpmigrate_gaps_v2",
        "source": str(json_path),
        "missingFunctionExports": _detect_missing_funcs(data),
        "cookRefCandidates": _detect_cook_refs(data, summary, graph_data),
    }

    if summary is not None:
        report["referencesBodyGap"] = _detect_refs_body_gap(summary)

    if graph_data is not None:
        orphans, dead_inputs = _detect_orphan_pins(graph_data)
        report["orphanPins"] = orphans
        report["unconnectedDataInputs"] = dead_inputs
        report["brokenReferences"] = _detect_broken_references(graph_data)
        report["unauditedK2Nodes"] = _detect_unaudited_k2nodes(graph_data)

        # Filter macros out of missingFunctionExports (false positives).
        # Some BPs reference macros in their on-disk FunctionGraphs array
        # even though the editor treats them as macros at runtime.
        macro_names: set = set()
        for graph in graph_data.get("Graphs", []):
            if graph.get("GraphType") == "Macro":
                macro_names.add(graph.get("Name", ""))
        if macro_names:
            filtered = [n for n in report["missingFunctionExports"] if n not in macro_names]
            removed = sorted(set(report["missingFunctionExports"]) & macro_names)
            report["missingFunctionExports"] = filtered
            if removed:
                report["macrosFilteredFromMissing"] = removed

        # Plan-stage completeness surfaces. The migration MUST address each
        # of these in the C++ output or the runtime state will diverge --
        # these are categories the bytecode-only summary does not cover.
        report["componentsRequired"] = _flatten_components(
            graph_data.get("Components", [])
        )
        report["classFlags"] = graph_data.get("ClassFlags", [])
        report["parentCDOOverrides"] = graph_data.get("ParentCDOOverrides", [])

    text = json.dumps(report, indent=2, ensure_ascii=False)
    if getattr(args, "output", None):
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


def _flatten_components(components: list, parent: str = "") -> list[dict]:
    """Flatten the SCS component tree into a list with attach-parent recorded.

    Carries DefaultOverrides through verbatim so detect-gaps consumers
    (recipe Step 1-E + emit-component-overrides) can drive deterministic
    constructor generation. Each override entry has Property / Type /
    OurValue / ArchetypeDefault.
    """
    out: list = []
    for n in components:
        entry: dict = {
            "name": n.get("Name", "?"),
            "componentClass": n.get("ComponentClass", "?"),
            "parent": parent or n.get("ParentNode", "") or "<root>",
            "parentIsNative": n.get("ParentIsNative", False),
        }
        overrides = n.get("DefaultOverrides", [])
        if overrides:
            entry["defaultOverrides"] = overrides
        out.append(entry)
        out.extend(_flatten_components(n.get("Children", []), parent=n.get("Name", "?")))
    return out


# ---------- subcommand: emit-variable-defaults ----------


_PIN_CATEGORY_TO_CPP: dict = {
    "bool":   "bool",
    "int":    "int32",
    "int64":  "int64",
    "byte":   "uint8",
    "string": "FString",
    "name":   "FName",
    "text":   "FText",
}


_RESERVED_KEYWORDS: set = {
    "alignas","alignof","and","and_eq","asm","auto","bitand","bitor","bool",
    "break","case","catch","char","class","compl","const","constexpr","const_cast",
    "continue","decltype","default","delete","do","double","dynamic_cast","else",
    "enum","explicit","export","extern","false","float","for","friend","goto","if",
    "inline","int","long","mutable","namespace","new","noexcept","not","not_eq",
    "nullptr","operator","or","or_eq","private","protected","public","register",
    "reinterpret_cast","return","short","signed","sizeof","static","static_assert",
    "static_cast","struct","switch","template","this","thread_local","throw","true",
    "try","typedef","typeid","typename","union","unsigned","using","virtual","void",
    "volatile","wchar_t","while","xor","xor_eq",
}


def _sanitize_name(raw: str) -> tuple[str, bool]:
    """Apply Rule 10 name sanitization. Returns (clean, was_changed)."""
    cleaned = re.sub(r"[\s\t\-]+", "", raw)
    if cleaned and cleaned[0].isdigit():
        cleaned = "Var_" + cleaned
    if cleaned in _RESERVED_KEYWORDS:
        cleaned = cleaned + "_"
    return cleaned, cleaned != raw


def _decode_var_type(b64_blob: str, name_map: list) -> dict:
    """Decode an FEdGraphPinType base64 blob.

    Returns {pinCategory, pinSubCategory, raw_hex}. The first 8 bytes are
    PinCategory (FName: int32 index + int32 Number); the next 8 bytes are
    PinSubCategory. Anything beyond is left to the caller -- most BP
    variables are primitives where category alone suffices.
    """
    import base64 as _b64
    import struct as _st
    out: dict = {"pinCategory": "", "pinSubCategory": "", "raw_hex": ""}
    try:
        b = _b64.b64decode(b64_blob)
    except Exception:
        return out
    out["raw_hex"] = b.hex()
    if len(b) >= 4:
        i0 = _st.unpack_from("<i", b, 0)[0]
        if 0 <= i0 < len(name_map):
            out["pinCategory"] = name_map[i0]
    if len(b) >= 12:
        i1 = _st.unpack_from("<i", b, 8)[0]
        if 0 <= i1 < len(name_map):
            out["pinSubCategory"] = name_map[i1]
    return out


def _resolve_var_cpp_type(var_type: dict) -> tuple[str, Optional[str]]:
    """Resolve C++ type from a decoded VarType. Returns (cpp_type, todo_msg)."""
    cat = var_type.get("pinCategory", "")
    sub = var_type.get("pinSubCategory", "")
    if cat == "real":
        return ("double" if sub == "double" else "float", None)
    if cat in _PIN_CATEGORY_TO_CPP:
        return (_PIN_CATEGORY_TO_CPP[cat], None)
    if cat in ("byte", "enum"):
        return (
            "uint8",
            f"[!] enum/byte resolution requires PinSubCategoryObject decoding (Rule 11);"
            f" cpp type may need TEnumAsByte<E...> or `enum class`",
        )
    if cat == "struct":
        return (
            "/* TODO: struct */ int32",
            f"[!] struct resolution requires PinSubCategoryObject decoding (Rule 11)",
        )
    if cat in ("object", "class", "softobject", "softclass", "interface"):
        return (
            f"/* TODO: {cat} */ UObject*",
            f"[!] object/class resolution requires PinSubCategoryObject decoding (Rule 11)",
        )
    return (
        "/* TODO: unknown PinCategory */ int32",
        f"[!] unknown PinCategory '{cat}' - manual review required",
    )


def _format_default_literal(cpp_type: str, raw: str) -> str:
    """Format a BP DefaultValue string into a C++ initializer expression.

    Falls back to type-natural defaults when raw is empty / 'None'. Returns
    only the right-hand side; the caller emits `= <expr>`.
    """
    if not raw or raw == "None":
        # Type-natural default per Rule 6 / 10 priority 4.
        if cpp_type == "bool":      return "false"
        if cpp_type == "FString":   return "FString()"
        if cpp_type == "FName":     return "NAME_None"
        if cpp_type == "FText":     return "FText::GetEmpty()"
        if cpp_type in ("int32", "int64", "uint8"):
            return "0"
        if cpp_type in ("float", "double"):
            return "0.0"
        return "{}"
    if cpp_type == "bool":
        return "true" if raw.strip().lower() in ("true", "1") else "false"
    if cpp_type in ("int32", "int64", "uint8"):
        return raw.strip()
    if cpp_type == "float":
        return raw.strip().rstrip("f") + "f"
    if cpp_type == "double":
        return raw.strip().rstrip("f")
    if cpp_type == "FString":
        return f'TEXT("{raw}")'
    if cpp_type == "FName":
        return f'FName(TEXT("{raw}"))'
    if cpp_type == "FText":
        return f'FText::FromString(TEXT("{raw}"))'
    return raw  # fallback: pass through


_TOOLTIP_PATTERNS = [
    (1, re.compile(r"(?:percentage|percent)\s*(?:case|context)?\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)", re.IGNORECASE)),
    (2, re.compile(r"(?:default|default\s*value|initial\s*value)\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)", re.IGNORECASE)),
]


def _read_metadata_tooltip(var: dict) -> str:
    """Extract the tooltip string from a NewVariable's MetaDataArray."""
    for f in var.get("Value", []):
        if not isinstance(f, dict):
            continue
        if f.get("Name") != "MetaDataArray":
            continue
        for entry in f.get("Value", []) or []:
            if not isinstance(entry, dict):
                continue
            key = ""
            val = ""
            for sf in entry.get("Value", []) or []:
                if not isinstance(sf, dict):
                    continue
                if sf.get("Name") == "DataKey":
                    key = sf.get("Value", "") or ""
                elif sf.get("Name") == "DataValue":
                    val = sf.get("Value", "") or ""
            if key == "tooltip":
                return val
    return ""


def _read_cdo_override(data: dict, bp_name: str, var_name: str) -> Optional[str]:
    """Look up a variable's value in `Default__<BP>_C`'s data block."""
    target = f"Default__{bp_name}_C"
    for exp in data.get("Exports", []) or []:
        if not isinstance(exp, dict):
            continue
        if exp.get("ObjectName") != target:
            continue
        for de in exp.get("Data", []) or []:
            if isinstance(de, dict) and de.get("Name") == var_name:
                v = de.get("Value")
                if v is None:
                    return None
                return str(v)
    return None


def cmd_emit_variable_defaults(args: argparse.Namespace, cfg: Config) -> int:
    """Emit UPROPERTY declarations from a UAssetGUI JSON's NewVariables.

    Implements Rule 10 priority 1-4 + Rule 11 type resolution + name
    sanitization. Each variable becomes a header-side block:

        /** <FriendlyName / category> -- <origin> */
        UPROPERTY(<flags>, Category="...")
        <type> <Name> = <default>;

    Origin labels: from CDO override / from BP NewVariable.DefaultValue
    / inferred from tooltip pattern <n> / type-natural default.
    """
    in_path = Path(args.input).resolve()
    if not in_path.exists():
        _die(f"Input not found: {in_path}")
    with open(in_path, encoding="utf-8") as f:
        data = json.load(f)

    name_map = data.get("NameMap", []) or []

    # Find NewVariables and the parent BP name (used for CDO lookup).
    bp_name = ""
    new_vars: list = []
    for exp in data.get("Exports", []) or []:
        if not isinstance(exp, dict):
            continue
        for de in exp.get("Data", []) or []:
            if isinstance(de, dict) and de.get("Name") == "NewVariables":
                bp_name = exp.get("ObjectName", "") or ""
                new_vars = de.get("Value", []) or []
                break
        if new_vars:
            break

    if not new_vars:
        out = "// no NewVariables found\n"
        _emit_to_output(out, args.output)
        return 0

    out_lines: list[str] = []
    for var in new_vars:
        if not isinstance(var, dict):
            continue
        var_name = ""
        var_type_blob = ""
        friendly = ""
        category = ""
        default_value = ""
        for f in var.get("Value", []) or []:
            if not isinstance(f, dict):
                continue
            n = f.get("Name")
            v = f.get("Value")
            if n == "VarName":
                var_name = str(v or "")
            elif n == "VarType":
                var_type_blob = str(v or "")
            elif n == "FriendlyName":
                friendly = str(v or "")
            elif n == "Category":
                category = str(v or "")
            elif n == "DefaultValue":
                default_value = "" if v in (None, "None") else str(v)

        if not var_name:
            continue

        sanitized, changed = _sanitize_name(var_name)
        decoded = _decode_var_type(var_type_blob, name_map)
        cpp_type, type_todo = _resolve_var_cpp_type(decoded)

        # Resolve default per Rule 10 priority order.
        cdo = _read_cdo_override(data, bp_name, var_name) if bp_name else None
        origin = ""
        chosen: str = ""
        if cdo not in (None, "", "None"):
            chosen = cdo
            origin = "from CDO override"
        elif default_value:
            chosen = default_value
            origin = "from BP NewVariable.DefaultValue"
        else:
            # Tooltip patterns
            tip = _read_metadata_tooltip(var)
            for n, pat in _TOOLTIP_PATTERNS:
                m = pat.search(tip)
                if m:
                    chosen = m.group(1)
                    origin = f'inferred from tooltip via pattern {n}: "{tip[:60]}"'
                    break
            if not origin:
                origin = "[!] type-natural default - REQUIRES USER INPUT"

        rhs = _format_default_literal(cpp_type, chosen)

        # FText Category may serialize as a raw 32-char hex GUID when the
        # display string is missing from the namespace map. That's noise,
        # not user intent -- fall back to "Default" so the C++ Category
        # is readable.
        if category and re.fullmatch(r"[0-9A-Fa-f]{32}", category):
            category = "Default"
        cat_safe = (category or "Default").replace('"', "'")
        out_lines.append(f"/** {friendly or var_name} -- {origin} */")
        if type_todo:
            out_lines.append(f"// {type_todo}")
        if changed:
            out_lines.append(
                f'UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="{cat_safe}",'
                f' meta=(DisplayName="{var_name}"))'
            )
        else:
            out_lines.append(
                f'UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="{cat_safe}")'
            )
        out_lines.append(f"{cpp_type} {sanitized} = {rhs};")
        out_lines.append("")

    text = "\n".join(out_lines).rstrip() + "\n"
    _emit_to_output(text, args.output)
    return 0


# ---------- subcommand: emit-dispatcher-delegates ----------


_BP_PROP_TYPE_TO_CPP: dict = {
    "FBoolProperty":   "bool",
    "FByteProperty":   "uint8",
    "FIntProperty":    "int32",
    "FInt64Property":  "int64",
    "FFloatProperty":  "float",
    "FDoubleProperty": "double",
    "FStrProperty":    "const FString&",
    "FNameProperty":   "FName",
    "FTextProperty":   "const FText&",
    # FObjectProperty / FStructProperty / FClassProperty / FArrayProperty
    # need extra metadata (PropertyClass / Struct / Enum) to resolve fully.
    # The generator emits an explicit `// TODO:` for those so nothing is
    # silently dropped -- see Rule 11.
}

_DELEGATE_MACROS: list[str] = [
    "DECLARE_DYNAMIC_MULTICAST_DELEGATE",
    "DECLARE_DYNAMIC_MULTICAST_DELEGATE_OneParam",
    "DECLARE_DYNAMIC_MULTICAST_DELEGATE_TwoParams",
    "DECLARE_DYNAMIC_MULTICAST_DELEGATE_ThreeParams",
    "DECLARE_DYNAMIC_MULTICAST_DELEGATE_FourParams",
    "DECLARE_DYNAMIC_MULTICAST_DELEGATE_FiveParams",
    "DECLARE_DYNAMIC_MULTICAST_DELEGATE_SixParams",
    "DECLARE_DYNAMIC_MULTICAST_DELEGATE_SevenParams",
    "DECLARE_DYNAMIC_MULTICAST_DELEGATE_EightParams",
    "DECLARE_DYNAMIC_MULTICAST_DELEGATE_NineParams",
]


def _resolve_prop_cpp_type(prop: dict) -> str:
    """FProperty entry -> C++ type string. Unknown -> `/* TODO: ... */`."""
    raw = prop.get("$type", "")
    short = raw.split(",")[0].split(".")[-1] if raw else ""
    if short in _BP_PROP_TYPE_TO_CPP:
        return _BP_PROP_TYPE_TO_CPP[short]
    # Object / class refs: try to read PropertyClass.ObjectName
    if short == "FObjectProperty":
        cls = prop.get("PropertyClass")
        if isinstance(cls, dict):
            obj = cls.get("ObjectName") or cls.get("Name")
            if obj:
                return f"U{obj}*"
        return f"/* TODO: FObjectProperty without PropertyClass */ UObject*"
    if short == "FClassProperty":
        return f"/* TODO: FClassProperty -> TSubclassOf<...> */ UClass*"
    if short == "FStructProperty":
        st = prop.get("Struct")
        if isinstance(st, dict):
            obj = st.get("ObjectName") or st.get("Name")
            if obj:
                return f"const F{obj}&"
        return f"/* TODO: FStructProperty without Struct */ struct"
    if short == "FEnumProperty":
        en = prop.get("Enum")
        if isinstance(en, dict):
            obj = en.get("ObjectName") or en.get("Name")
            if obj:
                return f"E{obj}"
        return f"/* TODO: FEnumProperty without Enum */ uint8"
    if short == "FArrayProperty":
        return f"/* TODO: FArrayProperty -> const TArray<...>& */ TArray<int32>"
    return f"/* TODO: unmapped property type {short} */ int32"


def cmd_emit_dispatcher_delegates(args: argparse.Namespace, cfg: Config) -> int:
    """Emit DECLARE_DYNAMIC_MULTICAST_DELEGATE_*Param macros.

    Input: UAssetGUI JSON. Walks every export whose `ObjectName` ends in
    `__DelegateSignature`; reads `LoadedProperties` (or `ChildProperties`)
    for entries flagged `CPF_Parm`; picks the macro by parameter count
    (0..9, UE limit). Output: one macro per dispatcher.

    See migrate-bp.md Rule 3.
    """
    in_path = Path(args.input).resolve()
    if not in_path.exists():
        _die(f"Input not found: {in_path}")
    with open(in_path, encoding="utf-8") as f:
        data = json.load(f)

    exports = data.get("Exports", []) or []
    out_lines: list[str] = []
    found = 0
    for exp in exports:
        if not isinstance(exp, dict):
            continue
        obj_name = str(exp.get("ObjectName", "") or "")
        if "__DelegateSignature" not in obj_name:
            continue
        found += 1
        base = obj_name[: obj_name.index("__DelegateSignature")]
        # Strip optional `_<HashOfHexChars>` suffix UE may append on
        # recompile (8+ uppercase hex). Conservative: only strip when the
        # tail is unambiguously a hash. Otherwise leave the name intact.
        m = re.match(r"^(.*?)(_[0-9A-F]{8,})$", base)
        dispatcher_name = m.group(1) if m else base

        params: list[tuple[str, str]] = []
        prop_list = exp.get("LoadedProperties") or exp.get("ChildProperties") or []
        for cp in prop_list:
            if not isinstance(cp, dict):
                continue
            flags = cp.get("PropertyFlags") or ""
            if "CPF_Parm" not in flags:
                continue
            cpp_type = _resolve_prop_cpp_type(cp)
            pname = cp.get("Name", "?")
            params.append((cpp_type, pname))

        n = len(params)
        if n > 9:
            out_lines.append(
                f"// TODO: dispatcher {dispatcher_name} has {n} parameters;"
                f" UE macro maximum is 9. Manual handling required."
            )
            continue

        macro = _DELEGATE_MACROS[n]
        typedef = f"F{dispatcher_name}"
        if params:
            args_str = ", ".join(f"{t}, {n}" for t, n in params)
            out_lines.append(f"{macro}({typedef}, {args_str});")
        else:
            out_lines.append(f"{macro}({typedef});")

    if not out_lines:
        out_lines.append("// no multicast delegate dispatchers found")

    text = "\n".join(out_lines) + "\n"
    _emit_to_output(text, args.output)
    return 0


# ---------- subcommand: emit-class-flags ----------


_CLASS_FLAG_TO_SPECIFIER: dict = {
    "Abstract":      "Abstract",
    "NotPlaceable":  "NotPlaceable",
    "DefaultConfig": "DefaultConfig",
    "Const":         "Const",
    "Hidden":        "Hidden",
    "Deprecated":    'Deprecated, deprecationMessage="<reason>"',
}


def cmd_emit_class_flags(args: argparse.Namespace, cfg: Config) -> int:
    """Emit a single `UCLASS(<specifiers>)` line from a graph dump.

    Input: DumpBPGraph JSON (top-level `ClassFlags` array of strings).
    Output: one C++ line, e.g. `UCLASS(Abstract, NotPlaceable)`.
    Empty -> `UCLASS()`. See migrate-bp.md Rule 14.
    """
    in_path = Path(args.input).resolve()
    if not in_path.exists():
        _die(f"Input not found: {in_path}")
    with open(in_path, encoding="utf-8") as f:
        data = json.load(f)
    flags = data.get("ClassFlags", [])
    specifiers = []
    for f in flags:
        spec = _CLASS_FLAG_TO_SPECIFIER.get(f)
        if spec is None:
            specifiers.append(f"/* unknown flag: {f} */")
        else:
            specifiers.append(spec)
    line = f"UCLASS({', '.join(specifiers)})" if specifiers else "UCLASS()"
    _emit_to_output(line + "\n", args.output)
    return 0


# ---------- subcommand: emit-component-overrides ----------


def cmd_emit_component_overrides(args: argparse.Namespace, cfg: Config) -> int:
    """Generate deterministic C++ constructor lines from component overrides.

    Input: either a detect-gaps report (top-level `componentsRequired`) or
    a raw DumpBPGraph dump (top-level `Components`). Each component's
    `defaultOverrides` array drives a fixed setter table -- no LLM.

    Output: C++ snippet on stdout, ready to paste into the constructor.
    Unmapped properties become `// TODO:` comments so the user sees the
    gap rather than silently losing state. See migrate-bp.md Rule 12.
    """
    in_path = Path(args.input).resolve()
    if not in_path.exists():
        _die(f"Input not found: {in_path}")
    with open(in_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict) and "componentsRequired" in data:
        comps = data["componentsRequired"]
    elif isinstance(data, dict) and "Components" in data:
        comps = _flatten_components(data["Components"])
    else:
        _die("Input must contain 'componentsRequired' or 'Components'.")

    out_lines: list[str] = []
    truncation_warnings: list[str] = []
    for comp in comps:
        name = comp["name"]
        cls = comp["componentClass"]
        parent = comp.get("parent", "<root>")

        out_lines.append(
            f"{name} = CreateDefaultSubobject<U{cls}>(TEXT(\"{name}\"));"
        )
        # USCS_Node::ParentComponentOrVariableName.ToString() emits "None" for
        # NAME_None -- i.e., a top-level SCS root. Treat both "<root>" and "None"
        # as "no attach parent": the first scene component conventionally
        # becomes RootComponent (see Rule 12).
        is_root = (not parent) or parent in ("<root>", "None")
        if not is_root:
            out_lines.append(f"{name}->SetupAttachment({parent});")
        else:
            out_lines.append(
                f"// TODO: top-level scene component - assign as RootComponent"
                f" if this is the first scene component on the actor."
            )

        for ov in comp.get("defaultOverrides", []):
            # Truncation detection (commandlet caps very large struct exports).
            # If hit, the property cannot be deterministically codegen'd.
            ov_val = ov.get("OurValue", "") or ""
            if ov_val.endswith("..."):
                truncation_warnings.append(
                    f"{name}.{ov.get('Property', '?')} (len={len(ov_val)})"
                )
            out_lines.extend(_emit_override(name, ov))
        out_lines.append("")

    if truncation_warnings:
        out_lines.append(
            "// WARNING: the following overrides were truncated by the dump"
            " commandlet. Re-dump with a larger MaxLen, or apply manually:"
        )
        for w in truncation_warnings:
            out_lines.append(f"//   - {w}")
        out_lines.append("")

    text = "\n".join(out_lines).rstrip() + "\n"
    _emit_to_output(text, args.output)
    return 0


_VEC_RE = re.compile(
    r"X=(-?\d+\.?\d*(?:[eE][+-]?\d+)?),\s*Y=(-?\d+\.?\d*(?:[eE][+-]?\d+)?),"
    r"\s*Z=(-?\d+\.?\d*(?:[eE][+-]?\d+)?)"
)
_ROT_RE = re.compile(
    r"Pitch=(-?\d+\.?\d*(?:[eE][+-]?\d+)?),\s*Yaw=(-?\d+\.?\d*(?:[eE][+-]?\d+)?),"
    r"\s*Roll=(-?\d+\.?\d*(?:[eE][+-]?\d+)?)"
)
_OBJ_SHORT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*'(.+?)'$")
_OBJ_LONG_RE = re.compile(r"^/Script/[A-Za-z0-9_./]+\.[A-Za-z0-9_]+'(.+?)'$")


def _extract_asset_path(s: str) -> Optional[str]:
    """Extract /Game/... path from a UE text-exported object reference.

    Accepts:
      - short form: `StaticMesh'/Game/.../A.A'`
      - long form:  `/Script/Engine.StaticMesh'/Game/.../A.A'`
      - inner-quoted variant: `Class'"/Game/.../A.A"'`
        (UE wraps paths in double quotes when they contain certain
        characters; the apostrophes still bound the reference.)

    Returns None for `None` / empty / unrecognized.
    """
    s = (s or "").strip()
    if not s or s.lower() == "none":
        return None
    m = _OBJ_SHORT_RE.match(s)
    if not m:
        m = _OBJ_LONG_RE.match(s)
    if not m:
        return None
    inner = m.group(1).strip()
    # Strip the inner double-quote wrapper if present:
    # `"/Game/.../A.A"` -> `/Game/.../A.A`
    if len(inner) >= 2 and inner[0] == '"' and inner[-1] == '"':
        inner = inner[1:-1]
    return inner or None


def _split_array(s: str) -> list[str]:
    """Split a UE text-exported array '(a,b,c)' into entries.

    Naive split on commas -- adequate for asset-reference arrays where
    paths use `/`, not for nested-paren structs. Strips outer parens.
    """
    s = (s or "").strip()
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1]
    if not s:
        return []
    return [e.strip() for e in s.split(",")]


def _split_struct_fields(s: str) -> list[tuple[str, str]]:
    """Split a UE text-exported struct '(K=V,K2=(nested),K3="q,uoted")' into [(K,V), ...].

    Tracks paren depth and double-quote state so nested structs and
    quoted strings (which may contain commas) are preserved as a single
    value. Strips outer parens.
    """
    s = (s or "").strip()
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1]
    fields: list[tuple[str, str]] = []
    cur: list[str] = []
    depth = 0
    in_quote = False
    for ch in s:
        if ch == '"':
            in_quote = not in_quote
            cur.append(ch)
        elif in_quote:
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            entry = "".join(cur).strip()
            if "=" in entry:
                k, _, v = entry.partition("=")
                fields.append((k.strip(), v.strip()))
            cur = []
        else:
            cur.append(ch)
    if cur:
        entry = "".join(cur).strip()
        if "=" in entry:
            k, _, v = entry.partition("=")
            fields.append((k.strip(), v.strip()))
    return fields


_COLLISION_RESPONSE_RE = re.compile(
    r'\(\s*Channel\s*=\s*"?([A-Za-z_][A-Za-z0-9_]*)"?\s*,\s*'
    r'Response\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)'
)


def _parse_collision_responses(raw: str) -> list[tuple[str, str]]:
    """Pull per-channel response overrides out of a serialized
    `FCollisionResponse` value. The BP serializer emits something like
    `(ResponseArray=((Channel="Visibility",Response=ECR_Ignore),...))`.
    Returns a list of `(channel_name, response_value)` -- empty if none
    match (the channel default is then used).
    """
    return _COLLISION_RESPONSE_RE.findall(raw or "")


# BodyInstance sub-fields routed through public setters. Direct field
# assignment is not possible for protected members (compile error); these
# entries map the field name to (target, method_call_template). `target`
# is "comp" (UPrimitiveComponent setter) or "body" (FBodyInstance setter).
_BODY_INSTANCE_SETTERS: dict = {
    # PrimitiveComponent-level convenience setters
    "LinearDamping":        ("comp", "SetLinearDamping({val}f)"),
    "AngularDamping":       ("comp", "SetAngularDamping({val}f)"),
    # FBodyInstance public setters that wrap protected members
    "CollisionEnabled":     ("comp", "GetBodyInstance()->SetCollisionEnabled(ECollisionEnabled::{val})"),
    "CollisionProfileName": ("comp", 'GetBodyInstance()->SetCollisionProfileName(FName(TEXT("{val}")))'),
    "ObjectType":           ("comp", "GetBodyInstance()->SetCollisionObjectType({val})"),
    # MassInKgOverride: BP stores the value with bOverrideMass=false (mass override
    # is not active, just the storage carries the value). Pass false here to match.
    "MassInKgOverride":     ("comp", "GetBodyInstance()->SetMassOverride({val}f, false)"),
    "MassScale":            ("comp", "GetBodyInstance()->SetMassScale({val}f)"),
    "bEnableGravity":       ("comp", "GetBodyInstance()->SetEnableGravity({val})"),
    # MaxAngularVelocity: pass bUpdateOverrideMaxAngularVelocity=false to avoid
    # flipping bOverrideMaxAngularVelocity (which BP keeps false on the template).
    "MaxAngularVelocity":   ("comp", "GetBodyInstance()->SetMaxAngularVelocityInRadians(FMath::DegreesToRadians({val}f), false, false)"),
    "bInertiaConditioning": ("comp", "GetBodyInstance()->SetInertiaConditioningEnabled({val})"),
}

# FBodyInstance fields that are `protected` AND lack any public setter -- skip
# silently (setting from C++ would require friend access or a reflection trick).
# In practice these fields fall back to engine defaults at construction.
_BODY_INSTANCE_SKIP: set = {
    "bInterpolateWhenSubStepping",
    "bOverrideMaxDepenetrationVelocity",
    "bOverrideWalkableSlopeOnInstance",
    "bPendingCollisionProfileSetup",
}


def _emit_body_instance(comp_name: str, val: str) -> list[str]:
    """Walk every BodyInstance sub-field deterministically.

    Routing per sub-field (in priority order):
      1. `_BODY_INSTANCE_SETTERS` entries -> proper setter call (handles
         protected member fields whose direct assignment would not compile).
      2. Nested-struct CollisionResponses -> per-channel
         `Comp->SetCollisionResponseToChannel(channel, response)` calls
         expanded out (the wrapped FCollisionResponse is protected, but
         UPrimitiveComponent exposes a virtual setter).
      3. Simple scalar (bool / number / quoted FName / FVector tuple)
         -> direct `Comp->BodyInstance.<Key> = <literal>;` (only valid for
         public members; if compile fails the field belongs in #1 above).
      4. Anything else -> explicit `// TODO:` so nothing is silently lost.
    """
    out: list[str] = []
    for key, raw in _split_struct_fields(val):
        # 0. Skip protected fields without a public setter (engine default holds).
        if key in _BODY_INSTANCE_SKIP:
            continue
        # 1. Known setter takes precedence.
        if key in _BODY_INSTANCE_SETTERS:
            target, tpl = _BODY_INSTANCE_SETTERS[key]
            unq = raw.strip().strip('"')
            # BP serialization uses True/False; convert to C++ bool literals.
            if unq == "True":  unq = "true"
            elif unq == "False": unq = "false"
            assert target == "comp"
            out.append(f"{comp_name}->{tpl.format(val=unq)};")
            continue

        # 2. CollisionResponses: nested struct of per-channel responses.
        # Expand to per-channel SetCollisionResponseToChannel() calls so
        # the BP's per-channel overrides (e.g. Visibility=Ignore) survive.
        # ch_resp is the serialized enum value already (e.g. "ECR_Ignore"),
        # so emit verbatim without prefixing.
        if key == "CollisionResponses":
            for ch_name, ch_resp in _parse_collision_responses(raw):
                out.append(
                    f"{comp_name}->SetCollisionResponseToChannel("
                    f"ECollisionChannel::ECC_{ch_name}, {ch_resp});"
                )
            continue

        # 2. Simple scalars -> direct field assignment.
        # Bool: True/False
        if raw in ("True", "False"):
            b = "true" if raw == "True" else "false"
            out.append(f"{comp_name}->BodyInstance.{key} = {b};")
            continue
        # Number (int, float, scientific notation)
        if re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", raw):
            suffix = "f" if "." in raw or "e" in raw.lower() else ""
            out.append(f"{comp_name}->BodyInstance.{key} = {raw}{suffix};")
            continue
        # Quoted FName / FString
        m = re.fullmatch(r'"([^"]*)"', raw)
        if m:
            inner = m.group(1)
            out.append(
                f'{comp_name}->BodyInstance.{key} = FName(TEXT("{inner}"));'
            )
            continue
        # FVector tuple (X=,Y=,Z=)
        m = _VEC_RE.search(raw)
        if m and raw.startswith("("):
            x, y, z = m.groups()
            out.append(
                f"{comp_name}->BodyInstance.{key} = FVector({x}f, {y}f, {z}f);"
            )
            continue
        # Bare enum identifier (e.g. ECC_WorldStatic)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", raw):
            out.append(f"{comp_name}->BodyInstance.{key} = {raw};")
            continue
        # Empty struct () -> default; skip.
        if raw == "()":
            continue

        # 3. Anything else (nested struct, etc.) -> explicit TODO.
        excerpt = raw[:80].replace("\n", " ")
        out.append(
            f"// TODO: apply BodyInstance.{key} = {excerpt}"
        )
    return out


# Properties that exist on component templates but have no runtime effect.
# UE writes these to the BP for editor-only / version-tracking reasons; a
# C++ class doesn't need to set them, and emitting `// TODO:` for them is
# noise. Keep this list conservative -- only proven-irrelevant keys.
_NOISE_PROPERTIES: set[str] = {
    "StaticMeshImportVersion",     # asset-cook bookkeeping, no runtime read
    "bVisualizeComponent",         # editor billboard toggle
    "bShouldUpdatePhysicsVolume",  # implicit from physics setup
}


def _emit_override(comp_name: str, ov: dict) -> list[str]:
    """Translate one DefaultOverrides entry to C++ lines (Rule 12 table)."""
    prop = ov.get("Property", "")
    type_ = ov.get("Type", "")
    val = ov.get("OurValue", "") or ""

    if prop in _NOISE_PROPERTIES:
        return []  # silently skip -- known editor/transient noise

    # Special-case OverrideMaterials (variadic SetMaterial(i, ...)) and
    # BodyInstance (deep struct walker) — they can't fit the table schema.
    if prop == "OverrideMaterials":
        out: list[str] = []
        for i, e in enumerate(_split_array(val)):
            if not e or e.lower() == "none":
                continue
            path = _extract_asset_path(e)
            if path:
                out.append(
                    f"{comp_name}->SetMaterial({i}, LoadObject<UMaterialInterface>("
                    f"nullptr, TEXT(\"{path}\")));"
                )
        if out:
            return out
    if prop == "BodyInstance":
        return _emit_body_instance(comp_name, val)

    # Table-driven dispatch. Add a new property -> setter mapping by adding
    # one line to COMPONENT_SETTER_TABLE below; no code changes here.
    spec = COMPONENT_SETTER_TABLE.get(prop)
    if spec is not None:
        emitted = _dispatch_component_setter(comp_name, val, spec)
        if emitted:
            return emitted

    # Unmapped -- surface as TODO (NEVER silent). Rule 12 forbids omission.
    excerpt = val[:80].replace("\n", " ")
    return [
        f"// TODO: apply override Property={prop} Type={type_} Value={excerpt}"
    ]


# ============================================================================
# COMPONENT_SETTER_TABLE -- Adding a new component class is one entry here.
#
# Each entry maps a Blueprint property name (the LHS of a SCS DefaultOverrides
# row) to a setter spec. The dispatcher reads `kind` and applies the matching
# value parser + code template:
#
#   "asset"   -> Comp->{setter}(LoadObject<{type}>(nullptr, TEXT("/Game/...")))
#   "class"   -> Comp->{setter}(LoadClass<{type}>(nullptr, TEXT("/Game/...")))
#   "bool"    -> Comp->{setter}(true|false)
#   "float"   -> Comp->{setter}(N.NNNf)
#   "int"     -> Comp->{setter}(N)
#   "vector"  -> Comp->{setter}(FVector(x,y,z))
#   "rotator" -> Comp->{setter}(FRotator(p,y,r))
#   "vec2d"   -> Comp->{setter}(FVector2D(x,y))
#   "enum"    -> Comp->{setter}({enumType}::{Value})
#
# To support a new property: add one line. To support a new component class:
# add the property names that class exposes. Verify the setter exists with
# `bpmigrate dump-class-reflection /Script/Module.YourComponent`.
# Properties NOT in this table fall through to a `// TODO:` comment so
# silent omission is impossible (Rule 12).
# ============================================================================

COMPONENT_SETTER_TABLE: dict = {
    # --- Transform (USceneComponent) ---
    "RelativeLocation": {"kind": "vector",  "setter": "SetRelativeLocation"},
    # _Direct preserves the exact FRotator triple. The non-_Direct variant
    # round-trips through FQuat and CAN re-normalize (e.g. Yaw=-90 snapping
    # to ~-26.565 in gimbal-lock-adjacent values), diverging from BP CDO.
    "RelativeRotation": {"kind": "rotator", "setter": "SetRelativeRotation_Direct"},
    "RelativeScale3D":  {"kind": "vector",  "setter": "SetRelativeScale3D"},

    # --- Mesh (Static / Skeletal) ---
    "StaticMesh":         {"kind": "asset", "setter": "SetStaticMesh",   "type": "UStaticMesh"},
    "SkeletalMesh":       {"kind": "asset", "setter": "SetSkeletalMesh", "type": "USkeletalMesh"},
    "SkeletalMeshAsset":  {"kind": "asset", "setter": "SetSkeletalMesh", "type": "USkeletalMesh"},

    # --- Audio (UAudioComponent) ---
    "Sound":            {"kind": "asset", "setter": "SetSound",            "type": "USoundBase"},
    "VolumeMultiplier": {"kind": "float", "setter": "SetVolumeMultiplier"},
    "PitchMultiplier":  {"kind": "float", "setter": "SetPitchMultiplier"},

    # --- Widget (UWidgetComponent) ---
    "WidgetClass":         {"kind": "class", "setter": "SetWidgetClass", "type": "UUserWidget"},
    "DrawSize":            {"kind": "vec2d", "setter": "SetDrawSize"},
    "WidgetSpace":         {"kind": "enum",  "setter": "SetWidgetSpace", "enumType": "EWidgetSpace"},
    "bDrawAtDesiredSize":  {"kind": "bool",  "setter": "SetDrawAtDesiredSize"},

    # --- Niagara (UNiagaraComponent) ---
    "Asset": {"kind": "asset", "setter": "SetAsset", "type": "UNiagaraSystem"},

    # --- Particle System (UParticleSystemComponent) ---
    "Template": {"kind": "asset", "setter": "SetTemplate", "type": "UParticleSystem"},

    # --- Decal (UDecalComponent) ---
    "DecalMaterial": {"kind": "asset", "setter": "SetDecalMaterial", "type": "UMaterialInterface"},
    "SortOrder":     {"kind": "int",   "setter": "SetSortOrder"},

    # --- Visibility / shadow / mobility (USceneComponent + UPrimitiveComponent) ---
    "bVisible":               {"kind": "bool", "setter": "SetVisibility"},
    "bHiddenInGame":          {"kind": "bool", "setter": "SetHiddenInGame"},
    "CastShadow":             {"kind": "bool", "setter": "SetCastShadow"},
    "bCastShadow":            {"kind": "bool", "setter": "SetCastShadow"},
    "bGenerateOverlapEvents": {"kind": "bool", "setter": "SetGenerateOverlapEvents"},
    "Mobility":               {"kind": "enum", "setter": "SetMobility", "enumType": "EComponentMobility"},

    # --- Common UActorComponent ---
    "bAutoActivate": {"kind": "bool", "setter": "SetAutoActivate"},
}


def _dispatch_component_setter(comp_name: str, val: str, spec: dict) -> list[str]:
    """Apply a COMPONENT_SETTER_TABLE entry to a value string. Returns [] if
    the value can't be parsed in the spec's `kind` (caller falls back to TODO).
    """
    kind = spec.get("kind")
    setter = spec.get("setter", "?")

    if kind == "asset":
        path = _extract_asset_path(val)
        if path:
            return [f'{comp_name}->{setter}(LoadObject<{spec["type"]}>(nullptr, TEXT("{path}")));']
        return []

    if kind == "class":
        path = _extract_asset_path(val)
        if path:
            return [f'{comp_name}->{setter}(LoadClass<{spec["type"]}>(nullptr, TEXT("{path}")));']
        return []

    if kind == "bool":
        b = "true" if val.strip().lower() in ("true", "1") else "false"
        return [f"{comp_name}->{setter}({b});"]

    if kind == "float":
        n = val.strip().rstrip("f")
        return [f"{comp_name}->{setter}({n}f);"]

    if kind == "int":
        return [f"{comp_name}->{setter}({val.strip()});"]

    if kind == "vector":
        m = _VEC_RE.search(val)
        if m:
            x, y, z = m.groups()
            return [f"{comp_name}->{setter}(FVector({x}f, {y}f, {z}f));"]
        return []

    if kind == "rotator":
        m = _ROT_RE.search(val)
        if m:
            p, y, r = m.groups()
            return [f"{comp_name}->{setter}(FRotator({p}f, {y}f, {r}f));"]
        return []

    if kind == "vec2d":
        m = re.search(r"X=([-\d\.eE]+),\s*Y=([-\d\.eE]+)", val)
        if m:
            x, y = m.groups()
            return [f"{comp_name}->{setter}(FVector2D({x}f, {y}f));"]
        return []

    if kind == "enum":
        return [f'{comp_name}->{setter}({spec["enumType"]}::{val.strip()});']

    return []


def _detect_unaudited_k2nodes(graph_data: dict) -> list[dict]:
    """Catch the silent failure mode: a K2Node kind whose commandlet branch
    forgot to emit `Resolved`. Without this audit, `_detect_broken_references`
    would silently skip such nodes (`if "Resolved" not in node: continue`),
    producing a false-clean `brokenReferences=[]` even when the BP is broken.

    Returns a per-class summary of K2Nodes that emit no `Resolved` field.
    The list is intentionally not flagged as broken (we don't know that
    they ARE broken) -- it's an audit signal that the toolchain coverage
    needs to grow. If you see your project's K2Node here, the fix is to
    add the `else if (auto* X = Cast<UK2Node_Foo>(Node))` branch in
    DumpBPGraphCommandlet.cpp (see CONTRIBUTING.md).

    Skipped (intentionally not audited):
      - K2Node_Knot, K2Node_IfThenElse, K2Node_FunctionEntry,
        K2Node_FunctionResult, K2Node_CommutativeAssociativeBinaryOperator,
        K2Node_MakeArray, K2Node_MakeStruct etc. -- self-contained, no
        external member reference to resolve.
    """
    SKIP_CLASSES: set = {
        "K2Node_Knot",
        "K2Node_IfThenElse",
        "K2Node_FunctionEntry",
        "K2Node_FunctionResult",
        "K2Node_CommutativeAssociativeBinaryOperator",
        "K2Node_MakeArray",
        "K2Node_MakeStruct",
        "K2Node_MakeMap",
        "K2Node_MakeSet",
        "K2Node_BreakStruct",
        "K2Node_Self",
        "K2Node_Literal",
        "K2Node_TemporaryVariable",
        "K2Node_Composite",
        "K2Node_Tunnel",
        "K2Node_Timeline",
        "EdGraphNode_Comment",
        "K2Node_PromotableOperator",
        "K2Node_Select",
        "K2Node_GetArrayItem",
    }
    counts: dict = {}
    examples: dict = {}
    for graph in graph_data.get("Graphs", []) or []:
        for node in graph.get("Nodes", []) or []:
            ncls = node.get("Class", "")
            if not ncls.startswith("K2Node_"):
                continue
            if ncls in SKIP_CLASSES:
                continue
            if "Resolved" in node:
                continue
            counts[ncls] = counts.get(ncls, 0) + 1
            if ncls not in examples:
                examples[ncls] = {
                    "graph": graph.get("Name", "?"),
                    "title": node.get("CompactTitle") or node.get("Title", "?"),
                }
    return [
        {
            "nodeClass": cls,
            "count": counts[cls],
            "exampleGraph": examples[cls]["graph"],
            "exampleTitle": examples[cls]["title"],
            "action": (
                "Add an `else if (auto* X = Cast<" + cls + ">(Node))` branch"
                " in DumpBPGraphCommandlet.cpp that emits `Resolved` +"
                " `UnresolvedReason`. See CONTRIBUTING.md."
            ),
        }
        for cls in sorted(counts)
    ]


# ============================================================================
# REF_KIND_REGISTRY -- single source of truth for refKinds across the toolchain.
#
# Adding a new refKind: add ONE entry here. detect-gaps / map-broken-refs /
# apply-fix-mapping all derive their behavior from this registry, so previous
# 8-site drift is impossible. To support a new K2Node kind that maps to an
# existing refKind, just add its class name to `k2node_classes`. To support a
# new kind of reference (e.g. a new K2Node that resolves an Enum), add a new
# refKind entry with all 4 fields below.
#
# Fields:
#   k2node_classes      -- list of `K2Node_*` class names that emit this refKind
#                          (consumed by `_detect_broken_references` to label
#                          each broken node).
#   suggested_action    -- short hint shown in detect-gaps output.
#   member_field_names  -- ordered list of node JSON field names to try when
#                          extracting the member name (first non-empty wins).
#                          Lets the commandlet emit kind-specific fields like
#                          `DelegateName` or `EnumName` without changing the
#                          extraction code.
#   instruction_label   -- short noun used by `_instruction_for_mapping_entry`
#                          for the `editorAction` instruction text, e.g.
#                          "Function 'X' on 'Y'". Set to None for kinds that
#                          never produce an editorAction (e.g. `castTarget`).
# ============================================================================

_DEFAULT_MEMBER_FIELDS: list[str] = [
    "FunctionName",
    "VariableName",
    "EventName",
    "MacroName",
    "TargetType",
    "DelegateName",
    "ProxyFunction",
]

REF_KIND_REGISTRY: dict = {
    "function": {
        "k2node_classes": ["K2Node_CallFunction"],
        "suggested_action": (
            "auto-map if a same-named member of the same kind exists on the new"
            " parent class; else require explicit user mapping"
        ),
        "member_field_names": ["FunctionName"],
        "instruction_label": "Function",
    },
    "variable": {
        "k2node_classes": ["K2Node_VariableGet", "K2Node_VariableSet"],
        "suggested_action": (
            "auto-map if a same-named member of the same kind exists on the new"
            " parent class; else require explicit user mapping"
        ),
        "member_field_names": ["VariableName"],
        "instruction_label": "Variable",
    },
    "castTarget": {
        "k2node_classes": ["K2Node_DynamicCast"],
        "suggested_action": (
            "Cast target deleted -- the user must supply the replacement class"
            " or remove the Cast"
        ),
        "member_field_names": ["TargetType"],
        "instruction_label": None,  # always reject -> noFix template
    },
    "macro": {
        "k2node_classes": ["K2Node_MacroInstance"],
        "suggested_action": (
            "macro library moved or removed -- repoint MacroInstance or inline"
            " the macro contents"
        ),
        "member_field_names": ["MacroName"],
        "instruction_label": "Macro",
    },
    "eventOverride": {
        "k2node_classes": ["K2Node_Event"],
        "suggested_action": (
            "override event removed from parent -- delete the node or rebind to"
            " a still-present override"
        ),
        "member_field_names": ["EventName"],
        "instruction_label": "Override event",
    },
    "delegate": {
        "k2node_classes": [
            "K2Node_AddDelegate",
            "K2Node_RemoveDelegate",
            "K2Node_ClearDelegate",
            "K2Node_CallDelegate",
            "K2Node_AssignDelegate",
        ],
        "suggested_action": (
            "multicast delegate (event dispatcher) missing on new parent;"
            " auto-map if same-named MulticastDelegate exists, else require user mapping"
        ),
        "member_field_names": ["DelegateName"],
        "instruction_label": "Delegate",
    },
    "createDelegate": {
        "k2node_classes": ["K2Node_CreateDelegate"],
        "suggested_action": (
            "CreateDelegate target function unresolved; user must rebind to a"
            " still-present function with matching signature"
        ),
        "member_field_names": ["FunctionName"],
        "instruction_label": "CreateDelegate target function",
    },
    "asyncTask": {
        "k2node_classes": ["K2Node_BaseAsyncTask"],
        "suggested_action": (
            "async / latent proxy class or factory function missing;"
            " user must update to a still-present AsyncAction"
        ),
        "member_field_names": ["ProxyFunction"],
        "instruction_label": None,  # always user_required -> deferredToUser
    },
}

# Derived dicts used by the rest of the file.
_REF_KIND_FOR_CLASS: dict = {
    cls: kind
    for kind, spec in REF_KIND_REGISTRY.items()
    for cls in spec["k2node_classes"]
}


def _detect_broken_references(graph_data: dict) -> list[dict]:
    """Scan a DumpBPGraph dump for K2Nodes whose member reference fails to resolve.

    Relies on the commandlet emitting `Resolved: false` + `UnresolvedReason`
    on `K2Node_CallFunction` / `VariableGet` / `VariableSet` / `DynamicCast`
    / `Event` / `MacroInstance`. These are exactly the nodes the editor
    would draw red after a parent rename, a reparent onto a base that
    dropped a member, or a deleted Cast target.

    Returns one entry per broken node with:
      - graph, node (title), nodeClass, nodeGuid
      - refKind: "function" | "variable" | "castTarget" | "macro" | "eventOverride"
      - memberName: the name being looked up
      - oldParent: the class the BP currently resolves the parent against
        (often the pre-reparent class, surfacing the reparent diff)
      - reason: the commandlet's UnresolvedReason text
      - suggestedAction: short hint for Layer 2 auto-mapping
    """
    out: list = []
    for graph in graph_data.get("Graphs", []) or []:
        gname = graph.get("Name", "?")
        for node in graph.get("Nodes", []) or []:
            # Resolved is only emitted by the K2Node branches that perform the
            # check; absence means "not applicable" (e.g. Branch nodes).
            # Untracked K2Nodes are reported in `unauditedK2Nodes` separately.
            if "Resolved" not in node:
                continue
            if node.get("Resolved"):
                continue
            ncls = node.get("Class", "?")
            kind = _REF_KIND_FOR_CLASS.get(ncls, "unknown")
            spec = REF_KIND_REGISTRY.get(kind, {})
            # Pull member name from the kind's preferred fields, falling back
            # to the generic field set (covers brand-new K2Nodes whose
            # registry entry isn't kind-specific yet).
            field_order = spec.get("member_field_names") or _DEFAULT_MEMBER_FIELDS
            member = "?"
            for f in field_order + _DEFAULT_MEMBER_FIELDS:
                v = node.get(f)
                if v:
                    member = v
                    break
            entry = {
                "graph": gname,
                "node": node.get("CompactTitle") or node.get("Title", "?"),
                "nodeClass": ncls,
                "nodeGuid": node.get("Guid", ""),
                "refKind": kind,
                "memberName": member,
                "oldParent": node.get("TargetClass", ""),
                "reason": node.get("UnresolvedReason", ""),
                "suggestedAction": spec.get("suggested_action", "") if spec else "",
            }
            out.append(entry)
    return out


def _detect_orphan_pins(graph_data: dict) -> tuple[list[dict], list[dict]]:
    """Scan a DumpBPGraph dump for orphan pins and silently-dead data inputs.

    Returns (orphan_pins, unconnected_data_inputs).

    - orphan_pins: any pin with `bOrphanedPin: true` (UE flag for pins
      whose target was renamed/removed).
    - unconnected_data_inputs: data input pins (non-exec, non-delegate,
      non-self) that have no LinkedTo connection AND no DefaultValue /
      DefaultObject. These are silent zero-valued inputs that production
      code likely did not intend.
    """
    orphans: list = []
    dead_inputs: list = []

    skip_pin_names = {"self", "Then", "Execute", "execute"}
    skip_pin_types = {"exec", "delegate"}

    for graph in graph_data.get("Graphs", []):
        gname = graph.get("Name", "?")
        for node in graph.get("Nodes", []):
            ncls = node.get("Class", "?")
            ntitle = node.get("CompactTitle") or node.get("Title", "?")
            nguid = node.get("Guid", "")

            for pin in node.get("Pins", []):
                pname = pin.get("Name", "?")
                ptype = pin.get("Type", "?")
                pdir = pin.get("Direction", "")
                phidden = pin.get("Hidden", False)
                porphan = pin.get("Orphan", False)
                padv = pin.get("Advanced", False)

                if porphan:
                    orphans.append({
                        "graph": gname,
                        "node": ntitle,
                        "nodeClass": ncls,
                        "nodeGuid": nguid,
                        "pin": pname,
                        "type": ptype,
                        "direction": pdir,
                    })

                # Silent dead data input detection
                if (pdir == "Input"
                        and ptype not in skip_pin_types
                        and pname not in skip_pin_names
                        and not phidden):
                    links = pin.get("LinkedTo", [])
                    default_value = pin.get("DefaultValue", "")
                    default_object = pin.get("DefaultObject", "")
                    autogen = pin.get("AutoDefaultValue", "")
                    has_default = bool(default_value or default_object or autogen)
                    if not links and not has_default:
                        dead_inputs.append({
                            "graph": gname,
                            "node": ntitle,
                            "nodeClass": ncls,
                            "nodeGuid": nguid,
                            "pin": pname,
                            "type": ptype,
                            "advanced": padv,
                        })

    return orphans, dead_inputs


def _detect_missing_funcs(data: dict) -> list[str]:
    """Functions referenced in FunctionGraphs but missing from FunctionExport."""
    exports = data.get("Exports", [])
    func_graph_names: set = set()
    fe_names: set = set()
    for exp in exports:
        if not isinstance(exp, dict) or "Data" not in exp:
            continue
        for d in exp.get("Data", []):
            if isinstance(d, dict) and d.get("Name") == "FunctionGraphs":
                for entry in d.get("Value", []):
                    if isinstance(entry, dict):
                        idx = entry.get("Value", 0)
                        if 0 <= idx < len(exports):
                            func_graph_names.add(exports[idx].get("ObjectName", ""))
    for exp in exports:
        if "FunctionExport" in exp.get("$type", ""):
            fe_names.add(exp.get("ObjectName", ""))

    missing = func_graph_names - fe_names - {"EventGraph"}
    return sorted(n for n in missing if n and not n.startswith("EdGraphNode_Comment"))


def _detect_cook_refs(
    data: dict,
    summary_text: Optional[str] = None,
    graph_data: Optional[dict] = None,
) -> list[dict]:
    """Variables that look like cook-only reference defaults.

    Three criteria, all must hold (per migrate-bp.md Step 2-C-1):

    1. Variable is never read or written anywhere in the BP's graph
       (no occurrences in the summarizer text outside the
       Variables section).
    2. Variable type is one of TSubclassOf, TSoftClassPtr,
       TSoftObjectPtr, or a UObject reference.
    3. Default value is a non-empty class/object path (not False/True/
       0/None/nullptr).

    These variables exist solely to keep their default value's package
    in the BP's import table for cook-walker tracking. Deleting them
    silently breaks packaged-build dependencies that editor/PIE never
    catch.

    Best results require both UAssetGUI JSON (for the CDO override
    default) AND the DumpBPGraph dump (for resolved Type/SubType).
    The summarizer text adds the usage filter (criterion 1).
    Without graph data, falls back to UAssetGUI Variables which
    misses class/soft-class details (the VarType blob is base64).
    """
    suspects: list = []
    REF_PIN_CATEGORIES = {"object", "class", "softobject", "softclass"}
    EMPTY_DEFAULTS = {"", "False", "True", "0", "None", "nullptr", None}

    # CDO override map (priority 1 default source per recipe rule 10).
    cdo_overrides: dict = {}
    for exp in data.get("Exports", []) or []:
        if not isinstance(exp, dict):
            continue
        if exp.get("ObjectName", "").startswith("Default__"):
            for d in exp.get("Data") or []:
                if isinstance(d, dict):
                    cdo_overrides[d.get("Name")] = d.get("Value")
            break

    # Variable list with type info: prefer graph dump (clean Type/SubType),
    # fall back to UAssetGUI NewVariables (VarType is base64 -> SubType
    # unavailable without decoding).
    var_records: list = []
    if graph_data and graph_data.get("Variables"):
        for v in graph_data["Variables"]:
            var_records.append({
                "name": v.get("Name"),
                "pinCategory": str(v.get("Type", "")).lower(),
                "subCategoryObject": v.get("SubType", ""),
                "newVarDefault": v.get("DefaultValue", ""),
            })
    else:
        for exp in data.get("Exports", []) or []:
            if not isinstance(exp, dict):
                continue
            for d in exp.get("Data") or []:
                if not isinstance(d, dict) or d.get("Name") != "NewVariables":
                    continue
                for entry in d.get("Value") or []:
                    if not isinstance(entry, dict):
                        continue
                    fields = {
                        x.get("Name"): x
                        for x in entry.get("Value", [])
                        if isinstance(x, dict)
                    }
                    var_records.append({
                        "name": fields.get("VarName", {}).get("Value"),
                        # VarType is a base64 EdGraphPinType blob -- cannot
                        # cheaply decode pin category here, so skip filter.
                        "pinCategory": "",
                        "subCategoryObject": "",
                        "newVarDefault": fields.get("DefaultValue", {}).get("Value"),
                    })

    # Body of summary text after the Variables section, for usage check.
    body_for_usage = ""
    if summary_text:
        m = re.search(
            r"--- Variables \(\d+\) ---\n(.*?)(?:\n---|\Z)",
            summary_text,
            re.DOTALL,
        )
        body_for_usage = summary_text[m.end():] if m else summary_text

    for v in var_records:
        name = v["name"]
        if not name:
            continue
        pin_cat = v["pinCategory"]
        # When graph data is unavailable, pin_cat is empty -- can't filter
        # criterion 2. We then return all vars with non-empty defaults and
        # mark them for manual review.
        type_check_possible = bool(pin_cat)
        if type_check_possible and pin_cat not in REF_PIN_CATEGORIES:
            continue

        # Resolve effective default: CDO override beats NewVariable.DefaultValue.
        effective = cdo_overrides.get(name)
        origin = "CDO override"
        if effective is None or effective in EMPTY_DEFAULTS:
            effective = v["newVarDefault"]
            origin = "NewVariable.DefaultValue"
        if effective in EMPTY_DEFAULTS:
            continue

        eff_str = str(effective)
        if eff_str in EMPTY_DEFAULTS or len(eff_str) < 2:
            continue

        # Heuristic: cook-ref defaults are always class/object paths
        # containing '/' or '.'. Filter out flat names that are unlikely
        # to be package paths.
        if "/" not in eff_str and "." not in eff_str:
            continue

        suspect: dict = {
            "name": name,
            "pinCategory": pin_cat or "<unknown -- supply --graph for type filter>",
            "subCategoryObject": v["subCategoryObject"],
            "default": eff_str,
            "defaultOrigin": origin,
        }

        if summary_text:
            if re.search(rf"\b{re.escape(name)}\b", body_for_usage):
                continue  # Variable IS used in graph -> not a cook-ref
            suspect["usageUnchecked"] = False
        else:
            suspect["usageUnchecked"] = True

        if not type_check_possible:
            suspect["typeCheckSkipped"] = True

        suspects.append(suspect)

    return suspects


def _detect_refs_body_gap(summary_text: str) -> list[str]:
    """C++ Function References that do not appear in the pseudocode body."""
    m = re.search(
        r"--- C\+\+ Function References ---\n(.*?)(?:\n---|\Z)", summary_text, re.DOTALL
    )
    if not m:
        return []
    refs: set = set()
    for line in m.group(1).splitlines():
        line = line.strip()
        if "::" in line:
            refs.add(line)

    body = summary_text[: m.start()]
    gaps: list = []
    for r in refs:
        cls, fn = r.split("::", 1)
        patterns = [
            r,
            fn + "(",
            "." + fn + "(",
            fn + "_result",
            fn + "_ReturnValue",
            fn + "_Output",
        ]
        if not any(p in body for p in patterns):
            gaps.append(r)
    return sorted(gaps)


# ---------- subcommand: find-bp ----------


def cmd_find_bp(args: argparse.Namespace, cfg: Config) -> int:
    root = cfg.require_content_root()
    target = args.name
    if not target.endswith(".uasset"):
        target = target + ".uasset"

    matches: list = []
    for p in root.rglob(target):
        if p.is_file():
            matches.append(p)

    if not matches:
        _die(f"No match for '{args.name}' under {root}", code=1)
    if len(matches) > 1 and not args.all:
        print("Multiple matches; pass --all or disambiguate by path:", file=sys.stderr)
        for p in matches:
            print(f"  {p}", file=sys.stderr)
        return 1
    for p in matches:
        print(str(p))
    return 0


# ---------- subcommand: inspect ----------


def cmd_inspect(args: argparse.Namespace, cfg: Config) -> int:
    """Full read-only inspection pipeline."""
    # 1. Resolve target .uasset
    src = _resolve_target_uasset(args.target, cfg)
    print(f"[inspect] target: {src}", file=sys.stderr)

    # 2. UAssetGUI tojson
    uagui = cfg.require_uassetgui()
    json_out = cfg.tmpdir / (src.stem + ".json")
    staged = cfg.tmpdir / src.name
    _stage_writable(src, staged)
    res = _run([str(uagui), "tojson", str(staged), str(json_out), cfg.ue_version])
    if res.returncode != 0:
        _die(f"UAssetGUI tojson failed (exit {res.returncode})", code=res.returncode)

    # 3. Summarize
    summary_out = cfg.tmpdir / (src.stem + ".txt")
    summarize_args = argparse.Namespace(
        json_path=str(json_out),
        bytecode=True,
        max_lines=0,
        output=str(summary_out),
    )
    cmd_summarize(summarize_args, cfg)

    # 4. Detect gaps. cmd_detect_gaps reads `graph` and `output` off the
    # namespace; supply None so getattr() / explicit checks succeed.
    gaps_args = argparse.Namespace(
        json_path=str(json_out),
        summary_text=str(summary_out),
        graph=None,
        output=None,
    )
    print("\n--- Inspection report ---")
    print(f"JSON:    {json_out}")
    print(f"Summary: {summary_out}")
    print()
    cmd_detect_gaps(gaps_args, cfg)

    if args.raw:
        print("\n--- summarizer raw output ---")
        print(summary_out.read_text(encoding="utf-8"))
    return 0


def _resolve_target_uasset(target: str, cfg: Config) -> Path:
    """Accept either an absolute .uasset path or a Blueprint name (resolved via Content/)."""
    p = Path(target)
    if p.suffix == ".uasset" and p.is_absolute():
        if not p.exists():
            _die(f"File not found: {p}")
        return p
    # Treat as BP name; search content root.
    root = cfg.require_content_root()
    name = target if target.endswith(".uasset") else target + ".uasset"
    matches = list(root.rglob(name))
    if not matches:
        _die(f"No Blueprint named '{target}' under {root}")
    if len(matches) > 1:
        print(f"Multiple matches for '{target}':", file=sys.stderr)
        for m in matches:
            print(f"  {m}", file=sys.stderr)
        _die("Disambiguate by passing the absolute .uasset path.")
    return matches[0]


# ---------- subcommand: dump-graph ----------


def cmd_dump_graph(args: argparse.Namespace, cfg: Config) -> int:
    rc = _run_commandlet(
        cfg,
        "DumpBPGraph",
        positional=[args.game_path],
        switches={"output": args.output} if args.output else {},
    )
    if rc == 0:
        # Surface the resulting path on stderr so it's not buried in
        # the editor's ~80-line shutdown log. The commandlet writes to
        # `<output>` if supplied, else `%TEMP%/migrate-bp/<asset>_graph.json`.
        if args.output:
            print(f"Graph: {args.output}", file=sys.stderr)
        else:
            asset = args.game_path.rstrip("/").split("/")[-1]
            default = Path(tempfile.gettempdir()) / "migrate-bp" / f"{asset}_graph.json"
            print(f"Graph: {default}", file=sys.stderr)
    return rc


# ---------- subcommand: apply-fix-mapping ----------


def _instruction_for_mapping_entry(entry: dict, new_parent_path: str) -> dict:
    """Translate one mapping entry into an instruction (status-only contract).

    Reads ONLY `status`, `refKind`, `oldMember`, `oldParent`, `newMember`,
    `newParent`, `candidates`, `nodeGuid`, `graph`, `node` from the mapping
    entry. `rationale` is passed through verbatim. This isolation guarantees
    that future Layer 2 enhancements (e.g. richer rationale text) do not
    change instruction shape -- backends and downstream consumers only need
    to react to `kind` and `action`.
    """
    base = {
        "nodeGuid": entry.get("nodeGuid"),
        "graph": entry.get("graph"),
        "node": entry.get("node"),
        "refKind": entry.get("refKind"),
        "rationale": entry.get("rationale", ""),
    }
    status = entry.get("status")
    ref_kind = entry.get("refKind")
    old_member = entry.get("oldMember", "?")
    old_parent = entry.get("oldParent") or "(unresolved)"

    if status == "auto":
        new_member = entry.get("newMember", "?")
        new_parent = entry.get("newParent", new_parent_path or "?")
        # Pull the noun ("Function", "Variable", "Delegate", ...) from the
        # registry instead of an if/elif chain. Falls back to a plain
        # quoted name when the refKind doesn't define an instruction label
        # (intentionally None for kinds that never produce editorAction).
        spec = REF_KIND_REGISTRY.get(ref_kind, {})
        label = spec.get("instruction_label")
        if label:
            replace_target = f"{label} '{new_member}' on '{new_parent}'"
        else:
            replace_target = f"'{new_member}' on '{new_parent}'"
        return {
            **base,
            "kind": "editorAction",
            "action": "rebind",
            "fromMember": old_member,
            "fromParent": old_parent,
            "toMember": new_member,
            "toParent": new_parent,
            "editorSteps": [
                f"Open the BP in the Blueprint editor.",
                f"In graph '{base['graph']}', locate the node '{base['node']}' (Guid {base['nodeGuid']}).",
                f"Right-click the node -> Replace References -> {replace_target}.",
                "Compile (F7) -> Save (Ctrl+S).",
            ],
        }

    if status == "reject":
        return {
            **base,
            "kind": "noFix",
            "fromMember": old_member,
            "fromParent": old_parent,
            "editorSteps": [
                f"Open the BP and locate node '{base['node']}' (Guid {base['nodeGuid']}) in graph '{base['graph']}'.",
                "This reference cannot be deterministically remapped (semantic deletion).",
                "Decide manually: (a) supply a replacement class/member, or (b) delete the node and rewire callers.",
                "After resolving, re-run `bpmigrate detect-gaps` to confirm no broken references remain.",
            ],
        }

    # status == "user_required" (or unknown -> treat as deferred)
    candidates = entry.get("candidates") or []
    return {
        **base,
        "kind": "deferredToUser",
        "fromMember": old_member,
        "fromParent": old_parent,
        "candidates": candidates,
        "editorSteps": [
            f"Open the BP and locate node '{base['node']}' (Guid {base['nodeGuid']}) in graph '{base['graph']}'.",
            (f"Pick a replacement from candidates: {candidates}."
             if candidates else
             "No near-name candidate was found on the new parent. Supply a member name manually."),
            "Then either: (a) edit the mapping JSON to set this entry to status=auto with the chosen `newMember`,"
            " and re-run `bpmigrate apply-fix-mapping`; or (b) fix the node directly in the editor.",
        ],
    }


def cmd_apply_fix_mapping(args: argparse.Namespace, cfg: Config) -> int:
    """Phase 2 (Layer 3 -- instruction emit, dry-run only).

    Consumes a `broken_refs_mapping_v1` JSON (from `map-broken-refs`) and
    emits a `broken_refs_instructions_v1` JSON describing what the user
    (or a future surgery backend) should do per node. This subcommand
    NEVER modifies a Blueprint -- the contract is "instructions only".

    The same JSON shape is intended to be consumed by Phase 5's surgery
    backends, so `kind: editorAction` entries are unambiguous (action,
    from/to member + parent, node Guid + graph) without parsing prose.
    """
    in_path = Path(args.mapping).resolve()
    if not in_path.exists():
        _die(f"Mapping not found: {in_path}")
    with open(in_path, encoding="utf-8") as f:
        mapping = json.load(f)
    if mapping.get("schema") != "broken_refs_mapping_v1":
        _die(f"unexpected schema: {mapping.get('schema')!r} (expected broken_refs_mapping_v1)")

    new_parent_path = mapping.get("newParentClass", "")
    entries = mapping.get("mappings", []) or []
    instructions = [_instruction_for_mapping_entry(e, new_parent_path) for e in entries]

    summary = {"editorAction": 0, "deferredToUser": 0, "noFix": 0}
    for ins in instructions:
        k = ins.get("kind", "deferredToUser")
        summary[k] = summary.get(k, 0) + 1

    out = {
        "schema": "broken_refs_instructions_v1",
        "newParentClass": new_parent_path,
        "totalInstructions": len(instructions),
        "summary": summary,
        "instructions": instructions,
    }
    text = json.dumps(out, indent=2, ensure_ascii=False)
    _emit_to_output(text + "\n", args.output)
    return 0


# ---------- subcommand: map-broken-refs ----------


def _levenshtein(a: str, b: str) -> int:
    """Plain DP edit distance. Used for name-similarity candidates only."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ac in enumerate(a):
        cur = [i + 1]
        for j, bc in enumerate(b):
            cur.append(min(cur[j] + 1, prev[j + 1] + 1, prev[j] + (ac != bc)))
        prev = cur
    return prev[-1]


def _name_similarity_candidates(target: str, available: list, max_distance: int = 3, limit: int = 5) -> list[str]:
    """Names within edit distance threshold (case-insensitive). Sorted by distance."""
    if not target:
        return []
    t_low = target.lower()
    scored: list[tuple[int, str]] = []
    for name in available:
        if name == target:
            continue
        d = _levenshtein(t_low, name.lower())
        if d <= max_distance:
            scored.append((d, name))
    scored.sort()
    return [n for _, n in scored[:limit]]


def _signatures_match(old_func: dict, new_func: dict) -> tuple[bool, str]:
    """Compare two UFunction signatures. Returns (matches, reason_if_not).

    Compared fields: parameter count, each parameter's `type` (FProperty
    class) and `targetType` (resolved object/struct/enum name), and the
    return-parameter position. Parameter `name` is intentionally NOT
    compared -- the BP only references the function symbol, not param
    names; renaming params does not break the call.
    """
    old_params = old_func.get("params", []) or []
    new_params = new_func.get("params", []) or []
    if len(old_params) != len(new_params):
        return False, f"param count {len(old_params)} -> {len(new_params)}"
    for i, (op, np) in enumerate(zip(old_params, new_params)):
        if op.get("type") != np.get("type"):
            return False, f"param[{i}] type {op.get('type')} -> {np.get('type')}"
        if (op.get("targetType") or "") != (np.get("targetType") or ""):
            return False, f"param[{i}] targetType {op.get('targetType')} -> {np.get('targetType')}"
        if bool(op.get("isReturn")) != bool(np.get("isReturn")):
            return False, f"param[{i}] isReturn differs"
    return True, ""


def _types_match(old_prop: dict, new_prop: dict) -> tuple[bool, str]:
    """Compare two FProperty type signatures."""
    if old_prop.get("type") != new_prop.get("type"):
        return False, f"type {old_prop.get('type')} -> {new_prop.get('type')}"
    if (old_prop.get("targetType") or "") != (new_prop.get("targetType") or ""):
        return False, f"targetType {old_prop.get('targetType')} -> {new_prop.get('targetType')}"
    return True, ""


def _classify_broken_ref(
    ref: dict,
    new_funcs: dict,
    new_props: dict,
    new_class_name: str,
    old_funcs: Optional[dict] = None,
    old_props: Optional[dict] = None,
) -> dict:
    """Decide auto / user_required / reject for a single broken reference.

    Conservative: a name match becomes `auto` only when the reference kind
    matches the candidate's kind (function-as-function, variable-as-variable,
    eventOverride-as-BlueprintEvent). When `old_funcs` / `old_props` are
    provided (from `--old-parent <reflection.json>`), the classifier ALSO
    compares signatures/types -- a name match with a mismatched signature
    drops back to `user_required` with the diff in `rationale`. This is
    the false-auto guard.
    """
    kind = ref.get("refKind")
    member = ref.get("memberName") or ""
    base = {
        "nodeGuid": ref.get("nodeGuid"),
        "graph": ref.get("graph"),
        "node": ref.get("node"),
        "refKind": kind,
        "oldMember": member,
        "oldParent": ref.get("oldParent"),
    }

    if kind == "function":
        if member in new_funcs:
            new_f = new_funcs[member]
            # Signature-aware path (when --old-parent supplied)
            if old_funcs is not None and member in old_funcs:
                ok, reason = _signatures_match(old_funcs[member], new_f)
                if ok:
                    return {
                        **base,
                        "status": "auto",
                        "newMember": member,
                        "newParent": new_class_name,
                        "rationale": (
                            f"function '{member}' name + signature match on new parent '{new_class_name}'"
                        ),
                    }
                return {
                    **base,
                    "status": "user_required",
                    "candidates": [member],
                    "rationale": (
                        f"function '{member}' name match on new parent '{new_class_name}' but signature differs:"
                        f" {reason}; review and adapt or rebind"
                    ),
                }
            # Name-only path (no --old-parent)
            return {
                **base,
                "status": "auto",
                "newMember": member,
                "newParent": new_class_name,
                "rationale": (
                    f"function '{member}' found on new parent '{new_class_name}'"
                    f" (name match; signature compare not enforced -- pass --old-parent to enforce)"
                ),
            }
        cands = _name_similarity_candidates(member, list(new_funcs.keys()))
        return {
            **base,
            "status": "user_required",
            "candidates": cands,
            "rationale": (
                f"function '{member}' not present on new parent '{new_class_name}'"
                + (f"; nearest by name: {cands}" if cands else "; no near-name candidates")
            ),
        }

    if kind == "variable":
        if member in new_props:
            new_p = new_props[member]
            if old_props is not None and member in old_props:
                ok, reason = _types_match(old_props[member], new_p)
                if ok:
                    return {
                        **base,
                        "status": "auto",
                        "newMember": member,
                        "newParent": new_class_name,
                        "rationale": (
                            f"variable '{member}' name + type match on new parent '{new_class_name}'"
                        ),
                    }
                return {
                    **base,
                    "status": "user_required",
                    "candidates": [member],
                    "rationale": (
                        f"variable '{member}' name match on new parent '{new_class_name}' but type differs:"
                        f" {reason}; review and adapt or rebind"
                    ),
                }
            return {
                **base,
                "status": "auto",
                "newMember": member,
                "newParent": new_class_name,
                "rationale": (
                    f"variable '{member}' found on new parent '{new_class_name}'"
                    f" (name match; type compare not enforced -- pass --old-parent to enforce)"
                ),
            }
        cands = _name_similarity_candidates(member, list(new_props.keys()))
        return {
            **base,
            "status": "user_required",
            "candidates": cands,
            "rationale": (
                f"variable '{member}' not present on new parent '{new_class_name}'"
                + (f"; nearest by name: {cands}" if cands else "; no near-name candidates")
            ),
        }

    if kind == "eventOverride":
        cand = new_funcs.get(member)
        if cand and cand.get("isBlueprintEvent"):
            return {
                **base,
                "status": "auto",
                "newMember": member,
                "newParent": new_class_name,
                "rationale": (
                    f"override event '{member}' present as BlueprintEvent on new parent '{new_class_name}'"
                ),
            }
        if cand:
            return {
                **base,
                "status": "user_required",
                "rationale": (
                    f"function '{member}' exists on '{new_class_name}' but is not a BlueprintEvent;"
                    f" cannot be overridden -- delete the node or rebind to a still-present override"
                ),
            }
        return {
            **base,
            "status": "user_required",
            "rationale": (
                f"override event '{member}' not present on new parent '{new_class_name}';"
                f" delete the node or rebind to a still-present override"
            ),
        }

    if kind == "castTarget":
        return {
            **base,
            "status": "reject",
            "rationale": (
                "Cast target deletion is semantic -- only the user can supply"
                " the replacement class or remove the Cast"
            ),
        }

    if kind == "macro":
        return {
            **base,
            "status": "user_required",
            "rationale": (
                "Macro library moved or removed; manual repoint required"
                " (no deterministic mapping available)"
            ),
        }

    if kind == "delegate":
        # Multicast delegate (event dispatcher). New parent's reflection
        # exposes them as FProperty entries (multicast delegate is an
        # FMulticastDelegateProperty / FMulticastInlineDelegateProperty).
        # Treat like a variable lookup: name match -> auto.
        if member in new_props:
            return {
                **base,
                "status": "auto",
                "newMember": member,
                "newParent": new_class_name,
                "rationale": (
                    f"multicast delegate '{member}' present on new parent '{new_class_name}'"
                ),
            }
        cands = _name_similarity_candidates(member, list(new_props.keys()))
        return {
            **base,
            "status": "user_required",
            "candidates": cands,
            "rationale": (
                f"multicast delegate '{member}' missing on new parent '{new_class_name}'"
                + (f"; nearest by name: {cands}" if cands else "; no near-name candidates")
            ),
        }

    if kind == "createDelegate":
        # CreateDelegate's `member` is the bound function on the BP itself
        # (or its current parent). After reparent, look in functions.
        if member in new_funcs:
            return {
                **base,
                "status": "auto",
                "newMember": member,
                "newParent": new_class_name,
                "rationale": (
                    f"CreateDelegate target function '{member}' present on new parent '{new_class_name}'"
                ),
            }
        cands = _name_similarity_candidates(member, list(new_funcs.keys()))
        return {
            **base,
            "status": "user_required",
            "candidates": cands,
            "rationale": (
                f"CreateDelegate target function '{member}' missing on new parent '{new_class_name}'"
                + (f"; nearest by name: {cands}" if cands else "; no near-name candidates")
            ),
        }

    if kind == "asyncTask":
        # ProxyClass + ProxyFunction. The new parent reflection alone cannot
        # tell us if a different async action exists; user must decide.
        return {
            **base,
            "status": "user_required",
            "rationale": (
                f"async task '{member}' proxy class/function missing;"
                f" user must update to a still-present AsyncAction"
            ),
        }

    return {
        **base,
        "status": "user_required",
        "rationale": f"unknown refKind: {kind}",
    }


def cmd_map_broken_refs(args: argparse.Namespace, cfg: Config) -> int:
    """Layer 2: classify each broken reference against a new parent's reflection.

    Inputs:
      - one of --gaps (detect-gaps JSON) or --graph (DumpBPGraph JSON);
        the latter recomputes brokenReferences inline.
      - --new-parent: a DumpClassReflection JSON for the candidate new parent.

    Output:
      mapping JSON (schema: broken_refs_mapping_v1) with one entry per
      broken reference, each labeled status=auto / user_required / reject
      and a `rationale` describing what was actually checked. Phase C
      (instruction emit / surgery backend) consumes this exact shape.
    """
    if not (args.gaps or args.graph):
        _die("provide --gaps <detect-gaps.json> or --graph <DumpBPGraph.json>")
    if not args.new_parent:
        _die("--new-parent <DumpClassReflection.json> is required")

    if args.gaps:
        with open(args.gaps, encoding="utf-8") as f:
            gaps = json.load(f)
        broken = gaps.get("brokenReferences", []) or []
    else:
        with open(args.graph, encoding="utf-8") as f:
            graph_data = json.load(f)
        broken = _detect_broken_references(graph_data)

    with open(args.new_parent, encoding="utf-8") as f:
        refl = json.load(f)
    new_funcs = {f.get("name"): f for f in (refl.get("functions") or []) if isinstance(f, dict)}
    new_props = {p.get("name"): p for p in (refl.get("properties") or []) if isinstance(p, dict)}
    new_class_name = refl.get("className", "?")

    old_funcs: Optional[dict] = None
    old_props: Optional[dict] = None
    if getattr(args, "old_parent", None):
        with open(args.old_parent, encoding="utf-8") as f:
            old_refl = json.load(f)
        old_funcs = {f.get("name"): f for f in (old_refl.get("functions") or []) if isinstance(f, dict)}
        old_props = {p.get("name"): p for p in (old_refl.get("properties") or []) if isinstance(p, dict)}

    mappings = [
        _classify_broken_ref(r, new_funcs, new_props, new_class_name,
                             old_funcs=old_funcs, old_props=old_props)
        for r in broken
    ]

    summary = {"auto": 0, "user_required": 0, "reject": 0}
    for m in mappings:
        s = m.get("status", "user_required")
        summary[s] = summary.get(s, 0) + 1

    out = {
        "schema": "broken_refs_mapping_v1",
        "newParentClass": refl.get("classPath", new_class_name),
        "newParentName": new_class_name,
        "totalBroken": len(broken),
        "summary": summary,
        "mappings": mappings,
    }
    text = json.dumps(out, indent=2, ensure_ascii=False)
    _emit_to_output(text + "\n", args.output)
    return 0


# ---------- subcommand: dump-class-reflection ----------


def cmd_dump_class_reflection(args: argparse.Namespace, cfg: Config) -> int:
    """Wrapper for the DumpClassReflection commandlet.

    Accepts either /Script/<Module>.<Class> or a /Game/-style BP path.
    Used by Layer 2 (`map-broken-refs`) to compare a Blueprint's broken
    references against the new parent class's reflection surface.
    """
    return _run_commandlet(
        cfg,
        "DumpClassReflection",
        positional=[args.class_path],
        switches={"output": args.output} if args.output else {},
    )


# ---------- subcommand: snapshot ----------


def cmd_snapshot(args: argparse.Namespace, cfg: Config) -> int:
    switches = {}
    if args.scenario:
        switches["scenario"] = args.scenario
    if args.output:
        switches["output"] = args.output
    return _run_commandlet(
        cfg, "SnapshotBPBehavior", positional=[args.game_path], switches=switches
    )


# ---------- subcommand: verify ----------


def cmd_verify(args: argparse.Namespace, cfg: Config) -> int:
    switches = {"behavior": args.behavior, "class": args.class_path}
    if args.output:
        switches["output"] = args.output
    return _run_commandlet(cfg, "VerifyMigration", positional=[], switches=switches)


def _run_inline_python(cfg: Config, script: str) -> int:
    """Execute a one-shot Python script inside the editor commandlet.

    Returns the editor's exit code. The temp file is overwritten on each
    call (single-process workflow; the editor itself locks anyway, so two
    parallel `bpmigrate` invocations would already serialise on the editor).
    """
    ue_cmd = cfg.require_ue_cmd()
    uproject = cfg.require_uproject()
    tmp = Path(tempfile.gettempdir()) / "migrate-bp"
    tmp.mkdir(parents=True, exist_ok=True)
    script_path = tmp / "_inline_run.py"
    script_path.write_text(script, encoding="utf-8")
    cmd = [str(ue_cmd), str(uproject), f"-ExecutePythonScript={script_path}",
           "-unattended", "-nop4", "-nosplash", "-nullrhi"]
    return _run(cmd).returncode


def _find_callers_via_ue(bp_path: str, cfg: Config) -> Optional[list[str]]:
    """Resolve `find_package_referencers_for_asset` and return the BP-only list
    (Maps filtered out). Returns `None` on script / IO failure so the caller can
    distinguish "could not enumerate" from a genuine empty referencer set.

    The bp_path is JSON-encoded into the generated script so paths containing
    apostrophes survive the round-trip without breaking the Python literal.
    """
    if "'" in bp_path and '"' in bp_path:
        # Defensive: both quote kinds in one path is too pathological to encode
        # safely with our simple inline-script approach. Refuse rather than
        # generate broken Python.
        print(f"_find_callers_via_ue: refusing path with mixed quotes: {bp_path}",
              file=sys.stderr)
        return None
    tmp_dir = Path(tempfile.gettempdir()) / "migrate-bp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # Per-call uuid so two parallel `bpmigrate` runs don't race on the same file.
    tag = uuid.uuid4().hex[:8]
    out_file = tmp_dir / f"_callers_{tag}.txt"
    out_path_literal = json.dumps(str(out_file).replace("\\", "/"))
    bp_path_literal = json.dumps(bp_path)
    script = (
        "import unreal\n"
        f"refs = unreal.EditorAssetLibrary.find_package_referencers_for_asset("
        f"{bp_path_literal}, load_assets_to_confirm=False)\n"
        f"with open({out_path_literal}, 'w', encoding='utf-8') as f:\n"
        "    for r in refs:\n"
        "        s = str(r)\n"
        "        if not s.startswith('/Game/Maps/'):\n"
        "            f.write(s + '\\n')\n"
    )
    try:
        rc = _run_inline_python(cfg, script)
    finally:
        pass
    if rc != 0 or not out_file.exists():
        try:
            out_file.unlink(missing_ok=True)
        except Exception:
            pass
        return None
    try:
        lines = [ln.strip() for ln in out_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    finally:
        try:
            out_file.unlink(missing_ok=True)
        except Exception:
            pass
    # Sort so the resulting `-callers=A,B,C` argv (and the `callers_requested`
    # echo in the resulting JSON) is byte-stable across editor sessions. Set
    # comparison would PASS either way, but byte-identical truth files are
    # part of the determinism contract.
    return sorted(lines)


def cmd_analyze_candidate(args: argparse.Namespace, cfg: Config) -> int:
    """6-criteria suitability matrix for a BP migration candidate.

    Reads a `dump-graph` JSON (run it implicitly if not provided) plus the
    AssetRegistry referencer count, then prints a markdown table + a single-
    line recommendation. Mirrors the manual analysis the recipe Step 0.5
    walks through.
    """
    bp_path = args.game_path

    # 1) dump-graph (or reuse provided JSON)
    if args.graph_json:
        graph_path = Path(args.graph_json)
    else:
        out_dir = Path(tempfile.gettempdir()) / "migrate-bp"
        out_dir.mkdir(parents=True, exist_ok=True)
        # Use the full game path (slashes -> underscores) so two BPs sharing
        # a leaf name in different folders don't clobber each other's dump.
        slug = bp_path.strip("/").replace("/", "_")
        graph_path = out_dir / f"{slug}_graph.json"
        rc = _run_commandlet(
            cfg, "DumpBPGraph",
            positional=[bp_path],
            switches={"output": str(graph_path)},
        )
        if rc != 0:
            print(f"analyze-candidate: dump-graph failed (rc={rc})", file=sys.stderr)
            return rc
    if not graph_path.exists():
        print(f"analyze-candidate: graph dump missing at {graph_path}", file=sys.stderr)
        return 1
    graph = json.loads(graph_path.read_text(encoding="utf-8"))

    # 2) caller count — None means we couldn't enumerate, distinct from
    # "0 callers". Treat that as a hard failure rather than recommending
    # PROCEED on bogus zero-caller data.
    callers = _find_callers_via_ue(bp_path, cfg)
    if callers is None:
        print(f"analyze-candidate: failed to enumerate callers for {bp_path}; aborting "
              f"(check editor commandlet output)", file=sys.stderr)
        return 2

    # 3) 6 criteria
    components = graph.get("Components", []) or []
    variables  = graph.get("Variables", [])  or []
    interfaces = graph.get("Interfaces", []) or []
    graphs     = graph.get("Graphs", [])     or []
    parent     = graph.get("ParentClass", "?")

    scs_count = len(components)
    if scs_count == 0 or (scs_count == 1 and components[0].get("Name") == "DefaultSceneRoot"):
        scs_status, scs_note = "✅", f"{scs_count} (no overlap risk)"
    elif scs_count <= 3:
        scs_status, scs_note = "⚠", f"{scs_count} ({', '.join(c.get('Name','?') for c in components)}) — verify no name match with target parent's UPROPERTY"
    else:
        scs_status, scs_note = "❌", f"{scs_count} components — high overlap risk; consider clean-slate replacement (Step 5-A-3)"

    bp_interfaces = [i for i in interfaces if isinstance(i, str) and i.endswith("_C")]
    if not bp_interfaces:
        intf_status, intf_note = "✅", "(none)"
    else:
        intf_status = "❌"
        intf_note = (
            f"{', '.join(bp_interfaces)} — UFUNCTION on a C++ port collides with "
            "BP-defined Interface override; selective migration only (interface "
            "method bodies stay BP-side)."
        )

    var_status = "✅" if len(variables) <= 5 else ("⚠" if len(variables) <= 15 else "❌")
    var_note = f"{len(variables)} variables — check for collisions with the target C++ parent's UPROPERTYs before reparent"

    fn_total   = len(graphs)
    fn_logic   = sum(1 for g in graphs if len(g.get("Nodes", [])) > 10)
    fn_stubs   = sum(1 for g in graphs if len(g.get("Nodes", [])) <= 2)
    fn_note    = f"{fn_total} graphs (logic-heavy ≥10 nodes: {fn_logic}, stubs ≤2 nodes: {fn_stubs})"

    cl_count = len(callers)
    if cl_count <= 5:
        cl_status, cl_note = "✅", f"{cl_count} caller BP(s)"
    elif cl_count <= 18:
        cl_status, cl_note = "⚠", f"{cl_count} caller BP(s) — caller-side rewrite is automated via rewrite-callers, but verify scope"
    else:
        cl_status, cl_note = "❌", f"{cl_count} caller BP(s) — large blast radius; consider per-function selective migration"

    # 4) Recommendation
    has_block = (intf_status == "❌") or (scs_status == "❌") or (cl_status == "❌")
    has_warn  = "⚠" in {scs_status, var_status, cl_status}
    if has_block and intf_status == "❌":
        rec = ("PROCEED WITH SELECTIVE MIGRATION — leave the BP-defined interface "
               "method bodies BP-side; native-port only the non-interface functions.")
    elif has_block and scs_status == "❌":
        rec = "ABORT in-place reparent — use clean-slate replacement (Step 5-A-3)."
    elif has_block and cl_status == "❌":
        rec = "PROCEED PER-FUNCTION — surgery one function at a time, verify each."
    elif has_warn:
        rec = "PROCEED WITH CARE — review warnings; rewrite-callers + verify-callers required."
    else:
        rec = "PROCEED — straightforward case."

    # 5) Markdown output
    rows = [
        ("SCS components",          scs_status, scs_note),
        ("BP-defined Interfaces",   intf_status, intf_note),
        ("Variables",               var_status, var_note),
        ("Functions",               "-",        fn_note),
        ("Caller count (BP-only)",  cl_status, cl_note),
        ("Reload-time validation",  "⚠",       "always run `bpmigrate verify-callers` after surgery; in-memory `compile_blueprint` is a known false positive"),
    ]
    print(f"# analyze-candidate: {bp_path}\n")
    print(f"_ParentClass:_ `{parent}`")
    print()
    print("| Criterion | Status | Note |")
    print("|---|---|---|")
    for c, s, n in rows:
        print(f"| {c} | {s} | {n} |")
    print()
    print(f"**Recommendation**: {rec}")
    return 0


def _compute_plan_callsites(
    bp_path: str, cfg: Config, graph_path_override: Optional[Path] = None
) -> tuple[
    Optional[list[tuple[str, str, int]]],
    Optional[list[str]],
    Optional[set[str]],
    Optional[dict[str, int]],
    Optional[Path],
]:
    """Editor-walk authoritative call-site enumeration via `DumpCallSites`
    (UE's own `K2Node_CallFunction` graph walk).

    Returns `(rows, callers, interface_methods, fn_size, truth_path)` —
    `rows` is the list of `(caller_package_path, function_name, count)`
    triples reported by the editor walk. Returns `(None, ...)` on any
    pre-condition failure (dump-graph / caller enumeration / DumpCallSites)
    so the caller can distinguish from a true empty result.
    """
    if graph_path_override is not None:
        graph_path = graph_path_override
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    else:
        out_dir = Path(tempfile.gettempdir()) / "migrate-bp"
        out_dir.mkdir(parents=True, exist_ok=True)
        slug = bp_path.strip("/").replace("/", "_")
        graph_path = out_dir / f"{slug}_graph.json"
        rc = _run_commandlet(
            cfg, "DumpBPGraph",
            positional=[bp_path],
            switches={"output": str(graph_path)},
        )
        # Same trust-the-output-file pattern as DumpCallSites: the editor
        # sometimes exits non-zero from unrelated BP load errors during its
        # own startup, but the dump itself succeeded. Accept the result if
        # the graph file is present and parseable.
        graph = None
        if graph_path.exists():
            try:
                graph = json.loads(graph_path.read_text(encoding="utf-8"))
                if not isinstance(graph.get("Graphs"), list):
                    graph = None
            except Exception:
                graph = None
        if graph is None:
            print(f"plan-rewrite-callers: dump-graph failed (rc={rc}, no usable output)", file=sys.stderr)
            return None, None, None, None, None
        if rc != 0:
            print(f"plan-rewrite-callers: dump-graph editor exited rc={rc} "
                  f"(likely unrelated BP load errors); graph file present and valid -- proceeding.",
                  file=sys.stderr)

    fn_size: dict[str, int] = {}
    for g in graph.get("Graphs", []):
        name = g.get("Name", "")
        if not name or name in ("EventGraph", "UserConstructionScript"):
            continue
        fn_size[name] = len(g.get("Nodes", []))

    bp_interfaces = [i for i in graph.get("Interfaces", []) or [] if i.endswith("_C")]
    interface_methods = set(_extract_interface_methods(bp_interfaces, cfg))

    callers = _find_callers_via_ue(bp_path, cfg)
    if callers is None:
        return None, None, interface_methods, fn_size, None
    if not callers:
        return [], callers, interface_methods, fn_size, None

    truth_dir = Path(tempfile.gettempdir()) / "migrate-bp"
    truth_dir.mkdir(parents=True, exist_ok=True)
    slug = bp_path.strip("/").replace("/", "_")
    truth_path = truth_dir / f"{slug}_callsites.json"
    rc = _run_commandlet(
        cfg, "DumpCallSites",
        positional=[],
        switches={
            "callers": ",".join(callers),
            "target": bp_path,
            "output": str(truth_path),
        },
    )
    # DumpCallSites exit code semantics:
    #   0 = all callers loaded + no errors
    #   3 = some callers failed to load (JSON still valid for loaded subset)
    #   other (e.g. 1, 2) = the editor itself raised errors (typically from
    #   *unrelated* BPs failing to compile during editor startup -- the
    #   commandlet's own work succeeded, the editor just exits non-zero).
    # The truth file itself is the authoritative signal: if it exists, was
    # written with our schema, and reports "callers_failed: 0" + "callsites: ...",
    # accept the result regardless of the editor's exit code. If the file is
    # missing or unparseable, then the commandlet truly failed.
    truth = None
    if truth_path.exists():
        try:
            truth = json.loads(truth_path.read_text(encoding="utf-8"))
            if truth.get("schema") != "callsites_v1":
                truth = None
        except Exception:
            truth = None
    if truth is None:
        print(f"plan-rewrite-callers: DumpCallSites failed (rc={rc}, no usable truth file)", file=sys.stderr)
        return None, callers, interface_methods, fn_size, None
    if rc not in (0, 3):
        print(f"plan-rewrite-callers: editor exited rc={rc} (likely unrelated BP load errors); "
              f"truth file present and valid -- proceeding.", file=sys.stderr)
    failed = truth.get("callers_failed") or []
    if failed:
        print(f"plan-rewrite-callers: {len(failed)} caller(s) failed to load (proceeding on the loaded subset)",
              file=sys.stderr)

    rows: list[tuple[str, str, int]] = [
        (s["caller"], s["function"], int(s.get("count", 1)))
        for s in truth.get("callsites", [])
    ]
    return rows, callers, interface_methods, fn_size, truth_path


def cmd_plan_rewrite_callers(args: argparse.Namespace, cfg: Config) -> int:
    """For each caller of a target BP, count how many times each of the target's
    BP-side function names appears in the caller's NameMap, then surface the
    best candidates for native migration (most-called + non-interface + has-body).

    Output: markdown table (caller × function -> count) + a recommendation
    list of functions worth migrating in priority order. Backed by the
    `DumpCallSites` editor commandlet (UE's own K2Node_CallFunction graph
    walk) so call-site counts match what the editor itself would see.
    """
    bp_path = args.game_path
    graph_override = Path(args.graph_json) if args.graph_json else None
    rows, callers, interface_methods, fn_size, _truth_path = _compute_plan_callsites(
        bp_path, cfg, graph_path_override=graph_override
    )
    if rows is None:
        # _compute_plan_callsites already printed the reason
        if callers is None:
            return 2  # caller-enumeration failure
        return 1
    if not callers:
        print(f"plan-rewrite-callers: no callers found for {bp_path}", file=sys.stderr)
        return 0
    # Re-load graph to get parent-class string for the header (cheap; the
    # dump file is already on disk by the time we reach here).
    if graph_override is not None:
        graph_path = graph_override
    else:
        slug = bp_path.strip("/").replace("/", "_")
        graph_path = Path(tempfile.gettempdir()) / "migrate-bp" / f"{slug}_graph.json"
    # 5) per-function aggregate
    fn_callers: dict[str, list[tuple[str, int]]] = {}
    for cp, fn, h in rows:
        fn_callers.setdefault(fn, []).append((cp, h))

    # 6) markdown
    print(f"# plan-rewrite-callers: {bp_path}\n")
    print(f"_callers (BP-only): {len(callers)} | functions: {len(fn_size)} (interface methods: {len(interface_methods)})_\n")
    if rows:
        print("## Caller × Function (hits)\n")
        print("| Caller | Function | Hits | NodeCount | Kind |")
        print("|---|---|---:|---:|---|")
        for cp, fn, h in sorted(rows, key=lambda r: (r[0], r[1])):
            short = cp.split("/")[-1]
            # fn may not be in fn_size if it lives on a graph kind that the
            # dump skips (EventGraph / UserConstructionScript) -- use 0 as
            # a neutral default so the row still renders rather than crashing.
            sz = fn_size.get(fn, 0)
            kind = "interface-stub" if fn in interface_methods else (
                "stub" if sz <= 2 else ("logic" if sz >= 10 else "small")
            )
            print(f"| {short} | `{fn}` | {h} | {sz} | {kind} |")
        print()

    # 7) recommendations: most-called non-interface logic
    recs = []
    for fn, lst in fn_callers.items():
        if fn in interface_methods:
            continue
        total_hits = sum(h for _, h in lst)
        recs.append((fn, len(lst), total_hits, fn_size.get(fn, 0)))
    recs.sort(key=lambda r: (-r[1], -r[2], -r[3]))

    if recs:
        print("## Migration candidates (non-interface, ranked)\n")
        print("| Rank | Function | #callers | Total hits | NodeCount |")
        print("|---:|---|---:|---:|---:|")
        for i, (fn, c, h, sz) in enumerate(recs[:10], 1):
            print(f"| {i} | `{fn}` | {c} | {h} | {sz} |")
        print()
        print(f"**Top candidate**: `{recs[0][0]}` — {recs[0][1]} caller(s), "
              f"{recs[0][2]} ref(s), {recs[0][3]} nodes.")
    else:
        print("## No non-interface functions are called by the discovered callers.\n"
              "_(Either the BP only exposes interface stubs, or callers route via the interface method-call channel.)_")
    return 0


def _extract_interface_methods(bp_interfaces: list[str], cfg: Config) -> list[str]:
    """Best-effort: dump each BP-defined interface and collect its function names.
    On any failure, return empty (the recommendation will still rank by frequency,
    just without an interface filter — caller can ignore obvious stubs)."""
    out: list[str] = []
    for itf in bp_interfaces:
        # itf form: "BI_X_C" — convert to /Game path heuristically by searching.
        # We don't always know the path, so try a directory probe.
        name = itf[:-2] if itf.endswith("_C") else itf
        # Search Content/ for "<name>.uasset"
        try:
            if not cfg.project_root:
                continue
            content = Path(cfg.project_root) / "Content"
            hits = list(content.rglob(f"{name}.uasset"))
        except Exception:
            continue
        if not hits:
            continue
        # Run dump-graph against /Game/<rel>
        rel = hits[0].relative_to(content).with_suffix("").as_posix()
        game_path = "/Game/" + rel
        out_dir = Path(tempfile.gettempdir()) / "migrate-bp"
        out_dir.mkdir(parents=True, exist_ok=True)
        gj = out_dir / f"{name}_graph.json"
        rc = _run_commandlet(
            cfg, "DumpBPGraph",
            positional=[game_path],
            switches={"output": str(gj)},
        )
        if rc != 0 or not gj.exists():
            continue
        try:
            data = json.loads(gj.read_text(encoding="utf-8"))
            for g in data.get("Graphs", []):
                n = g.get("Name", "")
                if n and n not in ("EventGraph", "UserConstructionScript"):
                    out.append(n)
        except Exception:
            continue
    return out


def cmd_verify_callers(args: argparse.Namespace, cfg: Config) -> int:
    """Drive the `VerifyCallers` commandlet.

    Invariant: every caller is force-unloaded, fresh-loaded from disk, and
    recompiled inside the editor; `FCompilerResultsLog::NumErrors` is the
    authoritative pass/fail signal (the in-memory `compile_blueprint` API
    is a known false-positive — see LIMITATIONS.md).
    Exit code = total error count across all callers.
    """
    if not args.target and not args.callers:
        print("verify-callers: provide --target=<TargetBP> (auto-discovers callers) "
              "or --callers=A,B,C (explicit list).", file=sys.stderr)
        return 2
    switches: dict[str, str] = {}
    if args.target:
        switches["target"] = args.target
    if args.callers:
        switches["callers"] = args.callers
    if args.output:
        switches["output"] = args.output
    return _run_commandlet(cfg, "VerifyCallers", positional=[], switches=switches)


def cmd_rewrite_callers(args: argparse.Namespace, cfg: Config) -> int:
    """Drive the `RewriteCallers` commandlet.

    Invariant: every K2Node_CallFunction in `--callers` whose target matches
    `--old` is rewritten to point at `--new`. DefaultObject (self-pin CDO),
    pin defaults, and downstream links are preserved; a pure DynamicCast is
    inserted automatically when the new return type is wider than the old.
    With `--save`, each modified caller is compiled + saved.
    """
    switches: dict[str, str] = {
        "callers": args.callers,
        "old": args.old,
        "new": args.new,
    }
    if args.pinmap:
        switches["pinmap"] = args.pinmap
    if args.save:
        # Empty value -> _run_commandlet emits a bare "-save", which is what
        # FParse::Param expects on the C++ side.
        switches["save"] = ""
    return _run_commandlet(cfg, "RewriteCallers", positional=[], switches=switches)


def _run_commandlet(
    cfg: Config, name: str, *, positional: list[str], switches: dict
) -> int:
    ue_cmd = cfg.require_ue_cmd()
    uproject = cfg.require_uproject()

    cmd = [str(ue_cmd), str(uproject), f"-run={name}"]
    cmd.extend(positional)
    for k, v in switches.items():
        # Empty / None value emits a bare flag ("-save") rather than "-save="
        # so it matches UE's FParse::Param semantics.
        if v == "" or v is None:
            cmd.append(f"-{k}")
        else:
            # UE FParse::Value uses comma / space / tab as token boundaries;
            # wrap any value that contains them in literal double quotes so
            # the full value survives parsing.
            sval = str(v)
            needs_quote = any(c in sval for c in (",", " ", "\t"))
            cmd.append(f'-{k}="{sval}"' if needs_quote else f"-{k}={sval}")
    cmd += ["-nosplash", "-nopause", "-nullrhi"]

    res = _run(cmd)
    return res.returncode


# ---------- argparse setup ----------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bpmigrate",
        description=(
            "Cross-platform CLI for the BP -> C++ migration toolchain. "
            "See the module docstring for environment-variable configuration."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Global options. Subcommands inherit them via the same parser
    # (argparse stores them on the namespace regardless of order).
    p.add_argument("--project-root", help="Project root (parent of Content/).")
    p.add_argument("--content-root", help="Content directory.")
    p.add_argument("--uproject", help="Path to .uproject file.")
    p.add_argument("--ue-cmd", help="Path to UnrealEditor-Cmd executable.")
    p.add_argument(
        "--uassetgui", help="Path to UAssetGUI.exe (default: bundled tools/UAssetGUI)."
    )
    p.add_argument(
        "--ue-version", help="UE version string passed to UAssetGUI tojson (default: UE5_2)."
    )
    p.add_argument("--tmpdir", help="Temporary directory for intermediates.")
    p.add_argument(
        "--config",
        help=(
            "Path to a TOML config file. Default: searches CWD for "
            ".bpmigrate.toml / bpmigrate.toml / pyproject.toml, then "
            "$BPMIGRATION_PROJECT_ROOT/.bpmigrate.toml. CLI flags and env "
            "vars take precedence over config-file values."
        ),
    )

    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("uasset-tojson", help="Convert .uasset to JSON via UAssetGUI.")
    sp.add_argument("uasset", help="Path to the source .uasset.")
    sp.add_argument("-o", "--output", help="Output JSON path (default: <tmpdir>/<name>.json).")
    sp.set_defaults(func=cmd_uasset_tojson)

    sp = sub.add_parser("summarize", help="Run summarizer on a UAssetGUI JSON.")
    sp.add_argument("json_path", help="Path to UAssetGUI JSON.")
    sp.add_argument("--bytecode", action="store_true", help="Include decompiled bytecode.")
    sp.add_argument("--max-lines", type=int, default=0, help="Truncate output to N lines.")
    sp.add_argument("-o", "--output", help="Output text path (default: stdout).")
    sp.set_defaults(func=cmd_summarize)

    sp = sub.add_parser("inspect", help="Full read-only inspection pipeline.")
    sp.add_argument(
        "target",
        help="Blueprint name (resolved via Content/) or absolute .uasset path.",
    )
    sp.add_argument("--raw", action="store_true", help="Append summarizer raw output.")
    sp.set_defaults(func=cmd_inspect)

    sp = sub.add_parser(
        "dump-graph", help="Invoke the DumpBPGraph editor commandlet (requires plugin)."
    )
    sp.add_argument("game_path", help="/Game/-style asset path.")
    sp.add_argument("-o", "--output", help="Output JSON path.")
    sp.set_defaults(func=cmd_dump_graph)

    sp = sub.add_parser(
        "apply-fix-mapping",
        help=(
            "Layer 3 dry-run: convert a mapping JSON into per-node editor"
            " instructions (broken_refs_instructions_v1). Never modifies BPs."
        ),
    )
    sp.add_argument("mapping", help="Path to a broken_refs_mapping_v1 JSON.")
    sp.add_argument("-o", "--output", help="Output instruction JSON (default: stdout).")
    sp.set_defaults(func=cmd_apply_fix_mapping)

    sp = sub.add_parser(
        "map-broken-refs",
        help=(
            "Layer 2 classifier: each broken reference -> auto / user_required / reject."
            " Inputs: detect-gaps result + new parent reflection."
        ),
    )
    sp.add_argument("--gaps", help="Path to detect-gaps JSON output (preferred).")
    sp.add_argument(
        "--graph",
        help="Path to a DumpBPGraph dump (alternative to --gaps; recomputes broken refs).",
    )
    sp.add_argument(
        "--new-parent",
        required=True,
        help="Path to a DumpClassReflection JSON for the candidate new parent class.",
    )
    sp.add_argument(
        "--old-parent",
        help=(
            "Optional. Path to a DumpClassReflection JSON for the BP's pre-reparent"
            " parent class. When supplied, name-matched candidates are also signature/"
            "type-compared -- mismatches drop from `auto` to `user_required`."
        ),
    )
    sp.add_argument("-o", "--output", help="Output mapping JSON path (default: stdout).")
    sp.set_defaults(func=cmd_map_broken_refs)

    sp = sub.add_parser(
        "dump-class-reflection",
        help=(
            "Dump UClass reflection (functions + properties) for a C++ or BP"
            " class. Used by `map-broken-refs` to find new-parent matches."
        ),
    )
    sp.add_argument(
        "class_path",
        help="/Script/<Module>.<Class> for C++ classes, or /Game/Path/BP_Foo for BP classes.",
    )
    sp.add_argument("-o", "--output", help="Output JSON path.")
    sp.set_defaults(func=cmd_dump_class_reflection)

    sp = sub.add_parser("snapshot", help="Capture a Blueprint behavior trace.")
    sp.add_argument("game_path", help="/Game/-style asset path.")
    sp.add_argument("--scenario", help="Path to scenario JSON (auto-generates if absent).")
    sp.add_argument("-o", "--output", help="Output behavior trace path.")
    sp.set_defaults(func=cmd_snapshot)

    sp = sub.add_parser(
        "verify", help="Compare a C++ class against a behavior trace."
    )
    sp.add_argument("--behavior", required=True, help="Path to behavior trace JSON.")
    sp.add_argument(
        "--class", dest="class_path", required=True,
        help="Class path, e.g. /Script/MyModule.MyClass.",
    )
    sp.add_argument("-o", "--output", help="Output regression report path.")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("scenario", help="Generate a default scenario JSON.")
    sp.add_argument("json_path", help="UAssetGUI JSON path.")
    sp.add_argument("--graph", help="Graph dump JSON path (for accurate parameter names).")
    sp.add_argument("-o", "--output", help="Output scenario path.")
    sp.set_defaults(func=cmd_scenario)

    sp = sub.add_parser(
        "detect-gaps", help="Static detection of missing exports / refs-body gaps / cook refs / orphan pins."
    )
    sp.add_argument("json_path", help="UAssetGUI JSON path.")
    sp.add_argument(
        "--summary-text",
        help="summarizer text output (enables references-body gap detection).",
    )
    sp.add_argument(
        "--graph",
        help="DumpBPGraph dump JSON (enables orphan-pin and dead-input detection).",
    )
    sp.add_argument("-o", "--output", help="Output JSON path (default: stdout).")
    sp.set_defaults(func=cmd_detect_gaps)

    sp = sub.add_parser(
        "emit-variable-defaults",
        help=(
            "Emit UPROPERTY declarations + deterministic initializers from"
            " a UAssetGUI JSON's NewVariables array. See Rule 10 / Rule 11."
        ),
    )
    sp.add_argument("input", help="Path to UAssetGUI JSON.")
    sp.add_argument("-o", "--output", help="Output file (default: stdout).")
    sp.set_defaults(func=cmd_emit_variable_defaults)

    sp = sub.add_parser(
        "emit-dispatcher-delegates",
        help=(
            "Emit DECLARE_DYNAMIC_MULTICAST_DELEGATE_*Param macros from"
            " a UAssetGUI JSON's `*__DelegateSignature` exports. See Rule 3."
        ),
    )
    sp.add_argument("input", help="Path to UAssetGUI JSON.")
    sp.add_argument("-o", "--output", help="Output file (default: stdout).")
    sp.set_defaults(func=cmd_emit_dispatcher_delegates)

    sp = sub.add_parser(
        "emit-class-flags",
        help=(
            "Emit `UCLASS(<specifiers>)` from a graph dump's ClassFlags array."
            " See Rule 14."
        ),
    )
    sp.add_argument("input", help="Path to DumpBPGraph dump JSON.")
    sp.add_argument("-o", "--output", help="Output file (default: stdout).")
    sp.set_defaults(func=cmd_emit_class_flags)

    sp = sub.add_parser(
        "emit-component-overrides",
        help=(
            "Generate deterministic C++ constructor lines from componentsRequired"
            " (detect-gaps output) or Components (raw graph dump). See Rule 12."
        ),
    )
    sp.add_argument(
        "input",
        help="Path to detect-gaps JSON or DumpBPGraph dump JSON.",
    )
    sp.add_argument("-o", "--output", help="Output .cpp snippet path (default: stdout).")
    sp.set_defaults(func=cmd_emit_component_overrides)

    sp = sub.add_parser("find-bp", help="Locate a Blueprint .uasset by name.")
    sp.add_argument("name", help="Blueprint base name (with or without .uasset).")
    sp.add_argument("--all", action="store_true", help="Print every match, not just one.")
    sp.set_defaults(func=cmd_find_bp)

    sp = sub.add_parser(
        "plan-rewrite-callers",
        help=(
            "For each caller of a target BP, count NameMap hits per BP function"
            " and rank non-interface logic functions for native migration."
        ),
    )
    sp.add_argument("game_path", help="/Game/Path/To/<TargetBP>.")
    sp.add_argument(
        "--graph-json",
        help="Reuse an existing dump-graph JSON (skip the editor commandlet).",
    )
    sp.set_defaults(func=cmd_plan_rewrite_callers)

    sp = sub.add_parser(
        "analyze-candidate",
        help=(
            "6-criteria suitability matrix for a BP migration candidate"
            " (SCS overlap / Interface / Variables / Functions / Caller count /"
            " Reload-validation). Emits markdown + a single-line recommendation."
        ),
    )
    sp.add_argument("game_path", help="/Game/Path/To/<BP> -- Blueprint to analyze.")
    sp.add_argument(
        "--graph-json",
        help="Reuse an existing dump-graph JSON (skip the editor commandlet).",
    )
    sp.set_defaults(func=cmd_analyze_candidate)

    sp = sub.add_parser(
        "verify-callers",
        help=(
            "Force-unload + fresh-load + recompile every caller of a target BP and"
            " report PASS/FAIL via FCompilerResultsLog. Exit code = error count."
        ),
    )
    sp.add_argument("--target", help="/Game/.../<TargetBP> (auto-discover callers via AssetRegistry).")
    sp.add_argument("--callers", help="Comma-separated explicit caller list (overrides --target discovery).")
    sp.add_argument("--output", help="Optional JSON report path (per-caller errors/warnings).")
    sp.set_defaults(func=cmd_verify_callers)

    sp = sub.add_parser(
        "rewrite-callers",
        help=(
            "Rewrite K2Node_CallFunction in caller BPs from <OldClass>.<OldFn>"
            " to <NewClass>.<NewFn>. Preserves DefaultObject (CDO) and inserts"
            " a pure DynamicCast for wide->narrow downcasts."
        ),
    )
    sp.add_argument("--callers", required=True,
                    help="Comma-separated /Game/.../<CallerBP> paths.")
    sp.add_argument("--old", required=True,
                    help='"<OldClassPath>.<OldFnName>" (function may contain spaces -- quote the whole arg).')
    sp.add_argument("--new", required=True,
                    help='"<NewClassPath>.<NewFnName>".')
    sp.add_argument("--pinmap", default="",
                    help='Optional pin renames "Old1=New1,Old2=New2".')
    sp.add_argument("--save", action="store_true",
                    help="Compile + save each modified caller (default: dry).")
    sp.set_defaults(func=cmd_rewrite_callers)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    # stdout/stderr are already UTF-8 reconfigured at module import time
    # (see top of file) so non-ASCII help text + report output survive
    # Korean cp949 / Western cp1252 Windows consoles. No re-call needed here.
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = Config(args)
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
