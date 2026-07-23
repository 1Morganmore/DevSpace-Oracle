---
name: chatgpt-pro-plan-handoff
description: "Use for staged ChatGPT workflows: attachment-only Pro plan then fresh regular GPT review and orchestrator, GPT 종합모드, or explicitly gated v3 parallel implementation with exact-unit workspaces and ff-only finalization."
---

# Staged plan, review, and orchestrator

All stages use one immutable workflow-selected, contract-validated, unmodified agbrowse version through `chatgpt_agbrowse_bridge.py`; `0.1.18` is the tested default. No in-app, direct-CDP, browser-use, or Chrome-plugin stage exists.

## Modes

### Pro plan handoff

1. Freeze the selected source and policy files into one content-addressed ZIP.
2. Fresh Pro conversation creates the plan from that attachment only.
3. Fresh regular GPT conversation reviews the immutable plan.
4. Continue only when the review envelope says `PASS`.
5. Fresh regular GPT orchestrator consumes the immutable plan, review, and handoff manifest.

Pro never uses an app. Every non-Pro stage—including plan, review, orchestrator, Web Multi-GPT, and Deep Research—must use the exact project app. Missing app identity blocks the workflow before submission.

### GPT 종합모드

Every new comprehensive workflow uses `codex.chatgpt.comprehensive-workflow/v4` with `relay.mode: web-native-v1`. V1 and v2 are recovery-only for already persisted matching legacy workflow state. V3 remains the separate parallel-implementation contract and is never repurposed as the relay workflow.

Local Codex token use is a hard efficiency invariant. Detailed plan, review, advisory, mission, and implementation content stays in immutable handoff files authored by the completing web stage. The host consumes only schemas, paths, hashes, verdicts, and bounded receipts; it must not rehydrate entire answer bodies or repeatedly poll unchanged state into the local model.

#### Thin one-minute runner cadence

After a comprehensive stage crosses the send boundary, unchanged waiting must not create repeated local-model turns. Invoke the existing hidden runner once and wait for its final response. The same runner process owns the entire `60-second sleep -> exact compact terminal check -> 60-second sleep` loop; it must not return `EXACT_ACTIVE` to Codex merely to schedule the next check.

- While the exact run remains active, the runner sleeps for 60 seconds and checks again internally. Do not end and restart the Codex turn, emit a progress narrative, inspect logs/DOM, load the answer body, call another observer, or start another browser helper.
- When the exact run completes, the runner returns one terminal receipt and the workflow advances immediately through the existing immutable result, structured-contract, cleanup, and next-stage path.
- Any non-active observation state is actionable. Return one bounded receipt and stop the internal wait loop; never convert identity mismatch, missing identity, absent target, terminal-pending-capture, uncertainty, or malformed observation into repeated waiting or a replacement submission.
- A user status request may trigger one immediate compact observation without replacing or duplicating the existing runner.
- This is the existing runner's wait policy, not a new detector, daemon, dashboard state machine, manifest field, browser authority, or lifecycle schema. PID, heartbeat, lock, active tab, and elapsed time remain diagnostic only.

1. Validate the structured v4 manifest, web-native relay mode, and both deterministic gates before browser work.
2. Run Deep Research only when its `auto` triggers select it or the user/policy requires it; otherwise persist an immutable skip descriptor.
3. Fresh regular GPT plans constructively from the original task with the project app context and research descriptor hash. It also authors the complete semantic prompt for the next review or Web Multi stage; local Codex validates and materializes those exact UTF-8 bytes without rewriting them.
4. Run genuine Web Multi-GPT only when its `auto` risk inputs select it or the user/policy requires it; otherwise persist an immutable skip descriptor.
5. Fresh regular GPT alone takes the adversarial posture and reviews the immutable plan plus research/advisory descriptors. On `REVISE` it authors the next Planner prompt and compact revision delta; on `PASS` it authors the orchestrator prompt and implementation mission.
6. Continue only on `PASS`.
7. Bind the web-authored mission into an `ExecutionMission` that preserves the original task, selected plan as guidance, mandatory conditions, write scope, acceptance tests, deviation policy, and host-only boundaries.
8. Fresh regular GPT orchestrator receives the reviewer-authored prompt and owns live workspace exploration, decisions, edits, tests, bounded adaptation, and all expensive strategy/implementation branches. Local Codex performs only deterministic binding/transport/recovery and final verification.

