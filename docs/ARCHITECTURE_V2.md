# Agbrowse GPT Workflow Harness v2

## Goal

Keep `agbrowse` and CodexPro external, thinly connect them to deterministic GPT workflow contracts, and preserve the useful upstream Multi-GPT semantics without inventing another browser engine. The harness owns routing, evidence, recovery, concurrency, app registration, and tab ownership. It does not own the browser implementation or CodexPro releases.

Prompt cognition is versioned separately as `codex.chatgpt.prompt-architecture/v3`. The v2 workflow/state topology remains recoverable, while new prompts separate task purpose, cognitive frame, action authority, context policy, challenge policy, output contract, reasoning budget, and decision authority. Unknown explicit profiles fail instead of silently becoming review.

## Non-negotiable boundaries

- Pinned/tested `agbrowse` is the only browser command surface used by GPT workflows. No in-app browser, custom Playwright/CDP runner, Proxima, or Chrome-plugin fallback exists.
- An LLM agent decides when to check or update `agbrowse`. The project provides explicit check/update/WhatIf/rollback operations and structured diagnostics, but no scheduled, silent, or automatic updater.
- CodexPro is external and latest-on-bootstrap for each new public address. It is not vendored or pinned here. The connector validates only the endpoint/root/drive/port/protocol contract and candidate app `connected + full_access` state before committing registry changes.
- Ordinary upstream drift yields actionable diagnostics for the LLM agent. Only safety invariants fail closed: duplicate submission risk, post-send uncertainty, foreign/ambiguous tab ownership, invalid immutable artifact bindings, or unproven requested capability.
- New regular GPT, Deep Research, and Web Multi-GPT work uses ChatGPT `High`. Legacy Very High runs are recoverable but cannot create replacement sends.
- Pro remains attachment-only and cannot select an app.

## Comprehensive workflow

The v2 pipeline is:

1. Validate a structured local manifest and persist immutable input hashes.
2. Run the deterministic Deep Research gate.
3. Run Deep Research when selected; otherwise persist an explicit skip artifact.
4. Run a fresh regular-GPT plan, always bound to the immutable research descriptor hash. The descriptor's `artifact` field is exactly `null` when research was skipped.
5. Run the deterministic Web Multi-GPT gate from validated user inputs and plan risk descriptors.
6. Run genuine upstream-parity Web Multi-GPT when selected; otherwise persist an explicit skip artifact.
7. Run a fresh adversarial review bound to plan, research, and advisory hashes.
8. Compile a narrow `ExecutionMission`; run the adaptive orchestrator from the original task, mission, live workspace, and nonbinding plan rather than the full review/advisory transcript.
8. Only after `PASS`, run a fresh orchestrator bound to every prior artifact.
9. Require local deterministic verification before declaring completion.

The gates accept `auto`, `require`, and `skip`. An explicit user request or `require` wins. `auto` selects Deep Research for current external facts, broad source synthesis, legal/market/standards questions, or recommendation uncertainty. `auto` selects Web Multi-GPT for contradictions, cross-component architecture, security/privacy/credential/legal/financial/irreversible-state risk, shared routing, schema migration, or public-release work. Invalid or missing gate inputs stop before network side effects.

### Versioned artifacts and bindings

New comprehensive runs use workflow schema v2 and never silently rewrite a v1 artifact. Every optional stage produces one immutable descriptor, even when skipped:

```json
{
  "stage": "deep-research",
  "decision": "run | skip",
  "gate_sha256": "<64 hex>",
  "artifact": {"path": "...", "sha256": "<64 hex>"}
}
```

For `skip`, `artifact` is exactly `null`; the descriptor itself still has a hash. Field absence is invalid. Plan binds the research descriptor hash. Review binds the plan, research descriptor, and advisory descriptor hashes. Orchestrator binds plan, research descriptor, advisory descriptor, and review hashes. The JSON schemas use `additionalProperties: false` for binding objects and reject absent-versus-null substitutions.

