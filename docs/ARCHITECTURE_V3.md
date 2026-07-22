# Architecture v3: Safe Parallel Implementation

## Status

Parallel implementation is an explicit v3 feature. It is not a replacement for the serial v1/v2 workflows or the Web Multi-GPT advisory workflow. The implementation remains inert unless both of these gates are present:

- the workflow schema is exactly `codex.chatgpt.comprehensive-workflow/v3` and `features.parallel_implementation_v1` is `true`;
- `CODEX_CHATGPT_PARALLEL_IMPLEMENTATION_V1=1` is present in the parent environment.

Gate validation occurs before a lease, run directory, staging repository, app, tunnel, or browser submission is created. A missing gate is terminal for the requested v3 operation; there is no silent downgrade or migration.

## Parent families

The common run-state API recognizes two parent families:

- `web-multi`: an advisory coordinator. It retains its existing v1/v2 behavior and recovery semantics and cannot own canonical Git, staging, graph, or finalizer authority.
- `parallel-implementation`: the only family allowed to hold the canonical project lease, bind the implementation graph, create staging state, advance component integration heads, and perform final verification and ff-only apply.

New parents persist `parent_family`. Historic Web Multi parent records without this field are recognized through a strict, read-only compatibility adapter only when the complete legacy parent shape validates. The adapter does not rewrite legacy state.

## Fixed parent-run topology

All v3 mutable runtime paths are derived from the parent run and cannot be selected by a worker:

```text
<parent-run>/parallel-runtime-v1/
  staging-repo/
  worktrees/
    u-<24 lowercase hex>/
  aggregate-worktree/
  missions/
  unit-results/
  recovery/
```

`codexpro_exact_unit_authority.py` is the common topology validator. It produces a hashed receipt that binds the canonical project, parent, component, unit, attempt, staging common Git directory, aggregate path, exact unit root, allowed roots, and sibling set.

For both logical and resolved/final paths, the validator rejects equality or ancestor/descendant overlap between a unit and the canonical repository, staging repository, staging common Git directory, aggregate worktree, or sibling unit worktree. Equality with a drive root or user home is rejected. Existing and planned path chains are checked for symlink, junction, or other reparse escape. `allowedRoots` must contain exactly the unit root.

## Exact-unit app authority

A parallel child never receives drive-root or home fallback. `codexpro_exact_unit_cloudflare_bootstrap.ps1` is the only exact-unit bootstrap entrypoint. Its server contract is:

- `--root <unit-root>`
- `--no-profile`
- `--bash off`
- `--write workspace`
- `--tool-mode full`
- no `--allow-home`
- a unique Cloudflare tunnel for the exact unit

The legacy bootstrap refuses `parallel-exact-unit` and directs callers to the exact entrypoint. The app registry binds `scope_mode=parallel-exact-unit` and the topology receipt hash; a later decision with a different scope or receipt fails closed.

The MCP identity probe validates the exact default root, singleton allowed roots, port, topology binding, bash/write/tool/profile contract, server information, and exposed tool list. A bash tool is not accepted in exact mode.

## Process identity

`codexpro_windows_process_identity.py` binds the actual listener owner rather than a launcher wrapper. Listener receipts include PID, creation time, final executable path and hash, normalized command-line hash, parent process chain, port, local addresses, endpoint key, and topology receipt.

Cloudflare receives a separate tunnel receipt that binds its PID, creation time, executable and command hashes, parent chain, local upstream, public URL hash, endpoint key, and topology receipt. Identity is re-collected immediately before submission; any drift blocks the send.

## Git isolation

`chatgpt_git_isolation.py` owns every Git operation. Workers receive only a checked-out unit directory and must not run Git commands or modify `.git`.

The parent captures a canonical baseline identity containing HEAD, tree, porcelain status, worktree inventory, recursive submodule state, local config, and filesystem identity. Staging is created with exactly:

```text
git clone --no-local --no-hardlinks --no-checkout -- <canonical> <staging>
```

The host uses an isolated HOME/config, disables prompting and credentials, rejects alternates/reference/shared object stores and unsafe inherited config, and verifies source/staging object identity. The staging common Git metadata tree is hashed, excluding the object database but including worktree indexes, HEADs, refs, logs, config, and other authority files. Unexpected mutation is a parent recovery event.

