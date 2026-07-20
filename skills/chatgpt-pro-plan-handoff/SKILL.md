---
name: chatgpt-pro-plan-handoff
description: "Use for staged ChatGPT workflows: attachment-only Pro plan then fresh regular GPT review and orchestrator, or GPT 종합모드 with fresh regular GPT plan, review, and orchestrator. All stages use one exact contract-validated agbrowse version."
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

Before the first non-Pro stage, require ChatGPT Developer Mode for the current account/workspace. When the custom app surface is unavailable, tell the user to enable `Settings > Apps > Advanced settings > Developer mode` or obtain the required admin/owner workspace permission, then stop before any stage submission. This prerequisite does not apply to the attachment-only Pro stage.

### GPT 종합모드

Every new comprehensive workflow uses `codex.chatgpt.comprehensive-workflow/v2`; v1 is recovery-only for an already persisted matching legacy workflow state.

1. Validate the structured v2 manifest and both deterministic gates before browser work.
2. Run Deep Research only when its `auto` triggers select it or the user/policy requires it; otherwise persist an immutable skip descriptor.
3. Fresh regular GPT plans constructively from the original task with the project app context and research descriptor hash. A prior plan/review is not the frame or authority.
4. Run genuine Web Multi-GPT only when its `auto` risk inputs select it or the user/policy requires it; otherwise persist an immutable skip descriptor.
5. Fresh regular GPT alone takes the adversarial posture and reviews the immutable plan plus research/advisory descriptors.
6. Continue only on `PASS`.
7. Compile an `ExecutionMission` that preserves the original task, selected plan as guidance, mandatory conditions, write scope, acceptance tests, deviation policy, and host-only boundaries.
8. Fresh regular GPT orchestrator owns live workspace exploration, decisions, edits, tests, and bounded adaptation from that mission. It does not consume the full review/advisory transcript as its cognitive frame; local deterministic verification remains required.

Gate policies are `auto`, `require`, or `skip`. An explicit user request wins. Deep Research `auto` triggers cover current external facts, broad source synthesis, legal/market/standards work, and recommendation uncertainty. Web Multi-GPT `auto` triggers cover verified contradictions, three or more affected components, two or more cross-component interfaces, security/privacy/credential/legal/financial/irreversible-state risk, shared routing, schema migration, and public release work. A routine plan does not pay the Web Multi-GPT latency cost merely because it entered comprehensive mode.

Each stage is a separate agbrowse `--parallel` session and must have a distinct canonical conversation URL. Stages remain serial within the same project.

New stages use `codex.chatgpt.prompt-architecture/v3` receipts. Purpose, cognitive frame, action authority, context policy, challenge policy, output contract, reasoning budget, and decision authority are separate fields. Unknown explicit profiles fail before submission; read-only, research, advisory, and unknown natural-language questions never silently become review. The universal anti-sycophancy suffix is retired: only review uses the adversarial module.

When review requests revision, write a compact immutable revision delta and give the next fresh Planner only that delta, not the full review prose. Web Multi-GPT treats the plan as an incumbent candidate, expands the original solution space, and may challenge its frame without becoming a plan-approval exercise. `PASS` is only a transition token.

An existing live agbrowse session is never a prerequisite. When the session list is empty, contract validation and the project lease run first, the exact executable proves `start --headed --port <exact-port>`, and only then does `web-ai send --url https://chatgpt.com/ --parallel` create the run-owned session/target with Web-AI auto-start disabled. Never start a Pro or GPT stage headlessly. A proven agbrowse-owned headless runtime may restart headed only when no other active/uncertain run or nonblank tab exists; otherwise stop once before submission with a deterministic block.

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

## Run

```powershell
python "$env:USERPROFILE\.codex\skills\chatgpt-pro-plan-handoff\scripts\run_pro_plan_handoff.py" --manifest <workflow.json>
```

Prepare immutable local artifacts without submitting:

```powershell
python "$env:USERPROFILE\.codex\skills\chatgpt-pro-plan-handoff\scripts\run_pro_plan_handoff.py" --manifest <workflow.json> --prepare-only
```

Use `codex.chatgpt.pro-plan-handoff/v1` only for `workflow_mode: pro-plan-to-gpt-orchestrator` and for recovery of an already persisted matching legacy comprehensive state with a valid stage checkpoint. A merely prepared v1 state has no recovery authority. Every new `workflow_mode: gpt-comprehensive` manifest must use `codex.chatgpt.comprehensive-workflow/v2` and include validated `gates.research` and `gates.advisory` decision inputs. A new v1 comprehensive manifest fails before network side effects with `COMPREHENSIVE_V2_REQUIRED`.

The driver validates the existing plan/review/orchestrator JSON envelope schemas and emits `final.json` only after deterministic contract checks.
