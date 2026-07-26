---
name: chatgpt-pro-plan-handoff
description: Run staged work with unchanged attachment-only Pro and Oracle-based regular comprehensive stages; optional Web Multi uses independent Oracle sessions.
---

# Pro and comprehensive handoff

Pro is attachment-only through `chatgpt-pro-browser` and Oracle. It never uses
DevSpace or CodexPro. CodexPro and all agbrowse creation are frozen; legacy
files remain only for exact persisted-run recovery.

New GPT comprehensive work uses
`bin/chatgpt_oracle_comprehensive.py` with schema
`codex.chatgpt.oracle-comprehensive/v1`:

```text
plan -> optional Pro or Oracle Web Multi -> review
     -> implementation -> final web gate -> one local deterministic gate
```

The manifest supplies absolute `project_root`, `workflow_dir`,
`initial_mission_path`, stable `workflow_id`, and a nonempty
`local_gate_command`. Every regular web stage writes its own next mission and a
bound `codex.chatgpt.oracle-stage-result/v1` receipt. The host validates
workflow/stage/attempt/input hashes, UTF-8 paths, output hashes, PASS status,
and the transition; it never rewrites the semantic prompt.

An optional Pro stage runs through Oracle attachment-only. Because Pro has no
DevSpace access, it returns one strict identity-bound JSON envelope containing
its output and next-mission text. The host mechanically preserves those strings
as UTF-8 files and computes the standard receipt; it does not summarize or
rewrite them.

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_comprehensive.py" --manifest C:\project\workflow.json --dry-run
```

The review GPT owns plan repair and finalization. It does not merely list
findings: it directly repairs every defect resolvable from the mission,
DevSpace workspace, project rules, or available evidence, writes the corrected
final plan, and authors the complete implementation mission. `PASS` and
`PASS_WITH_NOTES` proceed directly to implementation; notes are carried inside
that mission. New work must not emit `REVISE`. A legacy `REVISE` receipt is
accepted only for compatibility and ends in attention-required without creating
another plan. `FAIL` is reserved for a concrete unavailable external input or
authority, unresolved safety boundary, or genuine execution impossibility.

Every regular stage binds an exact project root and exact input mission path.
DevSpace may reuse or open only that normalized root, with at most one retry of
the same root after inspecting registered workspaces. Parent, child, similarly
named, active-workspace, and shell-boundary fallbacks are forbidden. The stage
reads its mission and applicable `AGENTS.md` chain completely before project
exploration or edits.

Transport or runner recovery keeps the same workflow and stage identity. It
must never create a `workflow-retryN` replacement. The revision budget and
remaining critical finding set are persisted in the workflow state for
operator visibility. Only final web PASS plus a zero-exit local gate can
complete. A Pro selection launches an explicit Oracle attachment-only stage
and waits for a bound receipt; it is never downgraded. Missing receipt/output,
crash, or ambiguity returns attention-required without a replacement submit.

Existing v1-v4 agbrowse comprehensive state and v3 parallel implementation are
legacy recovery-only. Their files remain installed for exact recovery but are
not the new-work route.
