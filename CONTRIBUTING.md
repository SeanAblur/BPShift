# Contributing to BPShift

Thanks for your interest. This project is most useful when contributors
add concrete cases — new K2Node mappings, new UE versions verified, new
gap-detection rules — that the recipe and CLI can apply.

## What we welcome

- **K2Node coverage** — extend the C++ mapping table in
  `skill/migrate-bp.md` Step 4 rule 7. Include a one-line BP example
  and the matching C++ idiom.
- **New gap detectors** — add a category to `bpmigrate detect-gaps`
  (in `python/bpmigrate.py`) and document it in
  `skill/migrate-bp.md` Step 1-E.
- **UE version compatibility** — try the toolchain on UE 5.3 / 5.4 /
  later, document deltas in `docs/UE_VERSIONS.md`, file PRs that fix
  breakage.
- **Bug reports with reproducible BPs** — see Issue Template.
- **Docs / examples / typos** — always welcome.

## Before opening a PR

1. **Read [LIMITATIONS.md](LIMITATIONS.md)**. If your change touches an
   "untested" row, add a verification step in the PR description.
2. **Smoke-test the CLI**:
   ```
   python python/bpmigrate.py --help
   python python/bpmigrate.py inspect --help
   python python/bpmigrate.py detect-gaps --help
   ```
   These must succeed with no errors.
3. **Run the determinism contract test** (with a representative BP from
   your project):
   ```
   TEST_BP_JSON=... TEST_BP_GRAPH=... TEST_NEW_PARENT=... \
   PYTHONHASHSEED=random python tests/test_determinism.py
   ```
   All commands must report `identical`.
4. **If you added a Python module**, add it to
   `pyproject.toml`'s `py-modules` list.
5. **If you changed `migrate-bp.md` rules**, the new rule must be
   DETERMINISTIC — explicit data lookup or regex, no LLM-side
   interpretation. The recipe's whole value rests on this.
6. **If you changed the UE plugin C++**, update both copies
   (`ue-plugin/BPMigration/Source/...` and any test-bench copy you
   used). Keep them in sync.

## Adding new behavior

The codebase keeps "what to update when X is added" mostly local to a
single registry / table per concern, so a new entity should not need to
touch many files. The map below tells you where each concern lives:

| Adding a new... | Single source of truth | Other touch points |
|---|---|---|
| K2Node kind (Resolved + brokenReferences) | `REF_KIND_REGISTRY` in `python/bpmigrate.py` (1 entry) | `DumpBPGraphCommandlet.cpp` `SerializeNode()` adds the `Cast<UK2Node_Foo>` branch + emits `Resolved`/`UnresolvedReason`. `bpmigrate detect-gaps` will report your class under `unauditedK2Nodes` until that branch lands. |
| Component-class setter mapping | `COMPONENT_SETTER_TABLE` in `python/bpmigrate.py` (1 dict entry) | Confirm the setter exists in the real UE API via `bpmigrate dump-class-reflection /Script/Module.YourComponent`. Compile-test the emitted code if uncertain. |
| Layer 3 instruction `kind` | `_instruction_for_mapping_entry()` (one new `kind` block) | If the new kind carries new fields (`surgeryHints` etc.), bump the JSON schema name. |
| `bpmigrate` subcommand | `argparse` registration in `build_parser()` + `cmd_<name>()` | Module-top docstring `Subcommands` list; `tests/test_determinism.py` if the command produces deterministic output; `README.md` Quick start if user-facing. |
| Editor commandlet | `<Name>Commandlet.{h,cpp}` under `ue-plugin/.../{Public,Private}/` | Add to `BPMigration.Build.cs` `PrivateDependencyModuleNames` only if you reach beyond `Engine/CoreUObject/BlueprintGraph/UnrealEd/Kismet/AssetRegistry`. Wrap from Python via `_run_commandlet()` + a new `cmd_<name>`. |
| C++ helper (`FBPCallFunctionRewriter`-style) | `<Name>.{h,cpp}` under `ue-plugin/.../{Public,Private}/` | Walk graphs via `FBPGraphWalk::ForEachExecGraph` -- do NOT roll your own walk, that contract is centralised so `DumpCallSites`/`CallFunctionRewriter`/etc. stay congruent. Use `Schema->TryCreateConnection()` over raw `MakeLinkTo()` so reconcile + reload-time validator pass. On partial-wiring failure, undo the half-link and remove the orphan node before falling through. If a switch value can contain a comma / space / tab, `_run_commandlet` quote-wraps it for you (UE `FParse::Value` uses those as token boundaries). |

If you find yourself updating five files for a one-feature change, that's
a smell -- the registry / helper for that concern should absorb the new
entry, not the call sites. File an issue.

## Local development

```
git clone <your-fork-url>
cd BPShift

# python module test (no UE required)
python python/bpmigrate.py --help
python python/bpmigrate.py detect-gaps <some-uasset-json> --graph <graph.json>

# UE plugin test (requires a UE 5.2 project)
cp -r ue-plugin/BPMigration <YourTestProject>/Plugins/
# rebuild from your IDE
```

## Issue / PR style

- Title: imperative present tense. e.g. "add Map_Find K2Node mapping".
- Body: what changed, why, what was tested, any open questions.
- For bug reports: see the issue template — include UE version, OS,
  the failing CLI invocation + stderr, and a minimal failing BP if
  shareable.

## License

By contributing, you agree your contributions are licensed under the
project's [MIT License](LICENSE). No CLA.