The stage relay is `codex.chatgpt.stage-relay/v1`. Its semantic payload is immutable and branch-specific. The host adds a separate deterministic binding wrapper containing the hashes that became known only after the previous web stage completed; this avoids self-referential hashes and prevents local prompt authoring. Unknown keys, wrong workflow/stage bindings, invalid UTF-8, `???`, replacement characters, overwrites, and cross-workflow reuse fail before the next submission.

Same-project browser submission remains one active or uncertain web run at a time. That serialization does not make implementation locally serial: the single orchestrator `ExecutionMission` must split safe independent exploration, editing, and test work into internal lanes or parallel tool calls and integrate them itself. Local Codex must not interpret global tool-parallelism guidance as permission to start local strategy search, code authoring, alternate implementation paths, or delegated execution. Its work is limited to prompt/manifest transport, recovery, hashes and locks, exact browser identity, host-only safety/release actions, and final deterministic verification.

Gate policies are `auto`, `require`, or `skip`. An explicit user request wins. Deep Research `auto` triggers cover current external facts, broad source synthesis, legal/market/standards work, and recommendation uncertainty. Web Multi-GPT `auto` triggers cover verified contradictions, three or more affected components, two or more cross-component interfaces, security/privacy/credential/legal/financial/irreversible-state risk, shared routing, schema migration, and public release work. A routine plan does not pay the Web Multi-GPT latency cost merely because it entered comprehensive mode.

Each stage is a separate agbrowse `--parallel` session and must have a distinct canonical conversation URL. Stages remain serial within the same project.

New stages use `codex.chatgpt.prompt-architecture/v3` receipts. Purpose, cognitive frame, action authority, context policy, challenge policy, output contract, reasoning budget, and decision authority are separate fields. Unknown explicit profiles fail before submission; read-only, research, advisory, and unknown natural-language questions never silently become review. The universal anti-sycophancy suffix is retired: only review uses the adversarial module.

When review requests revision, write a compact immutable revision delta and give the next fresh Planner only that delta, not the full review prose. Web Multi-GPT treats the plan as an incumbent candidate, expands the original solution space, and may challenge its frame without becoming a plan-approval exercise. `PASS` is only a transition token.

An existing live agbrowse session is never a prerequisite. When the session list is empty, contract validation and the project lease run first, the exact executable proves `start --headed --port <exact-port>`, and only then does `web-ai send --url https://chatgpt.com/ --parallel` create the run-owned session/target with Web-AI auto-start disabled. Never start a Pro or GPT stage headlessly. A proven agbrowse-owned headless runtime may restart headed only when no other active/uncertain run or nonblank tab exists; otherwise stop once before submission with a deterministic block.

## Parallel implementation v3

Use this path only for a new `codex.chatgpt.comprehensive-workflow/v3` manifest with both `features.parallel_implementation_v1: true` and `CODEX_CHATGPT_PARALLEL_IMPLEMENTATION_V1=1`. Validate both gates before creating state. Never migrate or downgrade a v1/v2 workflow into v3.

