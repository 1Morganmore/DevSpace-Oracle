---
name: web-multi-gpt
description: Run genuine app-only parallel ChatGPT web Multi-GPT through one exact contract-validated, unmodified agbrowse version. Use when the user explicitly requests web Multi-GPT, or as the advisory stage of GPT 종합모드 after a fresh plan and before a fresh review. Supports legacy 2-4 lanes and opt-in upstream-parity Planner topology up to 10 lanes; never uses Pro, source attachment fallback, or release authority.
---

# Web Multi-GPT

## Boundary

This is a manager-owned advisory workflow using `codex.chatgpt.prompt-architecture/v3` role receipts:

`Planner -> parallel Solvers -> parallel assigned Refiners -> bounded Merger/Refiner/Judge -> Organizer`

The compatibility topology remains stable for persisted v1/v2 recovery, but new prompts use distinct cognitive roles: BranchDesigner, IndependentProposalBuilder, FeasibilityEngineer, SynthesisArchitect, TargetedGapCloser, RubricJudge, AlternativeSynthesizer, DecisionAuthor, and FinalResponder. Only RubricJudge is adversarial.

Every node is a fresh regular ChatGPT conversation. Do not replace it with one conversation that role-plays several agents.

- `app_policy` is always `required` for every node.
- Each node attaches its own `prompt.txt` exactly once and no source file or source ZIP.
- The selected CodexPro app reads immutable source and prior-stage paths.
- Pro is forbidden inside this workflow. Pro remains attachment-only under its own skill.
- The result is advisory only. It cannot approve implementation, release, destructive action, or skip deterministic verification.
- Subagents must not start this workflow. They return `needs-manager-decision` with a proposed manifest and evidence paths.
- Planner creates branch briefs including a direct baseline and wildcard reframe; it does not dictate full solutions. Solvers receive the original task plus their branch brief and evidence slice, never the Planner's full narrative or peer output. Synthesis roles create new coherent candidates rather than concatenate. Organizer may repair material omissions against the original task.

## Runtime

Use:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_web_multi_runtime.py" --manifest <web-multi.manifest.json>
```

Read-only validation without browser startup:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_web_multi_runtime.py" --manifest <web-multi.manifest.json> --dry-run
```