The entry contract rejects a new v1 comprehensive manifest with `COMPREHENSIVE_V2_REQUIRED` before browser side effects. V1 comprehensive manifests are accepted only when the output directory already contains a matching immutable legacy state, valid stage checkpoint, source snapshot, and source archive proving that the same workflow is being recovered. A merely prepared v1 state has no recovery authority.

The durable state sequence is:

`PREPARED → RESEARCH_DECIDED → RESEARCH_COMPLETE|RESEARCH_SKIPPED → PLAN_COMPLETE → ADVISORY_DECIDED → ADVISORY_COMPLETE|ADVISORY_SKIPPED → REVIEW_PASS → ORCHESTRATOR_COMPLETE → LOCAL_VERIFY_REQUIRED`.

Each executed-stage descriptor stores `manifest_sha256`, `run_id`, `session_id`, `target_id`, and canonical URL. Each transition is written atomically before the next stage can acquire authority.

Session discovery is recovery evidence, not a creation prerequisite. If no agbrowse session is alive before a new stage, the bridge proves the validated executable is running headed with `start --headed --port <exact-port>` before any composer/send mutation, then disables Web-AI auto-start and uses `web-ai send --url https://chatgpt.com/ --parallel` to create the run-owned session/target. An empty session list cannot defer Pro, regular GPT, or a comprehensive stage. A headless runtime is restarted only after the global dispatch lock proves no other active/uncertain run and the public tab list proves every tab blank; otherwise the stage fails once before submission.

## Upstream-parity Web Multi-GPT

The advisory stage preserves `hehee9/multi-gpt@4f5e130` semantics:

New role prompts preserve the topology while preventing cognitive collapse: Planner emits branch briefs with a direct baseline and wildcard reframe; each Solver independently builds from the original task and one brief without the Planner narrative; only Judge is adversarial; synthesis roles create new coherent candidates; Organizer restores original-task fidelity. Role-specific evidence slices replace all-files-to-all-roles exposure.

## Exact run observation

`chatgpt_agbrowse_run.py --observe-run <exact-run-dir>` is the read-only status surface. It compares the persisted normalized project/project-key/run/session/target/canonical-URL tuple against exact session output, live tabs, and `activeCommand.sessionId` without activating, navigating, stopping, typing, or closing. Visible content, the active tab, another task's URL, elapsed time, and an empty capture are never identity or termination evidence.

- Planner produces up to ten nonempty approaches.
- Independent Solvers run concurrently on separate ChatGPT sessions/targets.
- Initial Refiners run independently; a failed refiner falls back to its input.
- Parallel Mergers use the upstream bounded group/weight/seed rules.
- Loop Refiners and a Judge repeat until adequate or the iteration limit.
- If all candidates are inadequate, the pre-merge set is restored and Final Merger/Final Refiner run.
- Organizer produces the final result, with a deterministic best-candidate fallback.

Concurrency is dynamic from the plan; there is no fixed solver count. Every role records a distinct run/session/target/canonical URL and immutable provenance. Map-style concurrency preserves logical input order.

The two upstream fallbacks are explicit result states rather than swallowed errors. `InitialRefiner` failure records the immutable failure envelope and forwards the paired Solver candidate unchanged. `Organizer` failure records its envelope and returns the deterministic best candidate selected by the final Judge order, or input order if no valid Judge order exists. Provider submission uncertainty is never converted to a semantic fallback.

## Deep Research selection

Deep Research is an app-backed composer feature. Its immutable manifest uses `app_policy: required`, the exact `chatgpt_app_name`, `research_selection_transport: "preselected-research"`, and `research_selection_contract: "codex.chatgpt.capability-selection/v1"`. One prepared run-owned ChatGPT target first receives `@<exact-app-name>` plus Tab and then the exact UTF-8 token `@심층 리서치` plus Tab. The bridge persists and verifies both action proofs before `SEND_STARTED`, activates the exact target, and calls send with `--reuse-tab --research deep`. Either missing proof fails closed without submission.