1. A Pro planner produces the approved plan and strict `implementation-graph-result-v1`.
2. `run_parallel_implementation.py prepare` acquires the sole `parallel-implementation` parent lease, captures canonical baseline identity, creates an independent staging clone with `--no-local --no-hardlinks --no-checkout`, binds every dependency/conflict edge into deterministic components, and emits exact unit missions.
3. Each implementer uses the strongest explicitly account-attested regular reasoning level (safe default `High`) and receives one `execution-mission-v2`, one exact unit root, immutable `input_base_oid`, explicit claimed paths, registered test IDs, and no Git authority. Start it only through `codexpro_exact_unit_cloudflare_bootstrap.ps1` after topology, singleton roots, listener, tunnel, server, and app identity attestations pass.
4. Persist `child-send-claim/v2` before provider invocation. A mutation-possible or confirmed attempt is never resubmitted; recover only the exact session/history. A durable zero-mutation proof may retry the same claim only.
5. `record-unit` derives the actual diff and rejects out-of-scope, reparse, gitlink, and common Git metadata mutation. The host runs registered tests and creates the deterministic commit.
6. Only independent components run in parallel. One component has at most one active unit, and its next unit starts from the current component integration head.
7. `finalize` is allowed only after every required unit is integrated. It deterministically integrates component heads, runs full registered tests, revalidates canonical identity, imports a temporary reserved ref, revalidates again, and performs ff-only apply.

The driver itself does not create a browser submission. It prepares exact child manifests for the existing bridge, which remains responsible for the immutable send boundary, exact provider recovery, and tab ownership checks.

## Safety

- Freeze prompt, source snapshot, ZIP, plan, review, and handoff hashes.
- Materialize every stage's complete instructions as `prompt.txt`, attach it exactly once, and send only the fixed short prompt-file handoff through the composer. For Pro source ZIP work, attach `prompt.txt` alongside the content-addressed ZIP; never expose the task body through `--prompt`.
- A stage may not rewrite a prior artifact.
- Same-project active or uncertain state blocks the next stage.
- A proven `PROVIDER_FAILED_TERMINAL` stream error is neither active nor uncertain: the shared runner records and cleans only that exact failed target, then performs its bounded same-mode retry. No stage advances until the retry produces a valid immutable result.
- An interrupted stage first recovers its recorded agbrowse session. If doctor lost the URL, automatically adjudicate bounded ChatGPT history using the stage's run-owned prompt filename, or the legacy high-entropy nonce plus immutable corroborators, and rebind only the exact original stage URL.
- Before diagnosing, polling, or recovering a stage, use `chatgpt_agbrowse_run.py --observe-run <exact-run-dir>` and require the persisted project/run/session/target/canonical-URL tuple. Never identify a stage from visible content, the active tab, another task's URL, or elapsed time. A matching `activeCommand.sessionId` protects the exact helper as active; zero captured text or apparent stalling is not termination authority.
- An explicit user stop uses `chatgpt_agbrowse_run.py --abandon-uncertain-run <exact-run-dir> --explicit-user-request --reason <reason>`. `USER_STOP_REQUESTED` and `ABANDONED_UNCERTAIN` both fail the stage; the former retains its project lock and the latter releases only that run's lock without producing a stage artifact.
- A missing URL after submission retains the stage lock while exact-session plus history adjudication runs. Only an exact matched `COMPLETE` stage may unblock the next stage; unresolved or ambiguous evidence remains blocked without a replacement submission.
- If a dead stale stage's exact doctor/history match points to a same-project, same-prompt URL already owned by a valid `COMPLETE` run, preserve the authoritative answer and both records, mark the stale duplicate `COMPLETE_SUPERSEDED`, release only its orphan lock, and continue. Any weaker or foreign collision remains blocked.
- A non-PASS review stops before orchestration.
- Automatically close every stage tab after that exact stage reaches durable `COMPLETE`, its immutable result is captured, its canonical URL has one unique live match, no foreign owner exists, and absence is re-verified. Active, uncertain, manual/unowned, foreign, and ambiguous tabs remain protected; cleanup failure records `cleanup_pending` and blocks only unsafe stage advancement.
- Never use resource pressure as authority to skip review, kill another project, or switch browser engines.

## Durable comprehensive goal supervisor

For one continuing goal, use `codex.chatgpt.goal-supervisor/v1` and `run_comprehensive_goal.py`. The additive outer supervisor seals exact UTF-8 mission bytes, runs existing comprehensive-v4 cycles, accepts only `CONTINUE`, `GOAL_COMPLETE`, or `USER_ACTION_REQUIRED`, executes only registered deterministic check IDs, and requires both web completion and host gates before final acceptance. Restarting reuses the same cycle manifest and existing v4 exact-run recovery; status reads durable JSON only and never starts a browser observer or model call.

