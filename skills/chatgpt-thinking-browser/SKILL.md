---
name: chatgpt-thinking-browser
description: Run new regular ChatGPT direct, plan, review, edit, and orchestrator work through Oracle plus the manually registered DevSpace workspace app; use legacy agbrowse only to recover an exact persisted old run.
---

# Regular ChatGPT through Oracle + DevSpace

Read `chatgpt-question-designer` first when shaping a new mission.

For new work, create one absolute UTF-8 mission file inside the project and
resolve the requested mode through:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_dispatch.py" --mode <direct|plan|review|edit|orchestrator> --project-root C:\project --mission-path C:\project\mission.md --manifest-output C:\project\.ai-bridge\oracle.json --reasoning-level "Very High" --dry-run
```

Remove `--dry-run` only for an explicitly authorized live web run. The runtime
sends plain `@DevSpace` plus the absolute mission path. It never attaches files,
opens ChatGPT settings, inspects/selects/deletes an app, or falls back to
agbrowse, Playwright, in-app Browser, or Chrome.

CodexPro is frozen for new work. Never mention it in a new mission, probe its
endpoint, repair/register/delete its app, or use it as a DevSpace fallback.

Oracle explicitly selects `GPT-5.6 Sol` and `heavy`, verifies the visible
`Extra High` tier, and records both in Oracle evidence. The exact 0.16.1
compatibility layer is hash-gated and fails closed on an unknown version or
third-party file. Never invent xhigh or silently downgrade.

Control state and final Oracle output are host-only below
`%USERPROFILE%\.codex\state\chatgpt-oracle`. Complete requires exit zero and
fresh nonempty host output. Recovery uses the stored slug:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_run.py" recover --run-dir C:\exact\host-run --action harvest
```

Recovery never restarts/resubmits, never downgrades durable COMPLETE, and uses
Oracle `--no-recover`.

For an already persisted agbrowse run only, use its exact legacy
`chatgpt_agbrowse_run.py --observe-run|--recover-run <run-dir>` command. Do not
create a new agbrowse run.
