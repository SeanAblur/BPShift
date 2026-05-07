# Install — fresh project

Step-by-step verification of a fresh install. Use this when bringing the
toolchain into a new UE project for the first time.

## 0. Prerequisites

- Unreal Engine 5.2 (other versions: see [UE_VERSIONS.md](UE_VERSIONS.md))
- A target UE 5.2 C++ project (`File > New Project > C++` if needed)
- Python 3.10 or newer
- Visual Studio 2022 with the UE workload (Windows) or the equivalent
  toolchain on macOS / Linux

## 1. Drop the plugin in

```
cp -r ue-plugin/BPMigration <YourProject>/Plugins/
```

If your project has never compiled C++ before, also create a `Source/`
directory with a minimal module (UE will refuse to load plugins on
content-only projects).

## 2. Regenerate project files

- Right-click `<YourProject>.uproject` -> **Generate Visual Studio
  project files** (Windows)
- Or run `<UE>/Engine/Build/BatchFiles/GenerateProjectFiles.bat` with
  the `.uproject` argument.

This adds `BPMigration` to the IDE solution.

## 3. Build the editor target

From the IDE: select `<YourProject>Editor | Win64 | Development` and
build.

From CLI:
```
"<UE>/Engine/Build/BatchFiles/Build.bat" <YourProject>Editor Win64 Development -Project="<abs-path>/<YourProject>.uproject"
```

A clean build of `BPMigration` should compile (in some order):

```
BPMigrationModule.cpp
BPGraphWalk.cpp                 # shared graph-walk helper
DumpBPGraphCommandlet.cpp
DumpClassReflectionCommandlet.cpp
SnapshotBPBehaviorCommandlet.cpp
VerifyMigrationCommandlet.cpp
RewriteCallersCommandlet.cpp    # caller-graph surgery (-callers / -old / -new)
VerifyCallersCommandlet.cpp     # post-surgery compile validation
DumpCallSitesCommandlet.cpp     # ground-truth K2Node_CallFunction enumeration
CallFunctionRewriter.cpp        # surgery helper consumed by RewriteCallers
BPBehaviorSnapshot.cpp
```

and link `UnrealEditor-BPMigration.dll` (or the `.dylib` / `.so`
equivalent on macOS / Linux).

## 4. Verify the commandlets are registered

Open the editor once (it will compile shaders / register modules), then
exit. Now:

```
"<UE>/Engine/Binaries/Win64/UnrealEditor-Cmd.exe" "<abs>/<YourProject>.uproject" -run=DumpBPGraph -nullrhi -nopause -nosplash
```

If the output contains `Usage: -run=DumpBPGraph /Game/...`, the
commandlet is registered. If you see `Failed to load commandlet`, the
module did not load — recompile and check the editor log for module
load errors.

Repeat with `DumpClassReflection`, `SnapshotBPBehavior`, `VerifyMigration`,
`RewriteCallers`, `VerifyCallers`, and `DumpCallSites` to confirm every
commandlet registered.

## 5. Install the CLI

```
# Either expose python/ on PATH, or install editable:
pip install -e .
```

(`pip install -e .` requires `pyproject.toml` and Python 3.10+. See
that file for `py-modules` configuration.)

## 6. Configure

Either set env vars in your shell rc:

```
export BPMIGRATION_PROJECT_ROOT=<abs-path-to-project>
export BPMIGRATION_UPROJECT=<abs>/<YourProject>.uproject
export BPMIGRATION_UE_CMD=<UE>/Engine/Binaries/Win64/UnrealEditor-Cmd.exe
```

Or drop a `.bpmigrate.toml` at your project root:

```toml
project_root = "C:/Path/To/YourProject"
uproject     = "C:/Path/To/YourProject/YourGame.uproject"
ue_cmd       = "C:/Program Files/Epic Games/UE_5.2/Engine/Binaries/Win64/UnrealEditor-Cmd.exe"
```

## 7. Smoke-test

```
bpmigrate --help                           # CLI loads
bpmigrate find-bp <SomeBP>                 # asset discovery works
bpmigrate uasset-tojson <abs-path>.uasset  # UAssetGUI works
bpmigrate dump-graph /Game/<path>          # UE commandlet works
```

If any of these fail, file an issue with the exact command + stderr +
your OS / UE version / shell.

## 8. Install the slash commands (Claude Code users only)

```
cp skill/inspect-bp.md skill/migrate-bp.md ~/.claude/commands/
```

(Windows: `%USERPROFILE%\.claude\commands\`.)

The `.md` files reference `bpmigrate ...` directly. Make sure
`bpmigrate` is on `PATH` or edit the .md to point at
`python <abs>/bpmigrate.py`.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Failed to find commandlet 'DumpBPGraph'` | Plugin module not loaded. Regenerate project files + rebuild. Check editor log for `BPMigration` module load errors. |
| `LoadObject failed: /Game/...` | UE editor was already open with the asset — release the lock, or use `bpmigrate uasset-tojson` (works on file copies). |
| `Failed to load Blueprint: C:/Program Files/Git/Game/...` on Git Bash | The shell mangled `/Game/...` to `C:/Program Files/Git/Game/...` before Python received the argv. The CLI sets `MSYS_NO_PATHCONV=1` for its own subprocesses, but cannot influence what the shell did to argv first. Fix: `export MSYS_NO_PATHCONV=1` in your shell rc, or use cmd / PowerShell. README Quick start documents the canonical setup. |
| `bpmigrate: command not found` (after install) | `pip install -e .` was skipped, or a stale shell PATH. Either re-run `pip install -e .` from the repo root, or invoke directly: `python python/bpmigrate.py ...`. |
| Env-var path errors after `MSYS_NO_PATHCONV=1` is set | Once MSYS path conversion is off, Cygwin-style paths (`/c/Program Files/...`) are no longer translated to Windows form. Always write env-var paths in forward-slash Windows form: `'C:/Program Files/Epic Games/UE_5.2/...'`. |
| `tomllib` ImportError | Python < 3.11. Either upgrade Python or `pip install tomli` (the optional toml-py310 extra). |
| Plugin build error: missing UnrealEd / BlueprintGraph | Verify the project IS an editor target (not Game-only). The plugin is editor-only by design. |