Before accepting a unit, the host derives the actual changed paths from porcelain-v2 status and raw diff. It includes untracked files and rename source/destination paths and rejects out-of-scope changes, `.git`, gitlinks/submodules, unmerged records, and reparse escapes. The host runs only registered test IDs and creates deterministic commits with the immutable input base as the single parent.

## Graph binding and scheduling

The Pro planner returns `implementation-graph-result-v1`. The binder validates bounded unique IDs, canonical relative claims, dependencies, test registry references, and acyclicity.

A disjoint-set union combines every dependency edge and every path-conflict edge. This deliberately makes any dependency or conflicting claim part of one component. Consequently no cross-component dependency or conflict survives graph binding.

Independent components may run in parallel. Each component has at most one active unit and advances in deterministic topological order. A unit's `input_base_oid` is immutable and equals the component integration head at dispatch time. A completed host commit becomes the next unit's input base.

Uncertainty is component-local when the common authority remains healthy, so unrelated components may continue. Damage to the canonical lease, staging common metadata, topology, app identity, listener, tunnel, or parent state escalates to parent recovery and stops all dispatch.

## Exactly-once submission

A parallel child uses `codex.chatgpt.child-send-claim/v2`. The immutable O_EXCL claim binds the run, parent, component, unit, attempt, input base, manifest, prompt, topology, listener, tunnel, server identity payload, and app-scope receipt.

The separate durable disposition record distinguishes:

- `CLAIMED_NOT_INVOKED`: the boundary exists but provider invocation is not known to have started;
- `ZERO_MUTATION_PROVEN`: durable evidence proves no provider mutation, allowing a bounded retry with the same claim only;
- `INVOKED_MUTATION_UNKNOWN` or `INVOKED_MUTATION_CONFIRMED`: no resend; recover only by the exact session/history identity;
- `RECOVERED_EXACT_SESSION`: the exact provider result was attributed.

Disposition transitions cannot move from an uncertain or confirmed invocation back to a resubmittable state. Every non-initial disposition requires a hash-verified evidence file inside the child run. An unresolved required unit blocks `APPLY_READY`.

## Deterministic integration and final apply

After all required units are integrated, component heads are combined in sorted component order in the aggregate worktree. Integration conflicts do not modify canonical state and require bounded repair or recovery.

The host runs the complete registered test set on the aggregate head, then revalidates the canonical baseline and filesystem identity. The verified integration object is imported under `refs/codexpro/parallel/<parent-run-id>`. Identity is checked again after import. Canonical application uses an expected-old `update-ref` and hard reset only after proving the target is a descendant of the baseline. This is ff-only; no merge commit, force update, or worker-controlled ref is allowed.

Failures before the ref update leave canonical source unchanged. Recovery evidence remains under the parent runtime. A required unit, integration, test, or identity failure prevents apply.

## Tab ownership

Parent coordinators have no browser identity. After the common parent or strict legacy adapter validates, the tab scanner skips that parent regardless of parent phase and continues to inspect every child independently. A malformed parent, unreadable/reparse state, any direct browser identity field even when null, or any matching foreign child/run keeps cleanup fail-closed.

## Driver

The host driver is:

```text
skills/chatgpt-pro-plan-handoff/scripts/run_parallel_implementation.py
```

Commands:

```text
prepare --manifest <workflow-v3.json> --graph <implementation-graph-result-v1.json>
record-unit --parent-run-dir <run> --result <implementation-unit-result-v1.json>
status --parent-run-dir <run>
finalize --parent-run-dir <run>
```

`prepare` performs gate admission, acquires the parent lease, snapshots canonical state, clones staging, binds the graph, creates exact worktrees, and emits immutable missions/child manifests. It does not submit a browser question itself. The existing bridge consumes those child manifests after exact app/process preflight. `record-unit` validates the recovered structured result, diff, tests, metadata, and host commit. `finalize` performs deterministic integration, full tests, identity revalidation, temporary-ref import, and ff-only apply.

## Compatibility

Serial v1/v2, Deep Research, and Web Multi-GPT advisory behavior retain their existing schemas and state transitions. New non-Pro stages select the strongest declared regular-web level (`Very High` before `High`), while explicit `High` remains valid for frozen comparison arms. The fixed prompt-file handoff wording is preserved, and stage manifests persist `mode_variant`, including `null` for Pro stages. No v3 field is inferred into a v1/v2 run.
