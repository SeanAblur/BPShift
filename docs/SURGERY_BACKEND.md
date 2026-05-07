# Surgery: what's shipped, what's not

The toolchain modifies `.uasset` graphs in two distinct ways. They have
different safety stories and different shipping status.

## 1. Caller-side surgery — `rewrite-callers` (shipped)

When the migrated C++ class renames a function, every other Blueprint that
called the old function needs every `K2Node_CallFunction` rewritten to
target the new one. The OSS package ships this end-to-end:

| Layer | Component | What it does |
|---|---|---|
| C++ helper | `FBPCallFunctionRewriter::Rewrite` | walks every exec graph in a BP via `FBPGraphWalk::ForEachExecGraph`, swaps matching `K2Node_CallFunction` nodes, preserves `DefaultObject` (CDO), pin defaults, and downstream links. Auto-inserts a pure `DynamicCast` on widening returns. On partial-wiring failure, undoes the half-link and removes the orphan cast node. |
| Editor commandlet | `RewriteCallersCommandlet` | thin driver: parses `-callers=A,B,C / -old=ClassPath.Func / -new=... / [-pinmap] / [-save]`, calls the helper per caller, optionally compiles + saves. Returns load + save failure counts in the exit code. |
| CLI wrapper | `bpmigrate rewrite-callers` | quote-wraps switch values containing comma / space / tab so UE `FParse::Value` accepts the full list. |
| Ground-truth dump | `DumpCallSitesCommandlet` | independent walk via the same `FBPGraphWalk::ForEachExecGraph`. Used by `plan-rewrite-callers` to enumerate `(caller, function, count)` triples editor-walk authoritatively. Same walker as the rewriter, so the truth-vs-rewrite contract holds. |
| Compile validation | `VerifyCallersCommandlet` (`bpmigrate verify-callers`) | force-unload + fresh-load + recompile each caller, read `FCompilerResultsLog::NumErrors` (authoritative — `compile_blueprint()` returns false-positive PASS in some cases). |
| Regression test | `tests/test_caller_plan_accuracy.py` | confirms plan rows = ground truth for a corpus of BPs, plus sanity-negative perturbations (drop / count-bump / synthetic-inject all detected). |

