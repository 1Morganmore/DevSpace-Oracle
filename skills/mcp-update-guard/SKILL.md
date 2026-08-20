---
name: mcp-update-guard
description: Part of the current Oracle automation path, safely update MCP servers, shared harness helpers, Oracle GPT runners, global skills, plugins, and related automation while preserving local customizations.
---

# MCP update guard

Use this skill for shared/global automation changes. Read the applicable
`AGENTS.md`, identify the authoritative source and installed deployment, and
preserve unrelated local customizations.

## Workflow

1. Classify the exact component and whether the work is an update,
   compatibility repair, policy refresh, or recovery fix.
2. Inspect source Git status and the installed file identity before editing.
   Never overwrite credentials, browser profiles, runtime state, or unrelated
   user changes.
3. For non-trivial GPT automation design or implementation, use the selected
   current GPT workflow only when the user asked for web delegation. Every new
   ChatGPT run uses Oracle:
   - regular modes, Deep Research, comprehensive stages, and Web Multi use
     Oracle plus the manually registered DevSpace app;
   - explicitly requested qualified Pro uses `pro-devspace`; explicitly
     requested immutable-evidence Pro uses `pro-attachment-only` with no app;
   - no route promotes itself to Pro, and no alternate browser backend is a
     fallback.
4. Prefer small compatibility changes over wholesale replacement. Preserve
   local ports, names, roots, tokens, routing, and hooks unless the task
   explicitly changes them.
5. For an upstream package check, treat npm latest/version integrity/`gitHead`,
   the annotated source-tag object and peeled commit plus signature state,
   GitHub Release, and default-branch head as independent evidence. Never call
   `releases/latest` a tag or substitute source-main bytes for the exact npm
   dist.
6. Batch coherent edits, inspect the final diff once, run focused regression
   tests, then broader tests according to blast radius.
7. Synchronize reusable GPT automation changes to the authoritative
   `codexpro-automation` source, install the verified bytes, commit with a
   descriptive message, push public-safe changes, and check CI.

## Single repair owner

Automation sources have exactly one repair owner. A project session that hits an
automation defect reports it and stops; it does not edit runners, state, patches,
or their tests. Cross-session patching previously produced duplicate fixes,
conflicting state rules, and repairs aimed at the layer that reported the symptom
instead of the layer that failed.

- Build the handover with
  `python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_incident.py" report --run-dir <exact-run-dir>`.
  The packet carries the exact run directory, the classified bucket, the
  lifecycle verdict with its authority source, and existing evidence paths.
- Classify before repairing. Run the aggregate report (no subcommand)
  `python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_diagnose.py" --summary-only`
  and fix the largest bucket rather than the newest report. `--summary-only`
  belongs to that aggregate form only: it omits the per-run `unresolved_runs`
  detail. `triage --run-dir <exact-run>` is the single-run form for one bounded
  next action and `watch --run-dir <exact-run>` streams one run's lifecycle;
  both reject `--summary-only`, so never combine the flag with a subcommand. A
  `pre-submit-*` bucket proves no web submission occurred and is safe to retry;
  a `post-submit-*` bucket requires exact-slug recovery and never a replacement
  submission. A `session-absent-awaiting-user-confirmation` run is provably
  pre-submit but still owns the project: release it only through the returned
  settle command
  (`chatgpt_oracle_run.py settle-no-submission --run-dir <exact-run> --confirmation user-confirmed-no-submission --reason <reason>`)
  after the operator confirms, never by editing state.
- Treat `safe_for_fresh_run: false` as binding. Do not resubmit, stop, or close
  another session's work while repairing code.

## Safety boundaries

- Do not delete or recreate credential-bearing state during a normal update.
- Do not use resource pressure as authority to block, terminate, downgrade, or
  duplicate user-visible work.
- Do not silently switch Oracle model, reasoning level, transport, or browser
  backend.
- Do not create a replacement run while repairing recovery code.
- Stop and report exact dirty files when authoritative persistence, push, or CI
  cannot be completed.

## Report

Report updated components, preserved customizations, focused and broad
verification, installed/source synchronization, commit/push/CI state, rollback
evidence, and any remaining risk.
