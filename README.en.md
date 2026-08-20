# Codex Web GPT Orchestrator

English | [한국어](README.md)

A Windows automation toolkit that delegates planning, research, review, code
changes, and testing to web ChatGPT while keeping local Codex work focused on
transport, recovery, identity, hashes, and the final deterministic gate.

The current release is `1.10.0`.

It connects two upstream tools:

- [Oracle](https://github.com/steipete/oracle) creates signed-in ChatGPT browser
  sessions, selects the model, waits for the response, and harvests the result.
- [DevSpace](https://github.com/Waishnav/devspace) lets ChatGPT read, edit, and
  run commands only inside project roots approved by the user.

Regular GPT runs verify `GPT-5.6 Sol` at the visible `Extra High` tier (`Power
4 of 5`), then send one line containing exactly `@DevSpace` and the absolute
UTF-8 mission-file path. Regular work uses this `extra-high` tier by default
and never promotes to Pro automatically. Pro has a limited daily allowance, so
it is selected only when the user explicitly requests it. New qualified Pro
runs use the `pro-devspace` transport: they mention `@DevSpace` and may write
mission-directed files and run the mission's commands inside the exact project
root. Repository safety rules stay authoritative; account, ChatGPT app
settings, and external state change only when the mission explicitly
authorizes them. `pro-attachment-only` remains a separate explicit
immutable-evidence route and is not an automatic fallback. The standard
comprehensive workflow allows a plan-to-Pro transition only when its manifest
sets `allow_pro: true`, which the host writes only after an explicit user
request. Pro skills never auto-invoke (`allow_implicit_invocation: false`).

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
       |-- qualified Pro: @DevSpace + mission path (writes inside the exact root)
       `-- evidence Pro: mission + hash-frozen attachments
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
| Pro | `pro` / Pro | Explicitly requested Pro judgment or design review; result only | Qualified `pro-devspace`: DevSpace mention + writes inside exact root; evidence `pro-attachment-only`: attachments only, no app; both `gpt-5.6-sol` + `Power 5 of 5` |

Orchestrator mode is a single web submission. Comprehensive mode contains an
orchestrator-equivalent implementation stage plus planning, independent review,
optional Pro or Web Multi-GPT, and final gates. Web Multi runs only when it is
explicitly selected; regular work and failures never transition to it automatically.

Standalone Pro is a one-shot route, separate from comprehensive mode. The
evidence route (`pro-attachment-only`) reviews the attached plan, code, or
document and returns the durable result; qualified Pro (`pro-devspace`) can
write files and run commands inside the exact project root within the
mission's scope. Neither Pro route transitions automatically into
implementation or another stage; use comprehensive mode only when the work
must continue from planning through implementation and gates.

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

The validated combination is Oracle `0.18.0` and DevSpace `1.0.7`. The installer
applies Windows compatibility patches only when exact upstream file hashes
match. Oracle 0.18.0's upstream disabled-tier detection and manual-login
reattach cookie-sync opt-in remain intact. The local hash-gated patches retain
strict visible `Power 4 of 5` and `Power 5 of 5` proof, a copied per-run
profile, and one overall answer-timeout budget. This release has not performed
live browser validation. Oracle `0.16.1`, `0.17.0`, `0.17.1`, `0.17.2`, and
`0.17.3` remain available only for exact recovery of runs already persisted
with those versions.

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

The DevSpace 1.0.7 compatibility layer replays an already-consumed rotated
refresh token only for the same client, scope, and resource, for 30 seconds and
at most 32 in-memory entries. Revocation, expiry, and mismatch remain
fail-closed; credentials and the OAuth database schema are unchanged. The one
existing HKCU Run value, `DevSpace MCP Server`, starts a hidden single-instance
watchdog. Every health cycle rereads `~/.devspace/config.json` and repairs only
the exact DevSpace service identity and Funnel. It never changes the Owner
credential, OAuth clients/tokens, ChatGPT registration/settings, or allowed
roots.

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

Pro runs only on an explicit user request. Qualified Pro (`pro-devspace`)
sends no attachments: the dispatch emits the `@DevSpace` mention plus the
absolute mission path, and the web Pro session may write mission-directed
files and run the mission's commands inside the exact project root.

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_dispatch.py" `
  --mode pro `
  --project-root C:\project `
  --mission-path C:\project\pro.md `
  --manifest-output C:\project\.ai-bridge\pro.json `
  --dry-run
```

The evidence route (`pro-attachment-only`) uses no project app. Build and
validate the 1 MiB context packet described in
`skills/chatgpt-pro-browser/SKILL.md`; dispatch then revalidates its manifest,
receipt, mission, packet, and evidence hashes before submission.
`--attachment` and `--context-manifest` are accepted only on this evidence
route.

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
- Exact recovery writers serialize on a per-run mutex and never wait for the
  project submission mutex. The unresolved run still blocks every fresh
  submission, and a late original observer cannot overwrite already harvested
  terminal durable state.
- Completion requires Oracle exit code zero and a fresh, nonempty durable output.

Recover one exact run with:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_run.py" recover `
  --run-dir C:\exact\oracle-run `
  --action harvest
```

Inspect local runtime readiness without opening ChatGPT or creating run state:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_run.py" preflight `
  --manifest C:\project\.ai-bridge\oracle.json `
  --expected-manifest-sha256 <oracle_manifest_sha256>
```

For DevSpace runs, preflight checks the exact Oracle and DevSpace package hashes,
the signed-in profile seed, project ownership, the running DevSpace process,
Tailscale Funnel, and exact local/public `/healthz` identity. It never opens a
browser, submits a prompt, applies compatibility patches, or validates ChatGPT
login/app UI. The Tailscale self hostname is detected automatically; use
`--devspace-hostname` only when an explicit override is required.

Preflight output is read-only advisory evidence; it does not persist submission
authority. A real DevSpace `run` rechecks the Tailscale hostname, Funnel mapping,
and exact local/public `/healthz` identity inside the existing project submit
mutex. Failure leaves structured `SUBMISSION_NOT_READY` evidence in the exact run
state and settles safely before starting the Oracle browser.

Classify or watch one exact persisted run without mutation:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_diagnose.py" triage --run-dir C:\exact\oracle-run
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_diagnose.py" watch --run-dir C:\exact\oracle-run
```

Oracle continues to own normal desktop completion notifications. `watch` adds
host-state visibility for `attention_required`, emits NDJSON only on meaningful
state changes, and never recovers or resubmits.

Diagnosis classifies persisted `blocked` and `not_executed` outcomes before a
complete lifecycle and reports same-artifact `OAuth token request failed` plus
`503` evidence under a distinct registered-app OAuth signature. Malformed or
ambiguous v1 markers and persisted `unknown` remain unresolved.

## Update, rollback, and uninstall

```powershell
.\install.ps1 -WhatIf
.\install.ps1
.\rollback.ps1
.\uninstall.ps1
```

## Documentation

- [Global ChatGPT routing and mode selection](docs/GLOBAL_CHATGPT_ROUTING.md)
- [DevSpace and Tailscale setup](docs/DEVSPACE_TAILSCALE_SETUP.md)
- [Technical changelog](docs/CHANGELOG.md)
- [Differences from upstream](docs/VS_UPSTREAM.md)
- [Release checklist](docs/RELEASE_CHECKLIST.md)
- [Security policy](SECURITY.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

## License

MIT License. Third-party copyrights and licenses for Oracle, DevSpace, and other
components are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
