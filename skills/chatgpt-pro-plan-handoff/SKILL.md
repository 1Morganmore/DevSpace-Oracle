---
name: chatgpt-pro-plan-handoff
description: Run staged work with unchanged attachment-only Pro and Oracle-based regular comprehensive stages; optional Web Multi uses independent Oracle sessions.
---

# Pro and comprehensive handoff

Pro is unchanged and attachment-only through Oracle. It never uses DevSpace.

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

When a plan routes to Pro and Pro needs an evidence packet, the plan-authored
next mission declares it in exactly one closed `[PRO_ATTACHMENT_CONTRACT]`
block. The JSON body uses schema `codex.chatgpt.oracle-pro-attachments/v1` and
an `attachments` array of absolute project-root-contained regular non-symlink
paths with optional SHA-256 values. The host attaches only the mission and these
declared files; it never discovers ZIPs from prose. A legacy Pro mission without
the block remains mission-only. Regular DevSpace stages reject this block and
never receive packet attachments.

Plan receipts should use `PLAN_READY`. For compatibility, `completed` is
accepted only when the plan receipt is otherwise a fully ready, blocker-free,
hash-valid transition to `review`, `web-multi`, or `pro`; ambiguous or incomplete
receipts remain fail-closed and are never rewritten on disk.

Pro must JSON-escape every quote and backslash inside `output_text` and
`next_mission_text`. The host always parses strict JSON first. If strict parsing
fails, it may make one narrow recovery attempt only for the canonical ordered
envelope whose text fields contain unescaped quotes. Recovery still requires the
exact workflow, stage, attempt, and input-mission identities plus a complete
unambiguous tail. Invalid escapes, truncation, duplicate/ambiguous boundaries,
or identity drift remain fail-closed. A recovered receipt records the immutable
source output SHA-256, recovery method, and strict parser error position.

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_comprehensive.py" --manifest C:\project\workflow.json --dry-run
```

Only PASS can reach implementation. Only final web PASS plus a zero-exit local
gate can complete. A Pro selection returns an explicit attachment-only handoff
and waits for a bound receipt; it is never downgraded. Missing receipt/output,
crash, or ambiguity returns attention-required without a replacement submit.

Existing v1-v4 agbrowse comprehensive state and v3 parallel implementation are
legacy recovery-only. Their files remain installed for exact recovery but are
not the new-work route.
