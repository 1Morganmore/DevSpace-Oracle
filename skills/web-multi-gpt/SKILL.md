---
name: web-multi-gpt
description: Run genuine parallel regular ChatGPT sessions through Oracle, with stable solver lanes, waves of at most five, file handoffs, and one merger. No single-GPT role simulation or alternate backend.
---

# Oracle Web Multi-GPT

Use `bin/chatgpt_oracle_multi.py` with schema
`codex.chatgpt.oracle-multi/v1`. Required fields:

- absolute `project_root`, project-contained `output_dir`
- `solvers`: 2..25 unique safe lane IDs, absolute mission paths, and exact
  lowercase `mission_sha256` values for the authored bytes
- `merger_mission_path` and its exact lowercase `merger_mission_sha256`
- `max_concurrency`: 1..5
- optional `parallel_policy` with exact `when: explicit-user-request`, a
  `max_total_sessions` cap counting every solver plus the merger, and a
  `max_concurrency` cap. New natural-language multi requests include it.
- optional `next_stage_result_path` for comprehensive relay
- optional exact `chatgpt_project_url` to start every independent solver and
  merger conversation inside the same ChatGPT Project

Advisory lanes are `access: read-only`. A write lane must declare
`access: worktree-write` and a distinct pre-created worktree `project_root`;
the canonical root is forbidden for write lanes.

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_multi.py" --manifest C:\project\multi.json --dry-run
```

Use the preview's exact `manifest_sha256` for the authorized live run:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_multi.py" --manifest C:\project\multi.json --expected-manifest-sha256 <manifest_sha256>
```

The preview returns `parallel_plan` with solver, merger, total-session, and
concurrency counts. Review those counts before authorizing a live run.

Each lane receives its own Oracle slug/run/output and only `@DevSpace` plus its
mission path. Lanes run in stable waves of at most five; a larger topology is
not reduced. Successful handoffs are preserved with exact SHA-256 bindings and
revalidated immediately before exactly one merger consumes them in lane order.
The merger child manifest carries the same pairs as `bound_inputs` for one last
runner check inside the submit mutex.
A reduced topology preserves its partial artifacts, but returns `ok=false` and
requires attention instead of advancing the comprehensive workflow. The parent
holds same-project exclusion while child
launches use a short parent-scoped mutex. On Windows each lane uses a separate
throwaway copy of the signed-in Oracle profile, preventing one solver from
closing or taking over another solver's Chrome session.

No attachments, app/settings automation, broad tab cleanup, `--force`,
restart, silent resubmission, or alternate solver/merger transport. Oracle owns
one-shot tab archival.
