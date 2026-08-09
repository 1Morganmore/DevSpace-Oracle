---
name: chatgpt-thinking-browser
description: Run regular ChatGPT direct, plan, review, edit, and orchestrator work through Oracle plus the manually registered DevSpace workspace app.
---

# Regular ChatGPT through Oracle + DevSpace

Read `chatgpt-question-designer` first when shaping a new mission.

For new work, create one absolute UTF-8 mission file inside the project and
resolve the requested mode through:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_dispatch.py" --mode <direct|plan|review|edit|orchestrator> --project-root C:\project --mission-path C:\project\mission.md --manifest-output C:\project\.ai-bridge\oracle.json --reasoning-level "Very High" --chatgpt-project-url https://chatgpt.com/g/g-p-example/project --dry-run
```

`--chatgpt-project-url` is optional. When present it must be the exact project
URL; Oracle starts the new conversation inside that project. Never fuzzy-match a
project name or silently fall back to the ChatGPT root page.

For a reusable local alias, set the exact URL with
`chatgpt_oracle_projects.py set <name> <url>` and pass
`--chatgpt-project <name>`. The dispatcher resolves the alias before hashing and
stores only the exact URL in the launch manifest.

Project placement does not prove that the DevSpace connector is available in
that Project chat. A terminal `TASK_OUTCOME: BLOCKED` reporting an unavailable
connector is a real capability failure: preserve that exact result and never
resubmit or silently fall back to a root chat.

For an explicitly authorized live web run, replace `--dry-run` with
`--expected-manifest-sha256 <oracle_manifest_sha256>` using the exact top-level
hash from that preview. The runtime
sends plain `@DevSpace` plus the absolute mission path. It never attaches files,
opens ChatGPT settings, inspects/selects/deletes an app, or falls back to
another backend, Playwright, in-app Browser, or Chrome.

New runs pin Oracle `0.17.1`. Oracle `0.16.1` and `0.17.0` are available only for exact
recovery of a run already persisted with that version.

`orchestrator` is a single web submission that carries the orchestrator
ownership contract: that one GPT session owns delegated exploration, code
authoring, tests, and internal parallel lanes, and its answer is the result.
It has no stages, no stage receipts, and no local gate. Do not confuse it with
comprehensive mode, which is a multi-stage workflow owned by
`chatgpt-pro-plan-handoff` and `bin/chatgpt_oracle_comprehensive.py`.
Comprehensive mode runs `orchestrator`-equivalent work as its implementation
stage, so it contains this mode rather than competing with it.

Choose `orchestrator` when the goal and approach are already settled and one
authorized execution pass should finish the work at the lowest cost. Choose
comprehensive mode when the plan itself needs an independent review stage,
when Pro or Web Multi must participate, or when completion must be proven by a
deterministic local gate.

Web Multi participates only when explicitly selected. Do not transition a
regular or failed run into Web Multi automatically.

Never probe, register, repair, or select an alternate app or backend.

Oracle explicitly selects `GPT-5.6 Sol` and `extra-high`, verifies the visible
`Extra High` tier before prompt send, and records both in Oracle evidence. The active 0.17.1
compatibility layer is hash-gated and fails closed on an unknown version or
third-party file. Never invent xhigh or silently downgrade.

On the current Power-slider UI, Oracle verifies `Power 4 of 5` for regular
`extra-high`; attachment-only Pro uses the same verified `GPT-5.6 Sol` model
with `Power 5 of 5` (the visible `Pro` choice). `heavy` is only Oracle's
internal compatibility token for that latter choice, never a claimed UI label.

Every new run copies the manually signed-in Oracle profile into a throwaway
per-run profile and asks Oracle to hide its owned window. This isolates
different projects: one completed run cannot close another run's live Chrome.
Do not replace this with the shared manual-login profile.

Control state and final Oracle output are host-only below
`%USERPROFILE%\.codex\state\chatgpt-oracle`. Complete requires exit zero and
fresh nonempty host output. Recovery uses the stored slug:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_run.py" recover --run-dir C:\exact\host-run --action harvest
```

Recovery never restarts/resubmits and never downgrades durable COMPLETE. If the
persisted CDP endpoint died, Oracle may launch a bounded recovery browser from
the run's recorded profile seed and open only that slug's exact persisted
conversation URL for harvest. It must not use a prompt or create a replacement
conversation. Session authority is monotonic: a later `running` observation
cannot downgrade `terminal_observed`. That disagreement remains
attention-required with the same project lock; a later exact terminal harvest
with fresh nonempty output settles it to COMPLETE.