**Safety story**:
- `--save` is non-atomic across multiple callers (caller #5 fails → callers
  #1–4 already on disk). Exit code reflects load + save failure counts.
- The commandlet does NOT write `.uasset.bak`; VCS (P4 / git) is the only
  undo path. See `skill/migrate-bp.md` Step 5-E for the recovery flow.
- Always pair with `verify-callers` afterwards — the on-disk state may
  compile differently from the in-memory state.

**Identity match**: pointer-eq → `ClassPathName` eq for `OldFuncOwner`
(handles REINST / hot-reload classes that get new pointers but keep their
package path).

## 2. Self-side broken-ref auto-fix — deferred

A different surgery: when a BP is reparented to a new C++ class, the BP's
own K2Nodes whose member references no longer resolve (e.g. `Set Tags`
on a parent that no longer has `Tags`) need to be rewritten to point at
the new parent's equivalent member, dropped, or surfaced to the user.

The OSS package ships **detection + classification + instruction emit**
for this surgery — not the surgery itself:

| Layer | Component | What it does |
|---|---|---|
| Detection | `bpmigrate detect-gaps brokenReferences` | DumpBPGraph emits `Resolved`/`UnresolvedReason` per K2Node; detect-gaps groups them by refKind (`function` / `variable` / `castTarget` / `eventOverride` / `macro` / `delegate` / `createDelegate` / `asyncTask`). |
| Classification (Layer 2) | `bpmigrate map-broken-refs --new-parent <ref>` | per broken ref: `auto` (name+signature match against new parent's reflection), `user_required` (near-name candidates only), `reject` (no fix possible, e.g. deleted Cast target). |
| Instruction emit (Layer 3) | `bpmigrate apply-fix-mapping --dry-run` | per K2Node `editorAction` JSON with explicit editor steps. NEVER touches `.uasset`. |
| **Application** | **— deferred —** | Actually rewriting the K2Node `MemberReference` field is left to a separate "broken-ref surgery backend". The safety story (per-team source-control discipline, backup / rollback policy, atomicity guarantees, audit trail) doesn't generalize. |

The user (or an LLM agent) currently applies these instructions by hand
in the editor (right-click K2Node → Refresh / Replace References / etc.).
The `apply-fix-mapping` JSON gives them an explicit per-node script.

### Why caller-side ships but self-side defers

- **Caller-side**: surgery target is well-defined (`K2Node_CallFunction`
  whose `(MemberParent, MemberName)` matches `(OldClass, OldFn)`),
  succeeds-or-fails per caller, validates via fresh-load compile.
  Recovery is per-file P4/git revert.
- **Self-side**: surgery target spans 8 refKinds (function / variable /
  cast / event override / macro / delegate / createDelegate / asyncTask),
  each with its own auto-vs-user-required heuristic, often needing the
  user's choice from a candidate list. Atomic-rollback semantics on a
  partially-rewritten BP are non-trivial; in practice the editor's own
  Refresh/Replace flow is what users converge on.

If you write a self-side broken-ref backend, see the input contract below.

---

## Self-side surgery backend — input contract

A backend is any program that reads `broken_refs_instructions_v1` JSON
(produced by `bpmigrate apply-fix-mapping`) and applies the changes.

```json
{
  "schema": "broken_refs_instructions_v1",
  "newParentClass": "/Script/Module.NewBase",
  "totalInstructions": 5,
  "summary": { "editorAction": 3, "deferredToUser": 1, "noFix": 1 },
  "instructions": [
    {
      "kind": "editorAction",
      "action": "rebind",
      "refKind": "function",
      "nodeGuid": "AC9F4E2D...",
      "graph": "EventGraph",
      "node": "Call OldFunc",
      "fromMember": "OldFunc",
      "fromParent": "OldBase",
      "toMember": "OldFunc",
      "toParent": "NewBase",
      "rationale": "...",
      "editorSteps": ["...", "..."]
    },
    {
      "kind": "deferredToUser",
      "refKind": "function",
      "nodeGuid": "...",
      "graph": "...",
      "node": "...",
      "fromMember": "OldFunc2",
      "fromParent": "OldBase",
      "candidates": ["NewFuncA", "NewFuncB"],
      "rationale": "...",
      "editorSteps": ["..."]
    },
    {
      "kind": "noFix",
      "refKind": "castTarget",
      "nodeGuid": "...",
      "graph": "...",
      "node": "Cast to None",
      "fromMember": "None",
      "fromParent": "",
      "rationale": "...",
      "editorSteps": ["..."]
    }
  ]
}
```

### Required behavior

**`kind: "editorAction"`** — apply the change. Backend MUST:
- Locate the K2Node by `(graph, nodeGuid)`. The Guid is stable across
  save/reload; do NOT rely on `node` (display title) or positional info.
- For `refKind == "function" | "variable" | "delegate" | "createDelegate"
  | "eventOverride"`: rewrite the node's `MemberReference` (`MemberName`,
  `MemberParent`, `bSelfContext`) to point at `(toMember, toParent)`. The
  Guid stays the same.
- Recompile the BP and write back. On compile failure, rollback and
  report.

**`kind: "deferredToUser"`** — skip. Do NOT auto-pick from `candidates`
(Layer 2 already decided this needs the user). A backend MAY prompt the
user; on user pick it should rewrite the mapping JSON to set
`status: "auto"` with the chosen `newMember` and re-run
`bpmigrate apply-fix-mapping` to regenerate instructions.

**`kind: "noFix"`** — skip. The reference has no deterministic auto-fix
(e.g. a deleted Cast target — the user must supply the replacement class
or remove the Cast).

### Required safety

A self-side backend touches `.uasset`. Before any modification:

1. **Snapshot** the BP (`.uasset.before-surgery`, P4 / git lock,
   backup branch — whatever fits the user's source-control workflow).
2. **Exclusive lock** on the BP for the duration of surgery. Concurrent
   edits + a half-applied surgery is the worst-case state.
3. **Atomic apply**. Either every `editorAction` succeeds and the BP
   recompiles, or the original BP is restored.
4. **Re-run detection**. After applying, `bpmigrate dump-graph + detect-gaps`
   should report `brokenReferences: []`. If non-empty, rollback.
5. **Idempotent**. Two runs on the same instruction set produce the same
   final state. Already-applied entries are no-ops, not double-applies.
6. **Audit trail**. Log every `(nodeGuid, fromMember, fromParent) →
   (toMember, toParent)` change to a separate file (e.g.
   `<bp>.surgery.log`).

### Reference implementations

None ship in the OSS package. If you write one, open a PR adding a row:

- backend name + URL
- supported UE version range
- safety mechanism (P4 / git / per-team)
- limitations (e.g. "doesn't handle MacroInstance redirection")

### Schema versioning

`broken_refs_instructions_v1` fields are stable for the v1 line: new
optional fields may be added; existing fields are not removed or renamed.
A breaking change bumps to `_v2` and ships alongside `_v1` for one minor
release before retirement.

A backend reading a field should fail loudly when the schema name does
not start with `broken_refs_instructions_v` or when a required field is
missing — that catches contract drift early.
