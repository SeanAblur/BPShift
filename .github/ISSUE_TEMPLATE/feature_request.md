---
name: Feature request
about: A capability the toolchain should add or improve.
title: "[feature] "
labels: enhancement
---

## What capability you want

<one or two sentences>

## Why

<what problem this solves; what BP shape currently isn't covered>

## Where you'd add it

Pick whichever fits (delete the others):

- **Recipe rule** — `skill/migrate-bp.md` Step <N>, rule <M>. Sketch
  the deterministic logic.
- **CLI subcommand / flag** — `python/bpmigrate.py`. Sketch the
  subcommand signature and JSON output shape.
- **UE plugin commandlet** — new commandlet under
  `ue-plugin/BPMigration/`. Sketch its inputs/outputs and which UE
  APIs it uses.
- **Detection rule** — `bpmigrate detect-gaps` new field. Define the
  detection criteria deterministically.

## Acceptance test

How would we know this works? A specific BP shape we could verify
against?

## Notes

<related issues, links, anything else>
