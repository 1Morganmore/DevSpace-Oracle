---
name: chatgpt-pro-plan-handoff
description: Run staged comprehensive GPT work through Oracle, with optional attachment-only Pro and independent Oracle Web Multi advisory stages, then one deterministic local gate.
---

# Oracle comprehensive workflow

Use `codex.chatgpt.oracle-comprehensive/v1` for new comprehensive work. The
active entry point is `%USERPROFILE%\.codex\bin\chatgpt_oracle_comprehensive.py`.
Every regular web stage uses Oracle plus the manually registered DevSpace app;
an optional Pro stage is Oracle attachment-only and selects no app.
The stage order is plan -> optional Pro or Oracle Web Multi -> review ->
implementation -> final web gate -> one deterministic local gate.

## Ownership

- The completing web stage authors the next semantic mission.
- Local Codex validates UTF-8, hashes, immutable workflow/stage bindings,
  transport, recovery, and the final deterministic gate. It never rewrites a
  web-authored next mission.
- Review repairs every locally resolvable plan defect inline, writes the final
  plan and implementation mission, then returns `PASS` or `PASS_WITH_NOTES`.
- A selected Web Multi advisory uses distinct Oracle sessions in waves of at
  most five and never starts automatically or as a fallback.
- Pro returns one identity-bound JSON envelope. The host materializes its output
  and next-mission strings byte-for-byte.

## Preview

Create an absolute UTF-8 initial mission inside the exact project root, then
compile without submitting:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_comprehensive.py" start --project-root C:\project --mission-path C:\project\mission.md --dry-run
```

Bind every regular stage to that exact root and mission path. DevSpace may retry
the same registered root once after listing workspaces; it must not substitute a
parent, child, similar name, active workspace, or shell workaround.

## Execution and recovery

Execute only after the user authorizes a live submission and supplies the exact
preview hash required by the runner. Transport recovery retains the same
workflow and stage identity; it never creates a replacement workflow or resets
the semantic revision budget.

A nonzero Oracle exit after submission is `attention_required`, not terminal
failure. Recover only the stored exact slug. Exact session authority is
monotonic: `terminal_observed` cannot regress to `live`; observer disagreement
retains the project lock until a later exact terminal harvest produces fresh,
nonempty durable output.

Completion requires the final web verdict, exact durable outputs and receipts,
and one zero-exit deterministic local gate. No dry-run, fixture, or lower-level
API substitutes for an approved representative live flow.
