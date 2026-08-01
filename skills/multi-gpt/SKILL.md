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
  `total file context too large`; pass an excerpt instead of a whole large document.
- `model`: omit it or specify exactly `gpt-5.6-luna`; every other value is rejected before any child starts
- `reasoning_effort`: omit it or specify exactly `xhigh`; every other value is rejected before any child starts
- `max_iterations`: `1` for prompt shaping, `2` for broad critique, `3` for architecture comparison, and `5` only for explicit heavy use

The tool returns a `job_id`. Runs commonly take 5-20 minutes. Poll `multi_gpt_status` sparingly until `completed`, `failed`, or `canceled`.

Use `multi_gpt_cancel` only for a mistakenly started or explicitly stopped job owned by the current MCP server. After an MCP server restart, stale job files remain inspectable with `multi_gpt_status`, but their former child processes cannot be targeted reliably. Do not expose partial hidden stage reasoning as a user answer.

## Lane Selection

- Use Multi GPT for alternatives, comparison axes, counterexamples, prompt design, or post-GPT critique.
- Use `independent-review` for a concrete plan, code change, investigation conclusion, architecture decision, or proof packet.
- When both help, Multi GPT shapes the axes or packet and the reviewer inspects the concrete evidence. Do not ask both the same question.
- The main agent reconciles the advisory result against current files and deterministic evidence.

## Context Economy

Use a bounded pass when one bad prompt or missed contradiction is likely to cost more than the Multi-GPT run.

Prefer skipping when context is small, selected files are few, the question is already precise, or exact code/schema/diff evidence dominates.

Compression may cover logs, CI output, search results, long documents, issue threads, and duplicate summaries. Keep patch-target source, diffs, schemas, validators, exact errors, security evidence, and migration contracts raw.

Record the job id, `requested_contract`, `enforced_launch_contract`, iteration budget, status, evidence paths, and advisory boundary when the result materially changes the task. The enforced launch contract is `gpt-5.6-luna` with `xhigh` reasoning. It records the server argv and Codex client contract, not an independent provider readback. This deliberately avoids the host's `max` compatibility path, which was observed as `medium` in OpenCodex request logs despite `max` appearing in argv and Codex turn context.

## Completion

On `completed`, use `result.final_answer` as advisory input or as the user-facing answer when the user explicitly requested Multi GPT. Do not present internal stage transcripts.

On `failed`, report the concrete runtime boundary and preserve stronger local or browser evidence. On `canceled`, stop polling and report cancellation briefly.
