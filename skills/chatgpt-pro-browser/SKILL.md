---
name: chatgpt-pro-browser
description: Use for ChatGPT Pro planning, research, and attachment-only work through Oracle; agbrowse and CodexPro are legacy recovery-only.
---

# ChatGPT Pro through Oracle

Oracle is the only backend for a new Pro run. It owns model selection, exact
file attachment, submission, durable output, exact-slug recovery, and one-shot
archive. There is no new agbrowse, CodexPro, DevSpace, in-app Browser, custom
CDP/Playwright, or `@chrome` fallback.

## Non-negotiable Pro contract

- `task_kind: pro`.
- Select the account-visible Pro model through Oracle; never downgrade to a
  regular GPT model.
- Never select, connect, inspect, register, repair, mention, or delete a
  ChatGPT app.
- Local context is attachment-only through Oracle `--file` arguments.
- Every attachment is an exact regular non-symlink file with a frozen SHA-256.
- Search or research is enabled only when explicitly requested and supported by
  the selected Pro route.

## Preflight

1. Do not run the resource guard as a routine or pressure gate.
2. Resolve and hash-validate the tested Oracle compatibility contract.
3. Validate the short UTF-8 mission and at least one exact attachment.
4. Claim the same normalized-project mutex used by regular Oracle work.
5. Use a fresh Oracle slug; do not reuse an unrelated tab or conversation.
6. Require Oracle model-selection and attachment evidence before accepting a
   successful send.

## Manifest and preview

Required fields:

- `project_root`.
- `task_kind: pro`.
- `mission_path`: the short Pro instruction file.
- `attachments`: one or more exact attachment paths.
- `model_strategy: select`.

Any app name, DevSpace mention, CodexPro field, or implicit model downgrade is a
hard error.

Preview without launching a browser:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_dispatch.py" --mode pro --project-root C:\project --mission-path C:\project\pro.md --attachment C:\project\packet.zip --manifest-output C:\project\.ai-bridge\pro.json --dry-run
```

The preview must show the exact Oracle command, attachment paths/hashes, model
selection, short prompt, output path, and slug without submitting.

## Execute and complete

Execute the compiled manifest only after a live Pro run was authorized:

```powershell
python "$env:USERPROFILE\.codex\skills\chatgpt-oracle-runtime\scripts\run_chatgpt_oracle.py" run --manifest C:\project\.ai-bridge\pro.json
```

Completion requires exact Oracle Pro model evidence, attachment evidence, exit
zero, a fresh nonempty host-only `output.md`, immutable hashes, and a refreshed
transcript. Oracle archives only after the durable one-shot output is saved.

## Recovery

Diagnose only the exact stored Oracle run directory and slug:

```powershell
python "$env:USERPROFILE\.codex\skills\chatgpt-oracle-runtime\scripts\run_chatgpt_oracle.py" recover --run-dir C:\exact\oracle-run --action harvest
```

Use `live` only to continue following that same stored session. Recovery never
restarts, resubmits, changes the model, changes attachments, or creates a
replacement. A zero exit without nonempty output remains `attention_required`.

For an already persisted agbrowse Pro run only, the former exact
`chatgpt_agbrowse_run.py --observe-run|--recover-run <run-dir>` commands remain
available. They must never create a new run.
