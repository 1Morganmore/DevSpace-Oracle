---
name: chatgpt-oracle-runtime
description: Run explicitly requested Oracle browser missions from a strict JSON manifest and an absolute UTF-8 mission file. Use only for the additive Oracle path beside existing agbrowse routing. Never use --file for general GPT browser runs, never automate app autocomplete or settings UI, and never restart or resubmit failed sessions automatically.
---

# ChatGPT Oracle Runtime

Use only when the user explicitly requests Oracle.

## Manifest

Require schema `codex.chatgpt.oracle-run/v1` with:

- `project_root`: absolute existing directory.
- `mission_path`: absolute UTF-8 regular file inside the project.
- `app_name`: one-line app name, without a leading `@`.
- `mode`: `browser`.
- Optional `run_root`, `oracle_command`, `oracle_args`, and mutex timeout.

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

## Recovery

Recovery always reuses the stored Oracle slug and never restarts or submits:

```powershell
python skills/chatgpt-oracle-runtime/scripts/run_chatgpt_oracle.py recover --run-dir C:\absolute\run --action harvest
```

Use `--action live` only to keep following the same stored session. A successful recovery must write a nonempty stored `output.md`, update `state.json` to `complete`, and refresh `transcript.md`; exit code zero without output is `attention_required`.

Same-project runs hold one cross-process mutex for the entire Oracle process lifetime. Different project roots use different mutexes and may run concurrently.
