# Codex Web GPT Orchestrator

English | [한국어](README.md)

A Windows automation toolkit that delegates planning, research, review, code
changes, and testing to web ChatGPT while keeping local Codex work focused on
transport, recovery, identity, hashes, and the final deterministic gate.

The current release is `1.8.0`.

It connects two upstream tools:

- [Oracle](https://github.com/steipete/oracle) creates signed-in ChatGPT browser
  sessions, selects the model, waits for the response, and harvests the result.
- [DevSpace](https://github.com/Waishnav/devspace) lets ChatGPT read, edit, and
  run commands only inside project roots approved by the user.

Regular GPT runs verify `GPT-5.6 Sol` at the visible `Extra High` tier, then
send one line containing `@DevSpace` and the absolute UTF-8 mission-file path.
Pro runs use `gpt-5.5-pro` with exact, hash-frozen attachments and no DevSpace.

## What it provides

- Web GPT can inspect, change, and test a local project.
- Direct, plan, review, edit, orchestrator, deep-research, and Pro modes.
- Genuine Web Multi-GPT with independent ChatGPT sessions.
- Read-only Local Multi-GPT with parallel Codex lanes on the PC.
- Comprehensive workflows from planning through implementation and final gate.
- Per-project exclusion, immutable mission and attachment hashes, and exact
  session recovery.
- Isolated browser profiles so different projects can run concurrently.
- Automatic archive lifecycle for conversations owned by Oracle.
- Install receipts, backups, rollback, and uninstall support.

## How it works

```text
User request
    -> Codex writes a UTF-8 mission and manifest
    -> Oracle starts a signed-in ChatGPT session
       |-- regular GPT: @DevSpace + mission path
       `-- Pro: mission + hash-frozen attachments
    -> web GPT explores, plans, edits, and tests
    -> Oracle saves the answer as a local artifact
    -> Codex checks identity, hashes, and one deterministic final gate
```

Host state and ChatGPT output are stored outside DevSpace projects under
`%USERPROFILE%\.codex\state\chatgpt-oracle`.

## Modes and English invocation names

| Mode | CLI / natural-language name | Purpose | Transport |
|---|---|---|---|
| Regular GPT | `direct` / GPT | Questions, analysis, and small tasks | Oracle + DevSpace |
| Plan | `plan` / plan | Design before implementation | Oracle + DevSpace, read-only |
| Review | `review` / review | Independent code or plan review | Oracle + DevSpace, read-only |
| Edit | `edit` / edit | Scoped changes and tests | Oracle + DevSpace |
| Orchestrator | `orchestrator` / orchestrator | One GPT completes an already-scoped task | Oracle + DevSpace |
| Deep Research | `deep-research` / deep research | Public research plus project evidence | Oracle Deep Research + DevSpace |
| Web Multi-GPT | Web Multi-GPT | Independent parallel perspectives and merger | 2-25 Oracle sessions |
| Local Multi-GPT | Local Multi-GPT | Local advisory synthesis and counterexample search | Fixed `gpt-5.6-luna` + `max`, read-only |
| Comprehensive | comprehensive mode | Plan, explicitly selected Pro/Web Multi, review, implementation, gate | Staged Oracle workflow |
| Pro | `pro` / Pro | Independent final judgment or design review; result only | Oracle attachments only; `gpt-5.5-pro` |

Orchestrator mode is a single web submission. Comprehensive mode contains an
orchestrator-equivalent implementation stage plus planning, independent review,
optional Pro or Web Multi-GPT, and final gates. Web Multi runs only when it is
explicitly selected; regular work and failures never transition to it automatically.

Standalone Pro is a one-shot review route, separate from comprehensive mode. It
reviews the attached plan, code, or document, returns the durable result, and
stops; it never transitions automatically into implementation or another stage.
Use comprehensive mode only when the work must continue from planning through
implementation and gates.

Local Multi-GPT and Web Multi-GPT are separate paths. Local Multi-GPT is an
optional advisory tool that runs Codex child lanes on the PC. Every stage is
fixed to `gpt-5.6-luna` with `max` reasoning; any other model or effort is
rejected before a child process starts. Web Multi-GPT instead runs independent
ChatGPT web sessions through Oracle and merges their results.

File-backed Local Multi-GPT jobs fail closed unless the MCP host defines
`MULTI_GPT_ALLOWED_ROOTS_JSON` as a JSON array of narrow absolute directories.
Filesystem roots and the whole home directory are rejected. The server resolves
canonical paths, rejects symlink/junction and sensitive inputs, blocks
high-confidence secret material, verifies strict UTF-8, and records redacted
relative-path plus SHA-256 provenance before starting a child. Background job
state is revisioned and atomically persisted; after a restart, a provably dead
owner becomes `failed` with `ORPHANED_AFTER_RESTART`, while live or ambiguous
external ownership is preserved and cannot be canceled by the new process.
The server also limits actual Codex child processes across all jobs with a FIFO
backpressure queue: four by default, configurable from 1 through 20 with
`MULTI_GPT_MAX_CHILDREN`.

## Requirements

- Windows 11
- Python
- Node.js 24 or later and earlier than 27
- Git for Windows / Git Bash
- Tailscale
- An Oracle browser profile signed in to ChatGPT
- One manually registered DevSpace app in ChatGPT Developer Mode

The validated combination is Oracle `0.17.0` and DevSpace `1.0.5`. The installer
applies Windows compatibility patches only when exact upstream file hashes
match. Oracle `0.16.1` is available only for exact recovery of runs already
persisted with that version.

## Install

```powershell
git clone https://github.com/1Morganmore/DevSpace-Oracle.git
cd DevSpace-Oracle
.\install.ps1 -WhatIf
.\install.ps1
```

The installer backs up replaced files and writes durable install receipts under
`%USERPROFILE%\.codex\receipts`.

## One-time DevSpace setup

You do not install one ChatGPT app per project. Register one DevSpace app and
add each permitted project as another `--root` argument.

```powershell
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py setup `
  --root C:\projects\alpha `
  --root C:\projects\beta `
  --hostname your-device.your-tailnet.ts.net `
  --public-port 8443 `
  --dry-run
```

Review the output, then replace `--dry-run` with `--apply`. In ChatGPT Developer
Mode, manually register one app:

- Name: `DevSpace`
- URL: `https://your-device.your-tailnet.ts.net:8443/mcp`

After owner approval, the automation does not inspect or manipulate ChatGPT
settings, app lists, permissions, deletion, or picker UI per task. Adding a new
project only changes the DevSpace allowed roots.

See [DevSpace and Tailscale setup](docs/DEVSPACE_TAILSCALE_SETUP.md) for the
complete procedure.

## Regular GPT example

Create a UTF-8 mission file inside the project, then dry-run the manifest:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_dispatch.py" `
  --mode orchestrator `
  --project-root C:\project `
  --mission-path C:\project\mission.md `
  --manifest-output C:\project\.ai-bridge\oracle.json `
  --reasoning-level "Very High" `
  --dry-run
```

Only when the run is authorized, remove `--dry-run` and pass the preview's
top-level `oracle_manifest_sha256` in the same command as
`--expected-manifest-sha256 <oracle_manifest_sha256>`.

## Pro example

Pro uses no project app. Build and validate the 1 MiB context packet described
in `skills/chatgpt-pro-browser/SKILL.md`; dispatch then revalidates its manifest,
receipt, mission, packet, and evidence hashes before submission.

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_dispatch.py" `
  --mode pro `
  --project-root C:\project `
  --mission-path C:\project\pro.md `
  --context-manifest C:\project\.ai-bridge\pro-context-manifest.json `
  --attachment C:\project\.ai-bridge\packet.zip `
  --manifest-output C:\project\.ai-bridge\pro.json `
  --dry-run
```

## Execution and recovery rules

- One active or uncertain Oracle workflow is allowed per normalized project.
- Different projects can run concurrently through isolated profiles.
- Web Multi-GPT runs child sessions in waves of at most five.
- Heavy non-Pro work receives about 90 minutes initially and another 90 minutes
  for exact recovery, for an effective ceiling of roughly 180 minutes.
- A browser or local-process exit is not proof that the web task failed.
- Recovery uses only the persisted Oracle slug and exact conversation URL. It
  never resubmits the task.
- Completion requires Oracle exit code zero and a fresh, nonempty durable output.

Recover one exact run with:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_run.py" recover `
  --run-dir C:\exact\oracle-run `
  --action harvest
```

## Update, rollback, and uninstall

```powershell
.\install.ps1 -WhatIf
.\install.ps1
.\rollback.ps1
.\uninstall.ps1
```

Use `-InstallLegacyRecoveryDependency` only on a machine that must recover an
already persisted legacy run.

## Documentation

- [Global ChatGPT routing and mode selection](docs/GLOBAL_CHATGPT_ROUTING.md)
- [DevSpace and Tailscale setup](docs/DEVSPACE_TAILSCALE_SETUP.md)
- [Technical changelog](docs/CHANGELOG.md)
- [Frozen legacy recovery assets](docs/FROZEN_LEGACY.md)
- [Release checklist](docs/RELEASE_CHECKLIST.md)
- [Security policy](SECURITY.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

## Legacy compatibility

The former CodexPro and agbrowse files remain only for exact recovery of already
persisted legacy runs. They are not a new-work route or fallback. See
[Frozen legacy assets](docs/FROZEN_LEGACY.md) for the inventory.

## License

MIT License. Third-party copyrights and licenses for Oracle, DevSpace, and other
components are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
