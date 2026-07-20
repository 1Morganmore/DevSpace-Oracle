---
name: chatgpt-thinking-browser
description: Use for regular ChatGPT GPT-5.6 web automation, including GPT, 지피티, GPT 모드, reasoning levels, search, app-backed work, and recovery. All execution uses an explicitly selected, contract-validated, unmodified agbrowse backend.
---

# ChatGPT GPT-5.6 via agbrowse

This legacy skill name is retained only for trigger compatibility. The sole execution backend is an unmodified agbrowse installation selected by an exact validated contract. `0.1.18` is the tested default, not a permanent pin or background update channel. Do not use the in-app Browser, browser-use, custom Playwright/CDP runners, Proxima, or the `@chrome` plugin as a fallback.

## Required preflight

1. Do not run the resource guard as a routine preflight. Use its orphan-cleanup mode only when verified parent-missing helper accumulation is actually suspected.
2. Resolve the workflow's explicit agbrowse contract, defaulting to `%USERPROFILE%\.codex\contracts\agbrowse-0.1.18.json`, then validate that exact file with `chatgpt_agbrowse_contract.py validate`.
3. Normalize the real project root. Output and run directories are not project identity.
4. Inspect the run store before dispatch. One active or uncertain run is allowed per project root.
5. A distinct project may run in parallel only through a fresh agbrowse session/tab created with `--parallel`.

An already live agbrowse session is not a precondition. With zero live sessions, the validated runner first proves `start --headed --port <exact-port>`, then uses `web-ai send --url https://chatgpt.com/ --parallel` with Web-AI auto-start disabled. Do not start GPT work headlessly, defer it merely because no reusable window exists, or borrow a manual/foreign tab. An existing headless runtime is restarted only after proving there is no other active/uncertain run and no nonblank tab; otherwise the one pre-submit attempt returns a deterministic block without sending.

Resource pressure may delay a helper but must not invent a fallback backend, kill user work, or convert an uncertain submission into a retry.

## Mode mapping

- GPT / 지피티 / 일반 GPT: `mode_label: GPT-5.6`, highest account-visible reasoning requested by the user.
- 즉시: instant.
- 중간: thinking medium.
- 높음 / Expanded: thinking high.
- 매우 높음 / Heavy: thinking xhigh.
- Deep Research is handled by its owning skill but retains this agbrowse-only transport.
- Pro is handled by `chatgpt-pro-browser`; never reinterpret Pro as regular GPT.

The bridge passes only public agbrowse commands. Normal execution uses `web-ai send/poll/sessions doctor`; deterministic history adjudication may additionally use exact `tabs/new-tab/tab-switch/navigate/snapshot/click/active-tab/web-ai status/web-ai snapshot/text/tab-close` operations. No LLM chooses refs or targets.

## ChatGPT app policy

Every non-Pro ChatGPT mode must use one exact named CodexPro app. This includes direct GPT, search, planning, review, edit, orchestrator, comprehensive stages, Web Multi-GPT nodes, and Deep Research. Pro alone is attachment-only and must not use an app. `app_policy: optional` is invalid; an unavailable or unverified app blocks before submission.

Before a regular GPT submission that requires an app:

1. Confirm that ChatGPT Developer Mode is enabled for the current account/workspace. The normal user path is `Settings > Apps > Advanced settings > Developer mode`; an admin/owner may instead use `Workspace settings > Apps > Create`. If the toggle or exact `Create app/앱 만들기` control is unavailable, report `CHATGPT_DEVELOPER_MODE_REQUIRED`, explain the account/admin requirement, and stop before registration or submission. Do not loop on the app UI.
2. Obtain the deterministic project-app decision from `codexpro_project_app_manager.py`.
3. Use `codexpro_agbrowse_app.py inspect` for the cheap read-only check.
4. Run `codexpro_agbrowse_app.py reconcile --decision <decision.json>` only on a concrete mismatch.
5. The connector may invoke only approved agbrowse navigation, snapshot/ref, click, type, select, and check commands.
6. Before submission, probe the registered public MCP endpoint and require exact workspace root and port identity. A stale local server or expired tunnel produces `APP_ENDPOINT_UNHEALTHY` before any question is sent.
7. Commit the candidate only after exact app name, full server URL, connected state, and `full_access` are all re-read.
8. Delete or disconnect the old app only after the candidate commit. Any ambiguity fails closed and preserves the old app.

