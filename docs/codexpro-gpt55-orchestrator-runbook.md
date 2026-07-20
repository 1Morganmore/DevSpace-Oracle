# CodexPro GPT-5.6 Drive-Slot Orchestrator Runbook

This is the global operating runbook for non-Pro GPT-5.6 runs. Each Windows drive has at most one drive-root CodexPro Developer App; projects on that drive share it while each prompt narrows `working_scope`, `allowed_paths`, `forbidden_paths`, and write authority. Distribution installs use dynamic Cloudflare URLs by default. A registered stable CDrive ngrok hostname may be reused locally. The filename retains `gpt55` only to preserve existing references.

## Normal Path

0. Require ChatGPT Developer Mode before any non-Pro app transaction.
   - Normal user path: `Settings > Apps > Advanced settings > Developer mode`.
   - Workspace admin/owner path: `Workspace settings > Apps > Create`.
   - If the toggle or exact `Create app/앱 만들기` surface is absent, return `CHATGPT_DEVELOPER_MODE_REQUIRED` with the setup guidance and stop before app mutation or GPT submission. Do not retry the UI blindly.

1. Resolve the task scope, not the app root.
   - The CodexPro app root defaults to the drive containing the task scope.
   - The GPT prompt must carry `working_scope`, `allowed_paths`, `forbidden_paths`, app-root write caution, and write authority.
   - Do not create project-root apps. Resolve every workspace to its drive root; C-drive work reuses the fixed CDrive app and each other drive owns one Cloudflare app.

2. Ensure the matching drive app.
   - Run `<CODEX_HOME>\bin\codexpro_project_cloudflare_bootstrap.ps1 -Root <DRIVE:\> -TunnelProvider auto`, then reconcile the emitted candidate through `<CODEX_HOME>\bin\codexpro_agbrowse_app.py`.
   - The thin connector owns only the exact app transaction and calls the selected contract-validated, unmodified agbrowse public commands. `0.1.18` is the tested default; another exact version requires explicit integrity and command-contract validation. No custom CDP, Playwright, in-app Browser, Proxima, or `@Chrome` fallback is permitted.
   - A confirmed result requires live MCP identity for the selected drive root, the current full Server URL including token, app visibility, and `full_access` evidence.

3. Keep the normal path light.
   - First probe the saved full MCP URL for live identity.
   - If the saved URL is alive and reports the selected drive root, do not open Plugins management.
   - Open `https://chatgpt.com/#settings/Plugins` only for URL mismatch, app missing, stale permission evidence, corrupted app state, or GPT tool-access failure.

4. Submit GPT-5.6.
   - Use GPT-5.6 with the requested reasoning level. For new regular-GPT, comprehensive, and Web Multi-GPT v2 work, an unspecified level means exact `High`; never attempt `Very High` and silently downgrade. Historical v1 evidence may retain its recorded variant for recovery only.
   - Invoke the confirmed matching-drive app at the start of the composer, then press Tab and verify that ChatGPT resolved it into an app chip before prompt submission. The version may increase only after an authorized URL replacement or force recreation for that drive.
   - This app mention belongs only inside the ChatGPT web composer. Do not use Codex Desktop connector/app cards such as `CodexPro-... open workspace` or `CodexPro-... server config` to run or verify this lane. `codex_apps resources/read failed` means the wrong Codex internal app surface was touched; it is not a valid ChatGPT web CodexPro transport test.
   - Do not present CodexPro connector card labels as executable steps in Codex Desktop. The only normal executable path is the agbrowse bridge and its exact project app transaction; connector-card labels are diagnostic wrong-surface text only.
   - For `지휘` / `오케스트레이터` / maximum-token-saving work, set `operation_mode: orchestrator` and state that GPT is the implementation owner. Codex must not create files, edit source, author skills, or do the delegated implementation locally until this GPT lane has produced direct CodexPro edits or patch-shaped implementation output.
   - For write/edit/orchestrator work, prefer task-file invocation: place the full contract under `working_scope`, then submit only a short Korean instruction such as `이 파일 읽고 작업해: <path>` after the resolved app chip. This keeps the composer light while preserving a local evidence anchor.
   - Always use a fresh ChatGPT conversation for CodexPro app-backed question submission. Do not continue prior app conversations with `chat_url`, `session_policy: reuse`, or session affinity; carry necessary continuity as a compact prompt state plus live project app scope.

5. Wait for the live lane.
   - `RESPONSE_IN_PROGRESS`, visible stop/generating state, and `partial-running` DOM recovery are not completion.
   - After restart or interruption, reattach the recorded conversation URL only to recover/reconcile that already-dispatched lane before retrying. Do not use that URL to submit a new follow-up question into the old app conversation.

## Stop-Before-Submit Conditions

- Matching-drive app preflight fails.
- App exists but full Server URL or all-actions permission cannot be verified after required repair.
- The same normalized project root already has an active or unresolved post-send run. Different project roots may continue concurrently through distinct agbrowse parallel sessions and targets.
- Resource pressure prevents a required helper from starting safely. Delay that helper and preserve all active user work; pressure never authorizes a replacement send, browser-engine switch, reduced Web Multi topology, or invented completion.
- Mode family or reasoning level cannot be explicitly verified or downgraded under the skill rules.
- A `지휘` / `오케스트레이터` task is about to proceed through local Codex implementation before a GPT-5.6 CodexPro orchestrator lane has been dispatched and reconciled.

## Codex-Side Ownership Latch

- In orchestrator mode, Codex is the manager, verifier, and local-host executor. GPT is the first implementation owner.
- Before the GPT result, Codex may only prepare the prompt/manifest, run preflight, collect compact evidence needed to brief GPT, recover/wait on the live lane, and maintain runtime health.
- After the GPT result, Codex may verify, run deterministic commands/tests, apply GPT-provided patches when direct CodexPro writes were unavailable, integrate, commit, report, and clean up.
- Advisory-only GPT output is incomplete. Correct it in the same GPT lane by asking for direct CodexPro edits or patch-shaped edits, changed-file list, commands/tests, blockers, and the exact Codex-local verification step.

## Repair Rules

- Saved URL alive and identity matches the selected drive: reuse without UI.
- Authoritatively verified full-URL mismatch: delete the old Developer App, increment version, create and connect the replacement. Loading delay, 401 diagnostic fetch, or connector-ID uncertainty must preserve the app and fail closed.
- A create-time 409 with a validated existing connector ID: use the exact existing connector route through public agbrowse commands and install/connect or confirm the existing `플러그인 작업` / `연결 해제` state without version increment.
- A successful connect with a stale installed list: use bounded post-submit connector evidence and verify the exact detail route instead of creating a duplicate.
- Permission-only failure after create/connect: preserve the app and repair the existing app detail page only.
- Stale permission state: navigate to the exact connector route with public agbrowse commands, open the same app detail and permission sub-view, then verify native radio value `full_access` again.
- Corrupted or repeatedly failing app: use `-ForceRecreate`; this must not be suppressed by recent registry state. Commit the replacement before retiring the old app.
- Exact run-owned utility tabs are closed after their transaction and absence is re-verified. Pre-existing, foreign, ambiguous, active, or uncertain user tabs must not be closed. Every exact run-owned `COMPLETE` work tab is automatically closed after immutable result capture and ownership verification.

## Explicit Overrides

- Project-root app overrides are unsupported; app identity remains drive-scoped.
- `mcp_resource_guard.py --cleanup-orphans --json`: use only when parent-missing helper accumulation is concretely evidenced. It is never a routine preflight or execution gate.

Overrides should be manager-owned and recorded in the run artifact.
