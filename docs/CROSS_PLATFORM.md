# Cross-platform notes

Verified on: **Windows 11 (Git Bash + Python 3.14)**.

The Python CLI is portable (uses `pathlib`, `subprocess`, no
platform-specific calls). The UE plugin is editor-build agnostic — UE
runs on Windows, macOS, and Linux. The friction is the bundled
**UAssetGUI.exe**, which is a Windows-only .NET binary.

## macOS / Linux options

`UAssetGUI.exe` is a .NET 6 app; pick whichever runtime fits your stack.
For all four options, wrap the launch in a shim script and point
`BPMIGRATION_UASSETGUI` at it:

```bash
# tools/UAssetGUI/uassetgui.sh
#!/usr/bin/env bash
exec <runtime> "$(dirname "$0")/UAssetGUI.exe" "$@"   # <runtime> per row below
```

| Option | `<runtime>` | Trade-off |
|---|---|---|
| .NET runtime | `dotnet` (`brew install dotnet` / `apt install dotnet-sdk-6.0`) | Native-ish; cleanest for CI. |
| Wine | `wine` (`brew install wine-stable` / `apt install wine`) | Heaviest dep, but mirrors Windows behavior 1:1. |
| Native UAssetAPI build | (your own CLI built from [UAssetAPI](https://github.com/atenfyr/UAssetAPI) source) | Smallest runtime; you write the `tojson` shim. PRs welcome. |
| Editor-commandlet only | (no shim — drop `BPMIGRATION_UASSETGUI`) | Skip bytecode; `dump-graph` + `detect-gaps --graph` cover graph-level analysis. Lose `summarize --bytecode`'s function-body pseudocode. |

## UE editor commandlets

`UnrealEditor-Cmd.exe` lives at:

| Platform | Path |
|---|---|
| Windows | `<UE>/Engine/Binaries/Win64/UnrealEditor-Cmd.exe` |
| macOS | `<UE>/Engine/Binaries/Mac/UnrealEditor.app/Contents/MacOS/UnrealEditor-Cmd` |
| Linux | `<UE>/Engine/Binaries/Linux/UnrealEditor-Cmd` |

Set `BPMIGRATION_UE_CMD` accordingly.

## Path conventions in the CLI

The CLI uses `pathlib` everywhere and accepts both Unix and Windows
separators. Internally, `MSYS_NO_PATHCONV=1` is set for subprocess
calls so Git Bash on Windows does not mangle `/Game/...` paths into
`C:/Program Files/Git/Game/...`. This is automatic — no user action.

## Python version

`tomllib` is stdlib in Python 3.11+. For 3.10:

```bash
pip install tomli
```

(Also documented in `pyproject.toml`'s `optional-dependencies.toml-py310`.)

## Known cross-platform pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| `dotnet not found` when invoking UAssetGUI on macOS | .NET runtime missing | `brew install dotnet` |
| `wine: command not found` (option 2) | Wine missing | install via package manager |
| Slash-command paths in `skill/*.md` use Windows separators | Doc examples written on Windows | the recipe says `<UE>/Engine/...` style; use the equivalent on your OS |
| `DumpBPGraph` produces UTF-16 output | Default `SaveStringToFile` encoding | the commandlet uses `ForceUTF8WithoutBOM`; if this changes, file an issue |

If you successfully run the toolchain on macOS or Linux end-to-end,
please open an issue / PR adding the verified version to the platform
table above.
