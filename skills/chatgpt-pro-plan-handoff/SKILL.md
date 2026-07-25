---
name: chatgpt-pro-plan-handoff
description: Run staged work with unchanged attachment-only Pro and Oracle-based regular comprehensive stages; optional Web Multi uses independent Oracle sessions.
---

# Pro and comprehensive handoff

Pro is unchanged and attachment-only through `chatgpt-pro-browser`. It never
uses DevSpace or Oracle.

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
