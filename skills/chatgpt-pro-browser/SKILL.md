---
name: chatgpt-pro-browser
description: Use for a one-shot ChatGPT Pro attachment-only plan, research, or review through Oracle. Return the Pro result only; never continue into comprehensive implementation. agbrowse and CodexPro are legacy recovery-only.
---

# ChatGPT Pro through Oracle

## Standalone scope

This is the standalone, one-shot Pro route. It may produce a plan, research
finding, review, or decision, but it returns that durable Pro result to Codex
and stops. It never starts a review-to-implementation chain, authors a
follow-on implementation stage, or invokes `chatgpt-pro-plan-handoff` on its
own. If the user asks for comprehensive mode, use `chatgpt-pro-plan-handoff`
instead; an optional Pro stage inside that workflow remains owned by the
comprehensive runner.

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
- Every new Pro consultation carries a project-specific, judgment-complete,
  maximum-useful-context packet. This is the universal default for every
  project and every Pro consultation; it requires no project-specific opt-in.
  A thin prompt or a convenience subset is not sufficient when more
  decision-relevant project evidence exists.
- Search or research is enabled only when explicitly requested and supported by
  the selected Pro route.

## Maximum project-context packet

Build the largest safe packet that improves the requested judgment. For every
project type, adapt the evidence mix to that project's domain, artifacts,
authority chain, lifecycle, and immediate decision. Fill the verified effective
Oracle/Pro attachment and model-context budget up to the practical maximum with
all non-duplicative, project-specific, decision-relevant project evidence. A
smaller packet
is valid only when useful project evidence is exhausted or a recorded safety,
transport, or format boundary prevents inclusion. Reserve deterministic
headroom for the mission, evidence index, and final Pro answer; never pad with
irrelevant files merely to increase byte count.

The evidence index must record the effective attachment/context budget used for
the run, included bytes or tokens when measurable, reserved answer headroom,
and why any remaining capacity could not improve the decision. When evidence
exceeds the boundary, allocate space deterministically in this order: governing
rules and exact question; canonical current state and measured primary evidence;
conflicts, failures, controls, and prior decisions; source or implementation
feasibility; then compact supporting detail. Record every excluded or truncated
artifact with its path, hash when safe, coverage boundary, priority, and expected
decision impact. Do not silently stop at a customary file count or convenience
bundle size when the verified route permits more useful context.

The packet must be specific to the resolved project and represent each category
below using the domain-appropriate equivalent. These categories apply across
software, research, finance, operations, creative, and other projects rather
than only to any one repository or domain. If a category has no evidence,
record that exact omission and its effect on the decision instead of silently
leaving it out.

- the user's durable objective, immediate question, deadlines, capital or other
  quantitative target, and acceptance/stop conditions;
- the complete applicable `AGENTS.md`/project-rule chain and any governing
  specifications, contracts, schemas, or plans;
- canonical current state, active work, frozen decisions, open candidates, and
  exact paths to authoritative artifacts;
- measured results with sample periods, costs, controls, nulls, concentration,
  failure modes, and data-coverage or lifecycle limitations;
- killed, rejected, blocked, or near-duplicate routes so Pro cannot recommend a
  renamed historical failure without explicitly adjudicating the conflict;
- prior Pro, regular GPT, Multi-GPT, external-review, or human decisions that
  materially affect the question, preferably as exact durable outputs;
- source feasibility, timestamps, hashes, provenance, unresolved contradictions,
  resource constraints, and current execution or collection boundaries;
- the arithmetic connecting the proposed decision to the user's real target,
  including capacity, cost, risk, and time-to-evidence where applicable.

Use the executable universal builder before every submission; policy prose is
not a substitute for its validator. It takes an explicit evidence allowlist
only and never recursively scans a project:

```powershell
python <skill-root>\scripts\build_project_context_packet.py build --manifest C:\project\.ai-bridge\pro-context-manifest.json
python <skill-root>\scripts\build_project_context_packet.py validate --manifest C:\project\.ai-bridge\pro-context-manifest.json
```

