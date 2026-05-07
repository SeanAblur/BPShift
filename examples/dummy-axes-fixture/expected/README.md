# Per-environment baseline (not committed)

This directory is intentionally empty in the repository. The toolchain's
deterministic-output contract is verified with `tests/test_determinism.py`,
which runs each command twice on a Blueprint **in your project** and
asserts the two outputs are byte-identical. Output content depends on
your asset paths, so the baseline is per-environment, not per-repo.

## Generating your own baseline

To produce a stable reference set you can compare future toolchain
changes against, run on a representative BP from your project:

```sh
BP=/Game/Path/To/MyBlueprint   # whichever BP you trust
OUT=examples/dummy-axes-fixture/expected

bpmigrate uasset-tojson <ContentRoot>/Path/To/MyBlueprint.uasset -o $OUT/bp.json
bpmigrate dump-graph $BP -o $OUT/bp_graph.json
bpmigrate dump-class-reflection /Script/Engine.Actor -o $OUT/parent_reflection.json

bpmigrate detect-gaps          $OUT/bp.json --graph $OUT/bp_graph.json -o $OUT/gaps.json
bpmigrate scenario             $OUT/bp.json --graph $OUT/bp_graph.json -o $OUT/scenario.json
bpmigrate emit-class-flags     $OUT/bp_graph.json -o $OUT/class_flags.cpp
bpmigrate emit-component-overrides $OUT/bp_graph.json -o $OUT/component_overrides.cpp
bpmigrate emit-variable-defaults   $OUT/bp.json -o $OUT/variable_defaults.cpp
bpmigrate emit-dispatcher-delegates $OUT/bp.json -o $OUT/dispatcher_delegates.cpp
bpmigrate map-broken-refs --gaps $OUT/gaps.json --new-parent $OUT/parent_reflection.json -o $OUT/mapping.json
```

Add this directory to your local `.git/info/exclude` if your fixture BP
contains private project paths.

## Why no committed baseline?

The fixture Blueprint described in `../README.md` is a synthetic
shape (4-component visual gizmo Actor with one CDO override). The exact
asset path differs per project, and committing one project's path strings
into the OSS repository would leak environment specifics. The
**deterministic contract itself** -- same input always produces the
same output -- is enforced by `tests/test_determinism.py` regardless of
whether a baseline is committed.
