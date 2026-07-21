---
name: chatgpt-pro-browser
description: Use for ChatGPT Pro web automation, Pro planning, Pro research, and attachment-only Pro work through explicitly selected, contract-validated, unmodified agbrowse.
---

# ChatGPT Pro via agbrowse

The only backend is an unmodified agbrowse installation selected by an exact validated contract. `0.1.18` is the tested default. There is no in-app Browser, browser-use, custom CDP/Playwright, or `@chrome` fallback.

## Non-negotiable Pro contract

- `mode_label: Pro`.
- `app_policy: forbidden`.
- Never select, connect, inspect, register, repair, or delete a ChatGPT app for a Pro run.
- Local context is attachment-only through repeated agbrowse `--file` arguments.
- Never downgrade Pro to regular GPT because mode selection, upload, browser startup, or recovery failed.
- Search is enabled only when requested.

## Preflight

1. Do not run the resource guard as a routine or pressure gate. Use orphan cleanup only when parent-missing helper accumulation is actually evidenced.
2. Resolve and validate the workflow's exact agbrowse contract; default to `%USERPROFILE%\.codex\contracts\agbrowse-0.1.18.json` only when no other versioned contract was explicitly selected.
3. Verify every attachment is a regular non-symlink file and freeze the manifest hash.
4. Claim one project run lease. Same-project overlap blocks; distinct projects may use independent `--parallel` sessions.
5. Supply explicit `--url https://chatgpt.com/` for the fresh parallel target so an active conversation is never cloned into the Pro run.
6. Keep the exact session ID, target ID, and canonical conversation URL.

An already live agbrowse session or window is not a precondition. Zero live sessions is the normal cold-start case: before any app/composer/send mutation, the shared runner calls the validated executable with `start --headed --port <exact-port>`. Only after that succeeds does it call `web-ai send --url https://chatgpt.com/ --parallel`, with Web-AI auto-start disabled so a different runtime mode cannot appear between proof and send. Never start Pro with `start --headless`. If the exact port contains an agbrowse-owned headless runtime, it may be stopped and restarted headed only when no other active/uncertain run and no nonblank tab exists; otherwise return one deterministic pre-submit block without attempting a send.

## Manifest

Required fields:

- `project_root`.
- `question` or `prompt`.
- `mode_label: Pro`.
- `app_policy: forbidden`.
- `files`: one or more attachment paths for attachment-backed work.

Optional fields include `search_enabled`, `timeout_seconds`, `goal`, `constraints`, and `output`.

Any app name in a Pro manifest is a hard error.

## Execute

```powershell
python "$env:USERPROFILE\.codex\skills\chatgpt-pro-browser\scripts\run_chatgpt_pro.py" --config <manifest>
```

Dry run:

```powershell
python "$env:USERPROFILE\.codex\skills\chatgpt-pro-browser\scripts\run_chatgpt_pro.py" --config <manifest> --dry-run
```

## Failure and recovery

- Use `chatgpt_agbrowse_run.py --observe-run <exact-run-dir>` before diagnosing a running or uncertain Pro task. The visible page, answer subject, active tab, or a URL pasted from another task is not run identity. Require the persisted project/run/session/target/canonical-URL tuple.
- A matching tab `activeCommand.sessionId` protects that exact Pro poll/helper as active. Zero captured characters, apparent stalling, unrelated-looking text, and elapsed time do not authorize stopping the helper or switching to another session.
- A provider-surface preflight rejection with `mutationAllowed: false` is a safe `SEND_REJECTED`; repair and resume the same run.
- Any exception, timeout, or invalid JSON after the send boundary becomes an uncertain submission block.
- A terminal ChatGPT stream-error banner is not a Pro answer. Record `PROVIDER_FAILED_TERMINAL`, preserve the exact failure bytes, close only its exact failed target, and allow the shared runner's bounded fresh-run retry without changing Pro mode or its attachment-only policy. Never convert an active or uncertain Pro run into this retry path.
- Poll only the recorded agbrowse session.
- Reject any canonical conversation URL already owned by another run.
- Narrow exception for stale same-project recovery: when an exact doctor/history match resolves to a URL already owned by a `COMPLETE` run with the same project and prompt hash and a valid immutable result capture, preserve both records and the authoritative answer, mark only the stale duplicate as `COMPLETE_SUPERSEDED`, and release its orphan lock. Different-project, different-prompt, incomplete, active, or weakly evidenced owners remain `BLOCKED_TARGET_AMBIGUOUS`.
- Job identity is the exact canonical conversation URL plus the run-owned `prompt-<run_id>.txt` filename. Never mix another URL/run into it; target/PID/heartbeat/lock/poll state are diagnostic only and cannot override exact terminal web evidence. Recover the persisted canonical URL first and observe its unique exact live target without navigation. Never run `poll --navigate` or a navigating doctor against a run with a known canonical URL. Only when the URL is missing may recovery inspect it with `agbrowse web-ai sessions doctor <session> --json` (without `--navigate`) and then invoke `chatgpt_agbrowse_run.py --recover-run <exact-run-dir>` for bounded read-only history adjudication. New runs match their unique `prompt-<run_id>.txt`; legacy runs require a high-entropy nonce plus two immutable corroborators.
- Accept only an exact `https://chatgpt.com/c/<id>` URL owned by the run.
- Never treat an unresolved/not-enabled send-click trace as proof that Pro was not submitted. An exact history match may rebind only the original Pro run and must retain Pro attachment-only/app-forbidden policy.
- Never create a replacement submission while completion is uncertain.
- Keep the original submitted Pro tab open while it is active, streaming, uncertain, or user-stopped-but-unconfirmed. After durable `COMPLETE` and nonempty immutable answer capture, automatically close its exact run-owned target only when the canonical URL has one unique live match, no foreign owner exists, and absence is re-verified. A separately created recovery utility target follows the same evidence-first ownership rule; manual/unowned, foreign, and ambiguous tabs remain protected.
- Do not treat raw `agbrowse web-ai stop`, a stale streaming selector, or a short terminal transcript as proof of successful completion.
- For an explicit user stop, use `chatgpt_agbrowse_run.py --abandon-uncertain-run <exact-run-dir> --explicit-user-request --reason <reason>`. Only `ABANDONED_UNCERTAIN` releases the exact project lock; `USER_STOP_REQUESTED` retains it until exact-session terminal evidence exists. Neither phase permits a Pro-to-regular downgrade or tab closure.

Completion requires exact mode evidence from agbrowse, terminal response state, nonempty answer capture, canonical URL, immutable result hashes, and no explicit user-stop request. `ABANDONED_UNCERTAIN` is not completion.
