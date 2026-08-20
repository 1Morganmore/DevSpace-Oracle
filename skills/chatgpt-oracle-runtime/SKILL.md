---
name: chatgpt-oracle-runtime
description: "Current Oracle runtime path for new ChatGPT work: regular modes use the manually registered DevSpace app, qualified Pro uses the pro-devspace write transport, evidence Pro uses pro-attachment-only, and it includes recovery, comprehensive relay, and genuine multi-session Web Multi-GPT."
---

# ChatGPT Oracle Runtime

This is the only active browser path for all GPT work. Regular modes use
DevSpace. Qualified Pro uses the `pro-devspace` transport — the DevSpace
mention plus the absolute mission path, with mission-scoped file writes and
command execution confined to the exact project root. Explicit
immutable-evidence Pro uses the `pro-attachment-only` transport without any
app. New runs pin Oracle `0.18.0`; Oracle `0.16.1`, `0.17.0`, `0.17.1`,
`0.17.2`, and `0.17.3` are accepted only when recovering an exact run already
persisted with that version. Oracle 0.18.0's upstream disabled-tier detection
and manual-login reattach cookie-sync opt-in are preserved under the local
hash-gated patches together with strict visible Power proof, per-run copied
profiles, and the existing timeout budgets; live browser validation is not yet
performed.

`chatgpt_oracle_dispatch.py` supports exactly `direct`, `plan`, `review`, `edit`,
`orchestrator`, `deep-research`, `manual`, and `pro`. `manual` is a supported
`manual-no-launch` profile, not a new submission route. `answer` in
`chatgpt-question-designer` is the prompt-design alias for dispatcher mode
`direct`, not a separate dispatcher key. Regular routes
select `gpt-5.6` and send exactly `@DevSpace` plus the absolute UTF-8 mission
path — no task body and no extra operational prose. The web GPT must use only
the exact project root recorded in that mission, read the mission and applicable
`AGENTS.md` completely first, and may retry that same root once after a timeout.
It must not substitute a parent, child, active workspace, or shell boundary
workaround. Evidence Pro (`pro-attachment-only`) selects `gpt-5.6-sol` with
`heavy` and sends one short
instruction plus exact attachment files; it never mentions DevSpace. Qualified
Pro (`pro-devspace`) selects the same model and effort, mentions DevSpace, and
sends the absolute mission path with no attachments. Pro runs only on an
explicit user request; no route promotes itself to Pro automatically.
Regular routes use the single supported `extra-high` tier and require Oracle
evidence for the visible `Extra High` (`Power 4 of 5`). Never invent xhigh,
use `Medium`/`High`, or silently downgrade.
Web Multi runs only when explicitly selected; it is never an automatic
transition or fallback for a regular or failed run.

## Manifest

Require schema `codex.chatgpt.oracle-run/v1` with:

- `project_root`: absolute existing directory.
- `mission_path` plus caller-pinned `mission_sha256`: absolute UTF-8 regular
  file inside the project and its exact bytes.
- `app_name`: one-line app name, without a leading `@`, for regular routes.
- `task_kind: pro` for Pro. The evidence route (`pro-attachment-only`) adds
  one or more exact `attachments`, ordered `attachment_sha256s`,
  `project_context_manifest_path`, and `project_context_manifest_sha256`; the
  qualified route (`pro-devspace`) forbids attachments and
  `project_context_manifest_*` fields
  (`PRO_DEVSPACE_ATTACHMENTS_FORBIDDEN`).
- `mode`: `browser`.
- Optional `run_root`, `oracle_command`, `oracle_args`, `thinking_time`,
  hash-validated `copy_profile`, and mutex timeout.
- Regular direct/orchestrator manifests use `task_outcome_contract: "v1"`;
  qualified `pro-devspace` manifests follow the same caller-chosen `legacy` or
  `v1` rule (dispatch uses `v1`, comprehensive uses `legacy`). Evidence
  `pro-attachment-only` keeps the forced legacy contract with a
  `not_applicable` classification.

## Run

Preview first:

```powershell
python skills/chatgpt-oracle-runtime/scripts/run_chatgpt_oracle.py run --manifest C:\absolute\oracle-job.json --dry-run
```

The preview must include final argv, prompt first line, absolute mission path, SHA-256, and artifact paths without launching Oracle or a browser.
Use this wrapper preview only. Do not substitute Oracle's own browser `--dry-run`.

Execute only after an explicit live-run request:

```powershell
python skills/chatgpt-oracle-runtime/scripts/run_chatgpt_oracle.py run --manifest C:\absolute\oracle-job.json --expected-manifest-sha256 <manifest.actual_sha256>
```

Complete requires Oracle exit code zero, a nonempty `--write-output` artifact,
and—when `task_outcome_contract` is `v1`—a final
`TASK_OUTCOME: EXECUTED` marker. `TASK_OUTCOME: NOT_EXECUTED` and
`TASK_OUTCOME: BLOCKED` preserve terminal transport evidence but return
attention-required; transport success alone never claims project execution.
The composer sends only the app mention plus the absolute mission path, so the
outcome contract lives in the mission: mission authors must require every
citation, footnote, and Markdown reference definition to appear before the
final marker, keeping that marker the final nonempty line. The classifier
still accepts the bounded provider-rendered case of exactly one marker followed
solely by blank lines or single-line HTTP(S) Markdown reference definitions;
ordinary trailing prose, a second marker, or a multiline or non-HTTP definition
remains `unknown`.
A nonzero Oracle exit after launch, including a browser response timeout, is
`attention_required` rather than proof that the web session failed. It retains
same-project ownership and permits only exact-slug `live` or `harvest`
recovery; it never authorizes a replacement submission.
For non-Pro runs, `--browser-timeout` is one overall answer budget. Oracle
fallback capture consumes only the remaining time. A host wall-clock watchdog
adds a short grace for a wedged CDP call; if it expires, the runner returns
`post_submit_watchdog_timeout`, preserves the exact process/session and browser
evidence, and remains unsafe for a fresh submission.