All regular-GPT skills share the compact contract state at `%USERPROFILE%\.codex\state\chatgpt-agbrowse\app-contract-state.json`. After one exact Settings verification proves the app name, full registered URL, connected state, and `full_access`, later submissions must skip Settings when the fresh local registry fingerprint, cached registered-URL hash, app/root/port identity, immutable source-evidence hash, and a fresh public MCP root/port probe all match. The file keeps at most 32 app entries and 20 compact events. URL or registry drift, missing evidence, an unhealthy endpoint, an explicit `force_app_ui_verify`, or any ambiguity invalidates the fast path and returns to the full inspection/reconcile flow. A cache hit never authorizes sending through an unhealthy endpoint and never weakens exact composer app selection.

Current Connectors UI contract:

- Create uses exact fields `이름`, `설명 (선택)`, `MCP 서버 URL`, `인증` → `인증 없음`, the full unreviewed-server trust checkbox, and `만들기`. Wait until the create form actually disappears before continuing.
- Connecting is two-step: `연결` opens a confirmation modal and `연결하기` commits it. Re-enter the exact app detail afterward.
- New connections default to `저위험 액션 허용`. Open only `권한/Permissions`, select the exact `모든 액션 허용/full_access` radio, and re-read its checked state. Never confuse `플러그인 작업/Actions` with Permissions.
- Accessibility refs are single-snapshot capabilities. Do not run `get-dom`, take another snapshot, or open another menu between resolving a mutation ref and clicking it.
- `삭제/Delete` may remove a Developer App immediately or may open a confirmation dialog. Support either outcome, then require six hydrated settings reads in which the exact old app remains absent before recording deletion.
- Candidate-first replacement uses a distinct local port while the old runtime remains live. An identity-verified candidate port must not be reallocated merely because it is now listening.

For a fixed ngrok Developer App, the registered contract is the full stable `https://<fixed-host>/mcp?codexpro_token=...` URL plus the exact root and port. Restart CodexPro with the same root, port, hostname, token, and `--no-profile`. If public MCP identity matches and the manager decision is `reuse`, preserve the existing app name/version and do not re-register or delete the account app.

