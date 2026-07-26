---
name: chatgpt-oracle-runtime
description: Run new non-Pro ChatGPT work through Oracle plus the manually registered DevSpace workspace app, including direct modes, recovery, comprehensive relay, and genuine multi-session Web Multi-GPT.
---

# ChatGPT Oracle Runtime

This is the only active browser path for all new GPT work. CodexPro and
agbrowse are frozen for exact legacy recovery only. Regular modes use DevSpace;
Pro uses Oracle attachment transport without any app.

Use `chatgpt_oracle_dispatch.py` for `direct`, `plan`, `review`, `edit`,
`orchestrator`, `deep-research`, `manual`, and `pro` routing. Regular routes
select `gpt-5.6` and send only `@DevSpace` plus the absolute project mission
path and a compact exact-workspace guard. The web GPT must use only the exact
project root recorded in that mission, read the mission and applicable
`AGENTS.md` completely first, and may retry that same root once after a timeout.
It must not substitute a parent, child, active workspace, or shell boundary
workaround. Pro selects the account-visible Pro model and sends one short instruction
plus exact attachment files; it never mentions DevSpace.
Regular routes select `GPT-5.6 Sol` with `heavy` and require Oracle
evidence for visible `Extra High`. Never invent xhigh or silently downgrade.

## Manifest

Require schema `codex.chatgpt.oracle-run/v1` with:

- `project_root`: absolute existing directory.
- `mission_path`: absolute UTF-8 regular file inside the project.
- `app_name`: one-line app name, without a leading `@`, for regular routes.
- `task_kind: pro` plus one or more exact `attachments` for Pro.
- `mode`: `browser`.
- Optional `run_root`, `oracle_command`, `oracle_args`, `thinking_time`,
  hash-validated `copy_profile`, and mutex timeout.

## Run

Preview first:

```powershell
python skills/chatgpt-oracle-runtime/scripts/run_chatgpt_oracle.py run --manifest C:\absolute\oracle-job.json --dry-run
```

The preview must include final argv, prompt first line, absolute mission path, SHA-256, and artifact paths without launching Oracle or a browser.
Use this wrapper preview only. Do not substitute Oracle's own browser `--dry-run`, because Oracle 0.16.1 may still enter browser preflight.

Execute only after an explicit live-run request:

```powershell
python skills/chatgpt-oracle-runtime/scripts/run_chatgpt_oracle.py run --manifest C:\absolute\oracle-job.json
```

Complete requires Oracle exit code zero and a nonempty `--write-output` artifact.
A nonzero Oracle exit after launch, including a browser response timeout, is
`attention_required` rather than proof that the web session failed. It retains
same-project ownership and permits only exact-slug `live` or `harvest`
recovery; it never authorizes a replacement submission.

## Recovery

Recovery always reuses the stored Oracle slug and never restarts or submits:

```powershell
python skills/chatgpt-oracle-runtime/scripts/run_chatgpt_oracle.py recover --run-dir C:\absolute\run --action harvest
```

Use `--action live` only to keep following the same stored session. A successful recovery must write a nonempty stored `output.md`, update `state.json` to `complete`, and refresh `transcript.md`; exit code zero without output is `attention_required`.

Direct same-project runs hold one cross-process mutex for the entire Oracle
process lifetime. A Multi parent owns that project mutex while authorized
children use a short parent-scoped launch mutex and isolated copied Chrome
profiles, then wait concurrently.
Control state, Oracle output, and transcripts live under
`%USERPROFILE%\.codex\state\chatgpt-oracle`, outside the DevSpace-writable
project.

Use `chatgpt_oracle_comprehensive.py` for the bounded plan → optional
Pro/Multi → review → implementation → final web gate flow. Each web stage
writes the next mission; the host validates only UTF-8, identity, paths, and
hashes. Use `chatgpt_oracle_multi.py` for independent solver sessions in waves
of at most five and one merger over handoff files.