One exclusive global composer lease begins before target creation and remains held without a handoff gap through mention typing, Tab selection, post-selection proof, final exact-target activation/state check, and the durable `SEND_STARTED` transition. The evidence cannot be reused by another run or after any intervening target mutation. Missing, stale, or ambiguous proof closes only the exact pre-submit target and stops before send; it never falls back to ordinary GPT or the app-selection path.

## State, locking, and recovery

One active or uncertain workflow is allowed per normalized project-root hash. Different projects use distinct parallel sessions/targets. Project identity never comes from an output directory.

Each stage journals its immutable descriptor and send boundary before execution. After the send boundary, failures retain the lock and recover the exact recorded run first. The canonical conversation URL plus the unique run-owned prompt filename are job identity; target IDs are locators/ownership evidence, while PIDs, heartbeats, locks, and local session status are diagnostics. Recovery observes one unique exact canonical URL without navigation, then uses the exact owned target, and only when the URL is missing performs bounded read-only history adjudication. Polling never uses `--navigate` on a URL-bound run. It never infers “not sent” from a click trace and never creates a replacement while ownership remains unresolved.

Completion requires terminal provider state, a nonempty current-run answer, immutable capture hashes, and durable result state. A dead local owner is not proof of completion or absence; it triggers adjudication.

An exact same-project recovery collision with an already `COMPLETE` URL owner is terminally settleable only when project identity and prompt hash match and the authoritative result file, hash, byte count, provider status, and evidence descriptor all verify. The authoritative run remains `COMPLETE`; the dead stale duplicate becomes `COMPLETE_SUPERSEDED` with an immutable proof and releases only its orphan lock. Cross-project, different-prompt, active, incomplete, ambiguous, or invalid-result owners remain blocked.

## Tab ownership and cleanup

The user has explicitly authorized automatic cleanup of completed work tabs for this workflow family. The harness automatically closes an exact run-owned completed original or child target only after durable `COMPLETE`, exact canonical URL ownership, a unique live URL match, no foreign owner, and re-list verification. This is a universal lifecycle rule, including legacy runs recovered to durable `COMPLETE`; it does not depend on a second explicit cleanup request or on the presence of a manifest flag. New manifests record `cleanup_policy=owned-complete-auto-v1` for auditability, not as the source of cleanup authority. The result remains valid if close fails; `cleanup_pending` permits only an exact retry.

Active, uncertain, user-stopped-but-unconfirmed, manual/unowned, foreign, and ambiguous targets are protected. A provider failure is protected unless it has been durably classified as `PROVIDER_FAILED_TERMINAL` with immutable failure capture; only that exact run-owned failed target may then close before the bounded same-mode retry. Exact run-owned pre-submit composers and completed recovery utility targets follow the same evidence-first close rule. Broad idle/pool cleanup is never ownership authority.

App settings operations use a separate newly created utility target. The connector snapshots the tab list before creation, rejects a `new-tab` result whose ID already existed, pins that exact new ID, and rejects any later snapshot target drift. Navigation, typing, and close are therefore unavailable against a Pro/GPT conversation target during app inspection, registration, permission, or deletion.

## App connector

Every non-Pro mode requires the exact drive-scoped CodexPro app; Pro alone is attachment-only. The connector calls only public `agbrowse` commands and deterministic role/name refs. It verifies exact app name, full server URL, connected state, and `full_access` before registry commit. Old-app cleanup occurs only after the candidate commits successfully. Contract receipts allow repeated questions to skip redundant registry checks when the active URL and registered URL still match.

Host routing is policy-driven and drive-scoped: this machine reuses the configured C-drive fixed-ngrok app for every C-drive project, while each other drive uses its own dynamic Cloudflare app. Public defaults remain portable and Cloudflare-oriented. A C-drive fixed-contract mismatch blocks and cannot fall back to a project-specific app.