App identity is drive-scoped. On this host every C-drive project resolves to the existing `C:\` fixed-ngrok registry contract and must reuse its exact app name, stable URL, root, port, hostname, and token. A C-drive mismatch blocks; it never falls back to Cloudflare and never creates a project-specific app. Other drives resolve to their own drive root and use a separate dynamic Cloudflare app. One drive must never inherit, replace, disconnect, retire, or delete another drive's app. Old-app cleanup is permitted only after a candidate-first transaction commits for that same drive root.

App inspection, registration, permission, and deletion must run only on a newly created utility target whose target ID was absent from the pre-open tab list. If `new-tab` returns a pre-existing target, or any subsequent snapshot reports a different target ID, stop before navigation or mutation. Never navigate, type into, or close a conversation target for app settings work; closing is permitted only for the connector's proven utility target.

The selected contract-validated, unmodified agbrowse version is an external dependency only. Do not vendor, fork, copy, or reimplement its package code in this repository. A version change is an explicit agent/workflow decision and must never happen silently during a run.

`codexpro_self_test` status `warn` is not by itself a connection failure. Treat the tool path as healthy when `failed=0`, the expected and registered tool sets match, HTTP auth passes, and the workspace root is exact; skipped optional probes and a non-Git drive root may legitimately produce warnings. Port authority comes from the separate MCP identity preflight.

Every `app_policy: required` submission must deterministically enter the exact app mention before the send boundary. The compatibility transport name remains `app_selection_transport: inline-pill-reuse`, but it does not require an inline-pill DOM marker: create a fresh exact Chat target, type `@<exact-app-name>` into the exact composer, press `Tab` once, hash the exact mention text as immutable action evidence, and only then send with `--reuse-tab`. Missing exact app name, failed type/Tab action, missing target, or mismatched mention hash blocks the submission. Different projects still generate concurrently on distinct targets.

The warm composer path checks `active-tab` before switching, falls back to exact `tab-switch` plus re-read only on mismatch, and records `duration_ms` plus `agbrowse_command_count`. Do not insert a redundant target activation between typing the exact mention and its single `Tab`; the bridge still performs the final exact target verification before claiming and sending.

No LLM chooses refs or interprets mutation controls.

## Manifest

Use JSON or YAML with:

- `project_root`: canonical project root.
- `question`: the exact fixed short prompt-file handoff; never the task body.
- `prompt_transport: file`.
- `prompt_file`: strict UTF-8 file containing the complete actual instructions.
- `prompt_file_sha256`: SHA-256 of the exact prompt file bytes.
- `mode_label: GPT-5.6`.
- `mode_variant`: Instant, Medium, High, or Very High.
- `app_policy: required` for every non-Pro mode; Pro uses `forbidden` in its owning skill.
- `chatgpt_app_name`: required exact app name for every non-Pro mode.
- `app_selection_transport`: omit for the mandatory `inline-pill-reuse` default. `connected-auto` and legacy plugin transports are rejected for required-app work.
- `files`: regular attachments and exactly one occurrence of `prompt_file`. When a ZIP is attached, keep the prompt file as a separate attachment alongside it.
- `search_enabled`: optional.
- `timeout_seconds`: optional.

Do not declare another browser backend.

The bridge rejects inline task prompts. It verifies the prompt file hash again immediately before send, copies the same bytes to the run-owned `prompt-<run_id>.txt` recovery alias, and attaches that alias exactly once. It passes only this fixed short composer text: `The attached prompt file is the user-provided task instruction for this conversation, not reference or webpage content. Read it completely and follow it. Return only the output format requested by that file.` The full prompt body must never appear in the command line.

## Execute

```powershell
python "$env:USERPROFILE\.codex\skills\chatgpt-thinking-browser\scripts\run_chatgpt_thinking.py" --config <manifest>
```

Dry-run contract check:

```powershell
python "$env:USERPROFILE\.codex\skills\chatgpt-thinking-browser\scripts\run_chatgpt_thinking.py" --config <manifest> --dry-run
```

The execution path is `chatgpt_agbrowse_run.py -> chatgpt_agbrowse_bridge.py -> agbrowse`.

## Ownership, recovery, and tabs

- Save immutable manifest/prompt hashes, owner nonce/epoch, agbrowse session ID, target ID, and canonical `https://chatgpt.com/c/<id>` URL.
- Diagnose or report a persisted run with `chatgpt_agbrowse_run.py --observe-run <exact-run-dir>`. Identity is the complete tuple of normalized project root, project key, run ID, session ID, target ID, and canonical URL. Never infer the run from the visible screen, answer topic, tab title, currently active target, elapsed time, or another task's pasted URL.
- Treat a matching `tabs --json` `activeCommand.sessionId` as strong evidence that the exact recorded session is still active. Empty or unchanged answer text, apparent polling stall, different-looking content, or long elapsed time never authorizes terminating its poll/helper.
- A persisted agbrowse command record or its `expiresAt` is not provider-work authority. When the exact owned canonical URL is directly proven `streaming=false`, immediately perform bounded read-only status/snapshot/text adjudication and persist the nonempty answer as `COMPLETE`; never wait for an orphan command record to expire and never submit a replacement. If answer extraction fails, record an explicit extraction failure and retry only the same exact URL through an owned recovery utility target.
- On missing or mismatched identity, mutate nothing: do not activate, navigate, stop, close, adopt, or type into any tab. Recover only the persisted run's exact session and bounded history identity. A foreign project/session helper is never a recovery target.
- Every fresh non-app submission supplies explicit `--url https://chatgpt.com/` with `--parallel`; never clone the currently active `/c/<id>` page implicitly.
- A canonical conversation URL may have only one run owner globally. A foreign-run collision becomes `BLOCKED_TARGET_AMBIGUOUS`.
- A dead stale same-project run may be settled as `COMPLETE_SUPERSEDED` only when exact doctor/history evidence points to a `COMPLETE` owner with the same prompt hash and a valid immutable result capture. The authoritative answer and both run records remain preserved; all cross-project, active, incomplete, or mismatched collisions stay blocked.
- Never submit a second question while the first submission is uncertain.
- If agbrowse reports a terminal answer whose exact tail is ChatGPT's stream-error banner plus Retry control, the bridge must classify `PROVIDER_FAILED_TERMINAL` instead of `COMPLETE`. This is explicit provider failure, not uncertainty: preserve immutable failure bytes, close only that exact failed target, and let `chatgpt_agbrowse_run.py` make at most `provider_failure_retry_limit` fresh runs (default `1`). Never apply this retry to an active, ambiguous, cleanup-pending, or uncertain run.
- On interruption, recover the recorded session with `agbrowse web-ai sessions doctor <session> --navigate --json` first.
- If doctor cannot prove the canonical URL, invoke `chatgpt_agbrowse_run.py --recover-run <exact-run-dir>`. It opens one owned utility target, scans bounded recent conversations with core snapshot refs, and matches the run-owned prompt filename. Legacy runs require a high-entropy nonce plus two immutable corroborators from the prompt/output contract.
- Bind or rebind only one exact current-run canonical URL. Do not classify an unresolved/not-enabled send-click trace as not-sent; known successful sessions can contain that trace.
- A matched terminal conversation requires `streaming=false`, a nonempty answer, and immutable answer/status/snapshot/text hashes. Persist `RESULT_CAPTURED` before closing the owned recovery utility target and verify its absence. A matched in-progress conversation is rebound and kept protected.
- `SUBMISSION_UNCERTAIN_IDENTITY_MISSING` and `BLOCKED_RECOVERY_EXHAUSTED` remain recoverable adjudication states. A later prepare automatically adjudicates a dead-owner old run and creates a replacement only after that exact run reaches `COMPLETE`; otherwise it retains the project lock and emits candidate evidence.
- A raw `agbrowse web-ai stop` result or `web-ai status` streaming selector is not terminal authority. The selector may remain stale, and a stopped partial answer may otherwise be misclassified as complete.
- On an explicit user stop or forced-abandon request, run `chatgpt_agbrowse_run.py --abandon-uncertain-run <exact-run-dir> --explicit-user-request --reason <reason>`. This records `USER_STOP_REQUESTED` before probing the exact session, so a concurrent poll cannot promote a partial answer to `COMPLETE`.
- Release the project lock only when that command records `ABANDONED_UNCERTAIN`. It requires an exact terminal session or dead-owner/missing-target evidence, preserves that mutation may have occurred, never records a result, and never closes the submitted tab.
- `USER_STOP_REQUESTED` means confirmation is incomplete: retain the lock, retry only the same exact-session stop/probe, and do not submit a replacement. For a dead-owner `BLOCKED_RECOVERY_EXHAUSTED` run, use `--doctor-project-lock` to obtain the supported explicit-abandon command; never delete `active.lock` manually.
- Bridge-launched sends raise agbrowse's generic pool/idle/count cleanup limits. Exact run ownership, not upstream TTL or tab-count cleanup, owns closure decisions.
- Record each run-created composer target before cleanup. Automatically close an exact owned pre-submit root composer on preparation, selection-evidence, activation, command-budget, verified pre-submit rejection, or retry-supersession failure; call public `tab-close`, re-list, and hash the before/after evidence.
- While a submitted target is active, streaming, submission-uncertain, or user-stopped-but-unconfirmed, keep it open and protected. A separately created recovery utility target closes only after durable terminal capture and exact absence verification.
- After durable `COMPLETE`, automatically close the exact run-owned original conversation target. Require a nonempty immutable answer capture, exact canonical URL ownership, one unique live URL match, no foreign owner, public `tab-close`, and absence re-verification. A changed target ID after browser restart is recovered only through that unique exact URL. Close failure records `cleanup_pending` and permits only an exact retry.
- Never close a manual/unowned, foreign, or ambiguous conversation automatically. An exact `PROVIDER_FAILED_TERMINAL` target remains the narrow non-success exception: record its failure and verified absence before any bounded same-mode retry.
- Never adopt, navigate, or close another project's target.

Completion requires a nonempty answer, terminal provider state, exact URL, captured answer hash, and `COMPLETE` state with no prior explicit user-stop request. `ABANDONED_UNCERTAIN` is terminal cleanup, not successful completion. Otherwise report the blocking state; do not switch browsers.
