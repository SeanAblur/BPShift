---
description: "Read-only inspection of a Blueprint's logic and structure. Runs the bpmigrate inspect pipeline and surfaces gaps."
argument-hint: "<BP-name> | <abs uasset path> [--raw]"
allowed-tools: ["Bash", "Read"]
---

# Inspect Blueprint (read-only)

Produce a human-readable summary of a Blueprint's structure, variables,
events, and per-function logic. **No file modification, no C++ code
generation, no source-control changes.**

**Arguments:** "$ARGUMENTS"
- First arg: Blueprint name (resolved via configured Content/) or an
  absolute `.uasset` path.
- `--raw`: append the full `summarizer` text after the summary.

**Setup**: this skill drives the `bpmigrate` CLI. Configure once via env
vars (or pass `--flag`s on each invocation):

- `BPMIGRATION_PROJECT_ROOT` — project root (parent of `Content/`).
- `BPMIGRATION_UASSETGUI` — path to `UAssetGUI.exe` (defaults to bundled
  `tools/UAssetGUI/UAssetGUI.exe`).
- `BPMIGRATION_UE_VERSION` — e.g. `UE5_2` (default).

---

## Step 0: argument validation

If no argument is provided, ask the user for a Blueprint name or `.uasset`
path and stop.

---

## Step 1: run the inspect pipeline

```bash
python <path-to>/bpmigrate.py inspect "<arg>"
# or, if you exposed the bundled binary on PATH:
bpmigrate inspect "<arg>"
```

The CLI executes, in order:

1. Resolves the target `.uasset` (by name lookup under the Content root,
   or an absolute path).
2. Runs `UAssetGUI tojson` against a temp copy of the asset (so an editor
   that is currently holding a lock on the source does not block).
3. Runs `summarizer` with `--bytecode`.
4. Runs `detect-gaps` (missing `FunctionExports` plus References-Body
   gaps when the summary text is available).

Tee stdout into the model context.

---

## Step 2: present the summary

Format the `summarizer` output for the user using the template below.
Source the field values from the bytecode summary; do not invent or
generalize.

```
# Inspect: <BP name>

**Path:** <uasset path>
**Parent class:** <parent>

## Coverage
- N functions total; M with bytecode, K without.
- Functions without bytecode (graph-only): <list>

## Variables
- <Name>: <type> [default=<value>]
- ...

## Event handlers
- <Name>: <event type>
- ...

## Function summaries
### <FunctionName> [bytecode | graph-only]
- inputs: <pin list>
- outputs: <pin list>
- pseudocode (when bytecode available):
    <summarizer body>
- structure (graph-only):
    <node types + main Exec connections>

## UberGraph mappings (when present)
- <event name> -> offset <N>

## External dependencies (detected only)
- Calls into: <C++ class::function list>
- Referenced by: <other Blueprints, when visible>
```

---

## Step 3: References-Body gap (when `detect-gaps` reported any)

When the CLI's `referencesBodyGap` field in the report is non-empty,
emit a separate section so the user knows manual verification is needed:

```
## ⚠ References-Body gap (manual verification needed)
The following symbols appear in "C++ Function References" but not in the
pseudocode body. Possible causes:

  1. The call node's output pin (especially ReturnValue) is unconnected,
     so the failure path is silently missing.
  2. UAssetGUI lost the K2Node pin information during serialization.

Verify by opening the Blueprint in the editor and `Ctrl+F`-ing the symbol.
  - <symbol list>
```

---

## Step 4: `--raw` (optional)

If the user passed `--raw`, append the entire `summarizer` text in a
fenced code block after the summary. The CLI already includes it in the
inspect output when `--raw` is passed.

---

## Error handling

- UAssetGUI failure -> stop, surface stderr.
- `summarizer` failure -> stop, surface stderr.
- `detect-gaps` failure -> show the summary anyway, mark the coverage
  field as `unknown` and note the failure.

---

## Forbidden

- Any file modification outside the CLI's transient temp directory.
- C++ code generation.
- Blueprint edits.
- Source-control submit/shelve operations.

---

## Usage

```
/inspect-bp MyBlueprint
/inspect-bp MyBlueprint --raw
/inspect-bp /abs/path/to/MyBlueprint.uasset
```