Resume only the exact parent and immutable manifest:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_web_multi_runtime.py" --manifest <web-multi.manifest.json> --resume-parent <parent-run-dir>
```

The default compatibility manifest schema remains `codex.chatgpt.web-multi/v1`. Required fields are:

- `workflow_id`
- exact normalized `project_root`
- `question`
- `source_snapshot_path` and immutable `source_snapshot_sha256`
- `output_dir` (never project identity)
- exact `chatgpt_app_name`
- `solver_count` in `2..4` (default `3`)
- `max_iterations` in `1..5` (default `2`)
- `provider_failure_retry_limit` in `0..2` (default `1`)
- optional `provider_parallel_limit` as an integer in `1..5` (default `5`); this is the hard provider-generation cap, not a topology reduction

V1 retains `mode_variant: Very High` by default; `High` remains available for its frozen comparison arm. V2 upstream-parity manifests accept the exact values `High` and `Very High`; when omitted, the strongest declared regular-web capability is selected (`Very High` before `High`). Any other value is rejected before any output, state, app, or browser side effect.

For an opt-in dynamic upstream-parity run, use schema `codex.chatgpt.web-multi/v2` with the same common required fields plus:

- `semantics_version: upstream-parity-v1`
- `planner_policy: upstream-nonempty-prefix10` for literal nonempty-prefix-10 acceptance, or `strict-6-10` for the operating policy that requires 6 through 10 approaches
- exact `mode_variant: High` or `mode_variant: Very High` (strongest declared level when omitted)
- optional `agbrowse_contract_sha256`, exactly 64 lowercase hexadecimal characters matching the SHA-256 of the resolved `agbrowse_contract` file; when omitted, the existing contract-resolution compatibility path remains supported
- no `solver_count` key at all, including `null`

V2 uses an exact top-level allowlist, including the optional `agbrowse_contract_sha256` binding and `provider_parallel_limit`, and is compiled before output directories, RunStore, parent locks, child artifacts, app checks, or browser targets can be created. A supplied contract hash is shape-checked and matched against the resolved contract file before those side effects. The accepted Planner result owns one immutable descriptor containing the observed count, retained ordered approaches, actual count, source payload hash, and descriptor hash. Do not infer topology from child counts, open tabs, capacity pressure, or a manifest field.

V1 remains byte-compatible: omitted `solver_count` means 3 and explicit values remain 2, 3, or 4. Do not silently upgrade a v1 manifest to v2.

## Upstream-Parity Scheduling

- Port selection and reduction semantics through the I/O-free `chatgpt_web_multi_upstream.py`; do not modify the workflow-selected external agbrowse package or add a second browser engine.
- Solver completion starts its same-lane InitialRefiner immediately. All accepted InitialRefiners must finish before Merger groups are computed.
- Merger completion starts its same-seed LoopRefiner immediately. All LoopRefiners must finish before Judge.
- Every provider-generation wave is chunked at `provider_parallel_limit` (maximum 5). Each submission barrier covers only submitting children in its exact chunk, preventing a 6–10 lane topology from waiting for unscheduled siblings. Chunking preserves each Solver→InitialRefiner and Merger→LoopRefiner lane pairing; it never changes the accepted topology.
- Candidate and group collectors use captured input-index slots. Completion order, stage IDs, model-returned source IDs, and result text never reorder them.
- Merger count is `min(8, n)`, group size is `min(3, n)`, and outstanding candidates receive upstream duplicate-pool weighting.
- Judge IDs are normalized from 1-based values; unknown and duplicate IDs are removed. Inadequate candidates are filtered and survivors are densely remapped. If all are inadequate, restore the original set.
- Only a nonempty proper outstanding subset narrows Final input.
- Run every planned FinalMerger. The FinalRefiner consumes the successful nonempty result in exact seed slot `0`, equivalent to upstream `finalMerge.solutions[0]`; never search for an old candidate ID 0 afterward.
- After a proven completed Solver, an InitialRefiner semantic or provider-terminal failure records immutable fallback provenance and forwards that Solver candidate unchanged. After all candidate results are durable, an Organizer semantic or provider-terminal failure records the same provenance and returns the first candidate in the final valid Judge order (or durable input order when there is no Judge order). Transport, identity, receipt, uncertainty, and cleanup errors remain hard failures. Resource pressure never reduces the Planner topology or authorizes a replacement send.
- V2 result output contains `fallback_provenance` plus role/session/target/canonical-URL provenance for every accepted stage.

## Ownership And Recovery

- One parent owns the real project `active.lock`; children never acquire or remove a second project lock.
- A second same-project parent blocks. Different normalized projects may overlap.
- Parent draining and child creation share one transition lock. A child is either completely durable before draining and included in the final scan, or rejected without artifacts or browser mutation.
- Each child has one durable O_EXCL `send.claim`, one session, one target, one canonical URL, and at most one public send.
- A transient app utility failure before any send/session/URL evidence may retry the same child only under the durable same-claim authority, bounded by both count and wall-clock deadline; it never creates a replacement child or claim.
- Recover only the recorded child run/session/target/URL while execution is active or uncertain. Never submit a replacement for an active or uncertain stage.
- Installed recovery state is evidence, not authority: a quiescent app/utility trace (including the v7/v8 incident fixtures) cannot create a Planner, child, session, target, or send. Resume only a durable exact identity tuple; otherwise preserve the parent lock and fail closed for manager adjudication.
- Diagnose a child only through its persisted project/parent/child/session/target/canonical-URL identity. Never use the currently visible tab, response subject, or another child/session as identity evidence. A matching `activeCommand.sessionId` protects the exact child helper as active; empty text, apparent stall, and elapsed time never authorize terminating it.
- A timeout or exception after the send boundary retains the parent lock until exact adjudication finishes.
- A ChatGPT stream-error banner must never count as a completed answer even when agbrowse reports `status=complete`. Preserve its immutable bytes, classify the exact child `PROVIDER_FAILED_TERMINAL`, close only its exact owned target, fail-close and release the clean parent, then let the runtime supervisor make at most the manifest's bounded fresh-workflow retry. This is permitted only after explicit terminal-failure proof; it is never an uncertainty fallback.
- The retry supervisor records a derived immutable manifest, a new workflow/parent/child identity, the failed terminal conversation evidence, and a retry-chain report. It never retries `RECOVERY_REQUIRED`, `SUBMISSION_UNCERTAIN_IDENTITY_MISSING`, an active response, ambiguous identity, or cleanup-pending work.

## App And Tab Lifecycle

- App inspection, registration, permission checks, and deletion use a fresh dedicated utility target and close it in a finally path with absence evidence.
- Before opening Settings, the first child checks the shared compact contract state at `%USERPROFILE%\.codex\state\chatgpt-agbrowse\app-contract-state.json`. An exact registry/app/root/port/registered-URL/source-evidence match plus a fresh healthy public MCP identity probe reuses the prior web verification without reopening Settings. Any mismatch or endpoint failure falls back or blocks under the regular-GPT contract; the cache never bypasses endpoint health.
- When no valid shared contract exists, the first child in one parent workflow performs the full app inspection, refreshes the bounded shared contract state, and records one hashed parent-workflow attestation. Later children reuse the parent attestation only after a fresh local registry fingerprint confirms the exact app name, scope root, server URL, and port; drift or ambiguity falls back to full verification.
- Persist child answer bytes/hash, terminal provider state, target, and canonical URL before completed-tab cleanup.
- Every run-owned completed tab is closed synchronously unless an exact cleanup recovery is already pending. Completed cleanup requires both exact target ID and exact canonical URL, one unique live match, and verified absence afterward.
- Ambiguity becomes cleanup-pending. Never use broad tab cleanup.
- Active, uncertain, manual, sibling, and foreign tabs are protected.
- Parent completion or failed-closed release requires no unresolved sends, no cleanup-pending child, and `owned_open_tabs=0`.
- An explicit stop of a submitted child is a parent-owned drain, never a child lock release. It publishes one immutable parent/child authorization, moves the parent and exact lock to `USER_STOP_REQUESTED`, and blocks child creation, resume, recovery, retry, and every new send.
- Retry a pending stop only with `chatgpt_agbrowse_bridge.py confirm-user-stop --run <exact-child-run-dir>`; it accepts no new identity or reason. Polling, blocked, timeout, active helper, URL/target ambiguity, or live owner stays pending. Terminal proof closes only the exact target+canonical URL, then the strict all-child scan may fail-close and unlink the unchanged exact lock.

## Dispatch Latency

- The first child pays the full account-app inspection cost only when the shared contract state is absent or invalid. Every later child must reuse either the valid shared contract or the hashed parent attestation after the fresh registry and endpoint checks; it must not reopen Settings on a valid reuse path.
- A warm composer fast path verifies `active-tab` first and runs `tab-switch` only after an exact target mismatch. It never reactivates another target between typing the exact `@app` mention and the single `Tab` confirmation.
- Composer resolution uses at most three fresh activate/snapshot attempts. Refs from earlier snapshots are discarded, 0 or multiple matching textboxes are never guessed, and typing happens immediately after the one exact fresh ref.
- A known rate-limit acknowledgement may be dismissed once only when the current run-owned target and ChatGPT composer route match, the rate-limit fixture text is present, and exactly one `알겠습니다` button exists. Its absence, ambiguity, or a foreign target disables clicking.
- Composer evidence records `duration_ms` and `agbrowse_command_count`. The deterministic warm path is five public agbrowse commands before the bridge's final pre-send target verification.
- The shared composer critical section may serialize the short mutation boundary, but provider generations from distinct owned targets must overlap. Performance pressure never authorizes skipping app identity, mention hash, send claim, exact target, or cleanup checks.

## GPT 종합모드

The exact order is:

1. fresh regular-GPT plan
2. fresh app-only web Multi-GPT advisory bound to that plan
3. fresh regular-GPT review bound to both immutable plan and advisory hashes
4. only after `PASS`, fresh regular-GPT orchestrator implementation

`REVISE` starts a new plan, new parent/advisory, and new review. Never reuse a prior conversation or advisory hash.

## Verification

Before treating the runtime as usable, require:

- deterministic 2-Solver and 4-Solver runs with `max_concurrent_child_generations >= 2`
- v2 cardinality fixtures for 0, 1, 5, 6, 10, and 11 approaches under both policies
- upstream five-candidate merger-group golden vectors and UTF-16/`Math.imul` score fixtures
- Solver→InitialRefiner and Merger→LoopRefiner overlap with both reducer barriers preserved
- all FinalMerger completion permutations selecting the captured seed-0 slot
- one send per child and unique session/target/canonical URL
- app-read nonce and complete path/hash/byte receipts
- exact recovery and no sibling/foreign mutation
- utility-target success/failure/interruption cleanup
- terminal `owned_open_tabs=0`
- the installed-only v7 and v8 quiescent app-trace incidents, proving that no recovery path creates a replacement send

For comparison, freeze exactly three arms: Pro attachment-only, web Multi-GPT Very High app-only, and web Multi-GPT High app-only. Freeze the same question, evidence, rubric, solver count, and iteration count, then record quality, time, recovery, context, and tab-cleanup evidence.