## Updates and external dependencies

The installer records a tested `agbrowse` baseline and contract hash. `doctor` reports installed version, executable hash, public-command capability, and the exact explicit command an LLM agent can use to check/update/revalidate/rollback. Version drift alone is diagnostic, not permanent lockout; the agent decides whether a contract refresh is safe. No background updater, long-lived candidate slot, or promotion pointer is installed.

An explicit update is a short transaction: acquire the local dependency-update lock, record prior npm version/integrity/executable/contract hashes, install the agent-selected version, capture and validate its public-command contract, write an activation receipt, then release the lock. Any install or validation failure immediately restores the recorded prior version and contract. The inverse checks npm's exit status, re-queries the exact recorded registry integrity, and proves restored version plus executable SHA-256 before it restores metadata or reports success. Active or uncertain GPT runs make the update command return a structured `DEFER_ACTIVE_WORK` diagnostic; they are never killed. `-WhatIf` reports the exact transaction without mutation.

CodexPro is fetched as `codexpro@latest` during public-address bootstrap. The harness records the resolved version and connector evidence for diagnosis but neither redistributes nor freezes it. If a new candidate app cannot prove its narrow connection contract, it is not promoted and the previous healthy registration remains intact.

## Public distribution

- Project-owned glue is MIT licensed.
- The upstream Multi-GPT MIT notice and provenance are preserved in third-party notices.
- `agbrowse` remains an external npm dependency; its package metadata and provenance are documented without copying its source.
- Codex, CodexPro, browser binaries, accounts, credentials, generated profiles, runtime state, and prompt/result artifacts are never distributed.
- The README includes an OpenAI non-affiliation and trademark disclaimer.
- A single install manifest drives install, doctor, update, and uninstall.
- Normal install first invokes side-effect-free `update.ps1 -Preflight`. It validates tools, protected run state, exact selected registry integrity, current dependency identity, and lock availability, returning a token bound to those inputs without reserving the lock. Only then does install stage and commit manifest files; the actual update reacquires the lock and rejects any token drift, causing the install file inverse. Receipt v3 records created files, overwritten-file backup hashes, installed hashes, and the exact dependency-update receipt that owns npm/contract/update-receipt pre-state. Failure after dependency mutation still runs that recorded inverse. `rollback` preflights dependency identity before touching installed files; drift returns `CONFLICT` with files unchanged. When all installed bytes remain receipt-owned, rollback restores the exact prior files and dependency state. A late npm failure or failed inverse proof after a clean preflight returns `PARTIAL` and never claims completion. `doctor`, `update`, `uninstall`, and `rollback` consume compatible receipt schemas, support `-WhatIf`, and use dated, bounded backups with machine-independent Python/Node/npm discovery.

New workflow schema v2 defaults to High. V1 manifests remain recovery-only with their original mode and evidence schema, but the universal exact-owned durable-`COMPLETE` automatic cleanup rule applies after recovery. A dry-run migration command validates a v1 manifest, removes fixed `solver_count` only with an explicit diagnostic, and emits a new v2 manifest; it never mutates old evidence. CodexPro bootstrap receipts record the resolved `codexpro@latest` package version and executable identity for audit without pinning it.

## Verification and release gates

The deterministic suite covers gate truth tables, High-only zero-side-effect rejection, hash/null tampering, upstream topology fixtures, Deep Research token/capability proof, same-project serialization, cross-project parallelism, exact uncertain recovery without duplicate send, completed-tab cleanup/protection, app candidate-first commit, latest-on-bootstrap argv, portability/secrets/licenses, and temp-`CODEX_HOME` install/doctor/uninstall/rollback.

Live tests are separate opt-in release checks. They verify the current ChatGPT UI marker for `@심층 리서치`, app registration permissions, and two-project concurrency without making live availability a unit-test dependency.