The UTF-8 JSON manifest uses schema
`codex.chatgpt.pro-project-context/v1` and must declare the exact absolute
`project_root`, non-empty `question`, non-empty project-specific
`required_categories`, `local_transport_envelope_bytes`,
`answer_headroom_bytes`, `metadata_reserve_bytes`, root-contained
`packet_path`, and an `evidence` allowlist. Each evidence entry has exactly
`path`, `category`, `priority`, and frozen `sha256`. The declared envelope is a
fixed tested local profile `oracle-pro-local-envelope-2026-08-03/v1`: total
uncompressed envelope 64 MiB, answer headroom 8 MiB, metadata reserve 1 MiB,
evidence budget 55 MiB, and a 32 MiB cap for each evidence file and packet ZIP.
These are a local proven/configured transport envelope, not vendor or model
limits; callers must use the exact values or preflight fails closed. The
builder writes the ZIP and its adjacent immutable-hash receipt; validation
fails closed on root escape, symlinks, stale hashes, unsafe evidence, duplicate
paths/archive collisions, absent required categories, and budget overflow.

Prefer one deterministic, path-preserved ZIP plus the short UTF-8 mission. The
ZIP must contain a root mission/packet, an evidence index, original absolute and
project-relative paths, per-entry SHA-256 and size, source qualification,
known omissions, and the exact prior answer when the new request challenges or
revises that answer. Preserve compact exact artifacts before prose summaries;
when raw evidence is too large, include a deterministic compact derivative and
the raw artifact's path/hash/coverage boundary.

Exclude credentials, secrets, cookies, browser/profile state, account or live
trading state, databases/WAL files, volatile logs, caches, and unrelated bulk.
Do not scan or hash a live database merely to enlarge a packet. Maximum context
means maximum useful and safe judgment context, not maximum filesystem volume.

The mission must tell Pro to read every attachment and the evidence index,
resolve contradictions against the stated authority order, distinguish observed
evidence from inference, and return exact decisions, gates, stop rules, target
arithmetic, and next actions. Preflight fails closed if a required category is
missing without an explicit omission record, if hashes are stale, or if the
packet cannot be tied to the exact project root and question.

## Required Web Multi decision

Every Pro mission must end by requiring this exact decision block in the Pro
answer:

```text
WEB_MULTI_NEEDED: YES|NO
WEB_MULTI_REASON: evidence-based reason tied to the decision and alternatives
```

Pro must choose `YES` only when three to five materially independent regular
GPT investigations would improve a non-trivial decision; it must not use Web
Multi for a trivial, single-answer, or purely mechanical question. If the
answer is `YES`, Pro must additionally author a ready-to-run Web Multi-GPT Very
High mission that specifies: three to five independent roles and questions,
the same project maximum-context evidence and the durable Pro answer each role
must use, stable lane order, and synthesis/judge criteria. The mission must
preserve the exact project root, read-only evidence boundaries, and the normal
same-project serialization contract.

After a durable Pro answer says `WEB_MULTI_NEEDED: YES`, Codex starts that
ready-to-run Oracle Web Multi mission automatically without a routine user
choice. It waits for the exact Pro session to be terminal first, keeps the
same-project lock/slug safety, and never resubmits or replaces the Pro session.

## Preflight

1. Do not run the resource guard as a routine or pressure gate.
2. Resolve and hash-validate the tested Oracle compatibility contract.
3. Build then validate the short UTF-8 mission and the judgment-complete
   maximum-useful-context packet with
   `build_project_context_packet.py`, including its effective budget/use/headroom record,
   deterministic priority order, evidence index, frozen hashes, and omissions.
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
transcript. Oracle waits on the original submitted session for its bounded
90-minute answer budget. Oracle archives only after the durable one-shot output
is saved.

## Recovery

Diagnose only the exact stored Oracle run directory and slug:

```powershell
python "$env:USERPROFILE\.codex\skills\chatgpt-oracle-runtime\scripts\run_chatgpt_oracle.py" recover --run-dir C:\exact\oracle-run --action harvest
```

Use `live` only to continue following that same stored session. Recovery never
restarts, resubmits, changes the model, changes attachments, or creates a
replacement. A zero exit without nonempty output remains `attention_required`.
If the initial run reports `post_submit_response_timeout`, the submitted Pro
response was still pending at Oracle's deadline: retain the exact lock and
wait passively. Do not launch repeated `live`/`harvest` recovery while the
conversation is visibly working; use exact recovery only after the original
observer/browser is no longer available.

For an already persisted agbrowse Pro run only, the former exact
`chatgpt_agbrowse_run.py --observe-run|--recover-run <run-dir>` commands remain
available. They must never create a new run.
