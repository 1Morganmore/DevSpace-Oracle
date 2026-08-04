---
name: multi-gpt
description: Run PC-local parallel-reasoning Multi GPT through the async multi_gpt_start, multi_gpt_status, and multi_gpt_cancel MCP tools. Use for broad alternatives, comparison axes, prompt shaping, counterexample discovery, large-context synthesis, or critique of a completed GPT answer. Do not use for simple one-shot advice or as approval, release, or deterministic verification authority.
---

# Multi GPT

## Purpose

Multi GPT is a manager-owned, read-only advisory pipeline: Planner -> parallel Solvers -> Refiners -> Merger/Judge -> Organizer. It is useful when several independent approaches are worth the runtime and context cost.

Do not use it for:

- short questions or one-off coding advice
- evidence-bound review of a concrete local artifact when `independent-review` is the better route
- web freshness or source authority
- approval, release, implementation entry, or deterministic verification
- replacing a live ChatGPT web lane that still owns the same question

Subagents must not start Multi GPT. They return `needs-manager-decision` with a proposed prompt and evidence paths.

## Orphan Cleanup Only

Do not run a resource preflight before Multi GPT and never gate a run on memory, CPU, or helper count. If parent-missing helper accumulation is actually suspected, use the orphan-only cleanup path:

```powershell
python "$env:USERPROFILE\.codex\bin\mcp_resource_guard.py" --cleanup-orphans --json
```

Do not kill live Multi-GPT, Codex, Chrome, or browser work because counts are high.

## Start And Monitor

Call `multi_gpt_start` with:

- `prompt`: the bounded problem and requested output
- `files`: absolute read-only evidence paths. Each attachment is inlined verbatim into every
  stage prompt, so the caps are a context budget, not an I/O limit: 512 KB per file and
  768 KB total. Oversized input fails at intake with `file too large` or
  `total file context too large`; pass an excerpt instead of a whole large document. Files
  must resolve inside `MULTI_GPT_ALLOWED_ROOTS_JSON`, a JSON array of narrow absolute
  directories configured on the MCP server. File-backed jobs fail closed when it is
  unset, and filesystem or home-wide roots are rejected. Canonical-path containment, symlink/junction rejection,
  sensitive-name and high-confidence secret-content denial, UTF-8 validation,
  byte length, and SHA-256 provenance are enforced
  before content reaches any child.
- `model`: omit it or specify exactly `gpt-5.6-luna`; every other value is rejected before any child starts
- `reasoning_effort`: omit it or specify exactly `max`; every other value is rejected before any child starts
- `max_iterations`: `1` for prompt shaping, `2` for broad critique, `3` for architecture comparison, and `5` only for explicit heavy use

The tool returns a `job_id`. Runs commonly take 5-20 minutes. Poll `multi_gpt_status` sparingly until `completed`, `failed`, or `canceled`.

Actual Codex child creation is protected by a server-wide FIFO semaphore. The
default is four active children across all jobs; set `MULTI_GPT_MAX_CHILDREN`
to an integer from 1 through 20 before starting the MCP server when a different
host-safe limit is required. Stage fan-out may queue more logical work, but it
cannot bypass this process limit.

Use `multi_gpt_cancel` only for a mistakenly started or explicitly stopped job owned by the current MCP server. Job state is revisioned and atomically persisted with a last-known-good backup. After an MCP server restart, a provably dead owner is reconciled to `failed / ORPHANED_AFTER_RESTART`; a live or ambiguous foreign owner remains read-only and cannot be canceled by the new process. Do not expose partial hidden stage reasoning as a user answer.

## Lane Selection

- Use Multi GPT for alternatives, comparison axes, counterexamples, prompt design, or post-GPT critique.
- Use `independent-review` for a concrete plan, code change, investigation conclusion, architecture decision, or proof packet.
- When both help, Multi GPT shapes the axes or packet and the reviewer inspects the concrete evidence. Do not ask both the same question.
- The main agent reconciles the advisory result against current files and deterministic evidence.

## Context Economy

Use a bounded pass when one bad prompt or missed contradiction is likely to cost more than the Multi-GPT run.

Prefer skipping when context is small, selected files are few, the question is already precise, or exact code/schema/diff evidence dominates.

Compression may cover logs, CI output, search results, long documents, issue threads, and duplicate summaries. Keep patch-target source, diffs, schemas, validators, exact errors, security evidence, and migration contracts raw.

Record the job id, `requested_contract`, `enforced_launch_contract`, iteration budget, status, evidence paths, and advisory boundary when the result materially changes the task. The enforced launch contract is `gpt-5.6-luna` with `max` reasoning. Multi-GPT keeps the normal Codex user configuration enabled so an installed OpenCodex base URL and model catalog reach the provider boundary, while explicit stage arguments continue to pin the model, effort, read-only sandbox, approval policy, and HTTP transport. Verify a representative run by matching its exact Codex session id to the hashed OpenCodex `conversationId`; nearby rows from another Codex task are not Multi-GPT evidence.

## Completion

On `completed`, use `result.final_answer` as advisory input or as the user-facing answer when the user explicitly requested Multi GPT. Do not present internal stage transcripts.

On `failed`, report the concrete runtime boundary and preserve stronger local or browser evidence. On `canceled`, stop polling and report cancellation briefly.
