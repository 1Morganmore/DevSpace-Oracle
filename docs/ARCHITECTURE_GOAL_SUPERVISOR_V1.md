# Durable Comprehensive-Goal Supervisor v1

## Decision

The implementation is a small headless outer supervisor around the existing comprehensive-v4 driver. It does not add a service, database, browser engine, tab detector, or visible console. A visible persistent console was rejected because it adds Windows focus and process-lifecycle failure modes; `status` is a read-only mechanical command over durable artifacts.

## Architecture

`chatgpt_goal_supervisor.py` owns goal phases, cycle and stagnation budgets, write-ahead cycle intents, exact web-authored next-mission bytes, registered host checks, completion acceptance, user boundaries, and repair recurrence accounting. `goal_cycle_adapter.py` creates one immutable v4 workflow per cycle and delegates all browser submission, canonical URL ownership, relay, Web Multi-GPT, recovery, cleanup, and 60-second waiting to the existing v4/agbrowse implementation.

The outer phases are `CREATED`, `CYCLE_READY`, `WEB_ACTIVE`, `HOST_VERIFYING`, `REPAIR_ACTIVE`, `WAITING_USER`, `GOAL_COMPLETE`, and `FAILED_CLOSED`. `goal-state.json` contains only a bounded cursor and artifact references. Semantic content is immutable under `cycles/NNNN/`; events are append-only and bounded.

Each completing orchestrator returns `codex.chatgpt.goal-cycle-result/v1` with exactly one decision: `CONTINUE`, `GOAL_COMPLETE`, or `USER_ACTION_REQUIRED`. `CONTINUE` must carry the next exact mission. `GOAL_COMPLETE` must carry a web-authored fallback mission for deterministic gate failure. The host validates identity and hashes and writes those UTF-8 bytes without paraphrasing.

Completion requires the web decision, satisfied declared criteria, complete implementation status, no blockers, and all fixed check-registry commands passing. Model-provided command strings are audit data only and are never executed. Commit and push authorization are recorded separately and never inferred from completion.

## Restart and duplicate prevention

Before invoking v4, the supervisor writes an immutable `run-intent.json` and points durable state at it. A restart in `WEB_ACTIVE` re-enters the same cycle manifest. The v4 driver reuses immutable stage checkpoints and searches for the exact existing run before any new start, so an uncertain or crossed send is recovered rather than duplicated. Mechanical `status` never invokes the cycle adapter or browser observer.

## Repair boundary

Only allowlisted automation fault codes participate in recurrence accounting. The first occurrence stops with exact evidence. A second identical fingerprint may launch one hidden, non-interactive Codex CLI repair transaction only when both goal policy and `CODEX_CHATGPT_AUTOMATIC_REPAIR=1` enable repair. Attempts are capped at two per family. The fixed repair prompt forbids target-project and browser mutation, preserves the exact web run, and requires focused tests, installed-deployment synchronization, authoritative-source commit/push, and passing CI. The original web cycle resumes only after a schema-validated receipt proves all invariants; otherwise the supervisor stops at a user boundary.

## CLI

```powershell
python "$env:USERPROFILE\.codex\skills\chatgpt-pro-plan-handoff\scripts\run_comprehensive_goal.py" prepare --manifest <goal.json>
python "$env:USERPROFILE\.codex\skills\chatgpt-pro-plan-handoff\scripts\run_comprehensive_goal.py" run --manifest <goal.json>
python "$env:USERPROFILE\.codex\skills\chatgpt-pro-plan-handoff\scripts\run_comprehensive_goal.py" resume --manifest <goal.json>
python "$env:USERPROFILE\.codex\skills\chatgpt-pro-plan-handoff\scripts\run_comprehensive_goal.py" status --manifest <goal.json>
```