The default is headless. A visible persistent console was deliberately not added because it would add Windows focus and lifecycle risk without improving authority. Mechanical status is available through the same CLI and renders from `goal-state.json`, immutable cycle results, gate receipts, and boundaries.

Normal waiting remains inside the existing hidden comprehensive runner at its 60-second cadence. The outer supervisor invokes one inner v4 cycle and receives one terminal/actionable result; it does not create intermediate Codex wakeups. Automatic repair is disabled unless both the immutable goal policy enables it and `CODEX_CHATGPT_AUTOMATIC_REPAIR=1`; only a second occurrence of an allowlisted deterministic automation fault can launch one hidden bounded Codex CLI repair transaction, with at most two attempts per family. The web cycle resumes only after the repair receipt proves exact-run preservation, no replacement submission, focused tests, installed synchronization, authoritative-source commit/push, and passing CI. Target-project commit and push are never implied by goal completion and require explicit original-goal policy grants.

```powershell
python "$env:USERPROFILE\.codex\skills\chatgpt-pro-plan-handoff\scripts\run_comprehensive_goal.py" prepare --manifest <goal.json>
python "$env:USERPROFILE\.codex\skills\chatgpt-pro-plan-handoff\scripts\run_comprehensive_goal.py" run --manifest <goal.json>
python "$env:USERPROFILE\.codex\skills\chatgpt-pro-plan-handoff\scripts\run_comprehensive_goal.py" resume --manifest <goal.json>
python "$env:USERPROFILE\.codex\skills\chatgpt-pro-plan-handoff\scripts\run_comprehensive_goal.py" status --manifest <goal.json>
```

## Run

```powershell
python "$env:USERPROFILE\.codex\skills\chatgpt-pro-plan-handoff\scripts\run_pro_plan_handoff.py" --manifest <workflow.json>
```

Prepare immutable local artifacts without submitting:

```powershell
python "$env:USERPROFILE\.codex\skills\chatgpt-pro-plan-handoff\scripts\run_pro_plan_handoff.py" --manifest <workflow.json> --prepare-only
```

Prepare and advance an explicitly gated v3 implementation:

```powershell
$env:CODEX_CHATGPT_PARALLEL_IMPLEMENTATION_V1 = "1"
python "$env:USERPROFILE\.codex\skills\chatgpt-pro-plan-handoff\scripts\run_parallel_implementation.py" prepare --manifest <workflow-v3.json> --graph <implementation-graph-result-v1.json>
python "$env:USERPROFILE\.codex\skills\chatgpt-pro-plan-handoff\scripts\run_parallel_implementation.py" record-unit --parent-run-dir <parent-run> --result <implementation-unit-result-v1.json>
python "$env:USERPROFILE\.codex\skills\chatgpt-pro-plan-handoff\scripts\run_parallel_implementation.py" finalize --parent-run-dir <parent-run>
```

Use `codex.chatgpt.pro-plan-handoff/v1` only for `workflow_mode: pro-plan-to-gpt-orchestrator` and for recovery of an already persisted matching legacy comprehensive state with a valid stage checkpoint. V2 has the same recovery-only status. A pre-v4 state without manifest identity must first pass its immutable snapshot, archive, and stage-checkpoint checks; the driver then pins the current manifest schema and exact SHA-256 once, and every later recovery requires an exact match. A merely prepared legacy state has no recovery authority. Every new `workflow_mode: gpt-comprehensive` manifest must use `codex.chatgpt.comprehensive-workflow/v4`, the exact web-native relay mode, and validated `gates.research` and `gates.advisory` inputs. A new v1 or v2 comprehensive manifest fails before network side effects with `COMPREHENSIVE_V4_REQUIRED`.

The driver validates the existing plan/review/orchestrator JSON envelope schemas and emits `final.json` only after deterministic contract checks.