## Preflight and triage

After the exact dry-run preview, validate the same manifest and hash without a
browser or submission:

```powershell
python skills/chatgpt-oracle-runtime/scripts/run_chatgpt_oracle.py preflight --manifest C:\absolute\oracle-job.json --expected-manifest-sha256 <manifest.actual_sha256>
```

Preflight invokes the pinned Oracle version, inspects exact compatibility hashes,
requires the signed-in profile seed, checks unresolved project ownership, and for
DevSpace validates the exact listener plus local/public `/healthz` identity and
Tailscale Funnel mapping. It does not patch packages, create run state, inspect
ChatGPT UI, or submit. A failed check is `not_ready`; fix it and rerun preflight.
Preflight is advisory evidence, not a reusable authorization. A DevSpace `run`
repeats the volatile hostname, Funnel, and strict local/public `/healthz` checks
inside the existing project submit mutex immediately before Oracle launch. A
failure persists structured readiness evidence and settles as a proven
pre-submit failure without opening a browser or conversation. Qualified Pro
(`pro-devspace`) runs this DevSpace readiness path like regular DevSpace work.
Evidence Pro and exact-run
recovery do not use this DevSpace readiness path.

Use `chatgpt_oracle_diagnose.py triage --run-dir <exact-run>` for a bounded next
action and `watch --run-dir <exact-run>` for read-only NDJSON lifecycle changes.
Only execute the returned exact recovery argv when triage identifies that action;
an active session is watched, never recovered or replaced. The aggregate bucket
overview is `chatgpt_oracle_diagnose.py --summary-only` (no subcommand); that
flag is rejected with `triage`/`watch`, which are single-run forms.
Persisted nonempty `blocked` and `not_executed` outcomes classify as terminal
task non-execution before a complete lifecycle can classify the run as complete.
Exact same-artifact `OAuth token request failed` plus `503` evidence has its own
registered-app OAuth signature. Persisted `unknown` and malformed or ambiguous
v1 marker output remain unresolved; diagnosis never invents a loose marker-only
settlement.
An unsettled Oracle `session not detected` refusal that exited before send is reported by
triage as `session-absent-awaiting-user-confirmation` with a
`settle_no_submission` argv: run that exact settle command
(`chatgpt_oracle_run.py settle-no-submission --run-dir <exact-run> --confirmation user-confirmed-no-submission --reason <reason>`)
only after the operator explicitly confirms the run was never submitted.

## Recovery

Recovery always reuses the stored Oracle slug and never restarts or submits:

```powershell
python skills/chatgpt-oracle-runtime/scripts/run_chatgpt_oracle.py recover --run-dir C:\absolute\run --action harvest
```

Use `--action live` only to keep following the same stored session. A successful recovery must write a nonempty stored `output.md`, update `state.json` to `complete`, and refresh `transcript.md`; exit code zero without output is `attention_required`.
The CLI keeps `--action live` inside one exact-slug recovery process for up to
90 minutes by default. Transient `stalled`, `running`, or observer disagreement
states keep the same live authority and project lock; they do not return every
few minutes for Codex-side polling. When the exact session becomes terminal,
the same process performs one harvest and returns once.
Every exact recovery writer is serialized by a mutex derived from that run
directory. It never acquires or waits for the project submission mutex:
unresolved project ownership still blocks every fresh submission independently.
After its normal observer wait, an original run rereads durable state and cannot
overwrite a terminal harvested result written by exact recovery.
If Oracle proves both that no live tab matches the exact slug and that its
metadata has no recoverable canonical conversation URL, the runner returns
`recovery_binding_unavailable` immediately instead of repeating that invariant
failure for 90 minutes. It preserves `submitted_unknown` ownership; restore the
exact persisted conversation URL before recovering the same slug, and never
replace or resubmit it.

Oracle's `Prompt did not appear in conversation before timeout (send may have
failed)` message is likewise submission-uncertain. No-live-tab plus missing
saved-URL recovery evidence does not mechanically prove non-submission. A
validated Oracle 0.17.1 `APP_MENTION_ROUTE_UNCONFIRMED` rejection is also
eligible because the compatibility contract clears the composer and throws
before either send path. A maintenance owner may release either exact run only
after explicit user confirmation through `chatgpt_oracle_run.py
settle-no-submission` with the exact run directory, `--confirmation
user-confirmed-no-submission`, and a concise reason. The settlement is hash-bound to
project/workflow/stage/attempt/input evidence and does not launch Oracle;
comprehensive mode may consume only one replacement for that binding.

Direct same-project runs hold one cross-process mutex for the entire Oracle
process lifetime. A Multi parent owns that project mutex while authorized
children use a short parent-scoped launch mutex and isolated copied Chrome
profiles, then wait concurrently.
Control state, Oracle output, and transcripts live under
`%USERPROFILE%\.codex\state\chatgpt-oracle`, outside the DevSpace-writable
project.

Use `chatgpt_oracle_comprehensive.py` for the bounded plan → explicitly selected
Pro/Multi → review → implementation → final web gate flow. Each web stage
writes the next mission; the host validates only UTF-8, identity, paths, and
hashes. Use `chatgpt_oracle_multi.py` for independent solver sessions in waves
of at most five and one merger over handoff files.
