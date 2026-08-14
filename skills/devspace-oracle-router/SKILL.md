---
name: devspace-oracle-router
description: Route natural-language Korean or English requests from Orca Codex, OpenCode, and other terminal providers to DevSpace Oracle direct GPT, plan, review, edit, orchestrator, deep research, Web Multi-GPT, Local Multi-GPT, comprehensive, Pro, Project-profile, recovery, and DevSpace-root operations. Use when the user mentions DevSpace Oracle, ChatGPT Project memory, 일반 GPT, 직접, 계획, 검토, 수정, 지휘, 심층 리서치, 딥 리서치, 웹 멀티 GPT, 로컬 멀티 GPT, 종합모드, Pro, Extra High, or changing a DevSpace path.
---

# DevSpace Oracle natural-language router

Interpret the user's request, choose exactly one primary route, show the dry-run
receipt, and start a paid/live web run only after explicit authorization. On
Windows use the persistent `$env:USERPROFILE\.codex\bin`; Orca may inject a
temporary `CODEX_HOME`. On POSIX use `$CODEX_HOME/bin` when set, otherwise
`$HOME/.codex/bin`.

## Route modes

- 일반 GPT, GPT, 직접, direct: `chatgpt_oracle_dispatch.py --mode direct`
- 계획, plan: `--mode plan`
- 검토, review: `--mode review`
- 수정, edit: `--mode edit`
- 지휘, 지휘모드, orchestrator: `--mode orchestrator`
- 심층 리서치, 딥 리서치, deep research: `--mode deep-research`
- Pro, 프로: `--mode pro`; run only when the user explicitly requests Pro.
  Without `--attachment`, it is qualified Pro (`pro-devspace`): the `@DevSpace`
  mention plus the mission path, with mission-scoped writes and commands inside
  the exact project root. With `--context-manifest` and `--attachment`, it is
  the evidence route (`pro-attachment-only`): attachment-only Oracle, and never
  select DevSpace or a ChatGPT Project.
- 웹 멀티 GPT, Web Multi-GPT: `chatgpt_oracle_multi.py`; require the explicit
  parallel policy below.
- 로컬 멀티 GPT, Local Multi-GPT: use the local `multi_gpt_start`,
  `multi_gpt_status`, and `multi_gpt_cancel` tools only when the provider exposes
  them. Report unavailable instead of substituting Web Multi-GPT.
- 종합모드, comprehensive: `chatgpt_oracle_comprehensive.py`; preserve its
  authored stage missions and one deterministic local gate.
- DevSpace 경로, roots, 접근 경로: run
  `skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py roots`
  from the installed runtime or repository and use its preview/apply contract.
  On Windows the installed script is
  `$env:USERPROFILE\.codex\skills\chatgpt-workspace-setup\scripts\devspace_tailscale_setup.py`.

Regular web modes always select GPT-5.6 Sol and verify visible Extra High. Pro
selects the account-visible Pro tier and runs only on an explicit user
request. Never silently downgrade either route, and never promote a regular
route to Pro automatically.

## Author and preview

Create one absolute UTF-8 mission file inside the exact project root. For a
regular route, run:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_dispatch.py" --mode plan --project-root C:\project --mission-path C:\project\.ai-bridge\mission.md --manifest-output C:\project\.ai-bridge\oracle.json --chatgpt-project devspace-oracle --dry-run
```

Return the preview and its exact `oracle_manifest_sha256`. For an explicitly
authorized live run, repeat the same command without `--dry-run` and add
`--expected-manifest-sha256 <preview hash>`. Never edit the mission between
preview and live execution.

## Bind a ChatGPT Project

Store only an exact Project URL under a local name:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_projects.py" set devspace-oracle https://chatgpt.com/g/g-p-example/project
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_projects.py" list
```

Use `--chatgpt-project <name>` with the regular dispatcher. For Web Multi, read
the alias with `chatgpt_oracle_projects.py get <name>` and write that returned
exact URL as `chatgpt_project_url` in the multi manifest. The dispatcher resolves
regular aliases before hashing. Never fuzzy-match a title or fall back to a
root/new-chat URL.

Treat Project routing as capability-gated. A ChatGPT Project shares its files,
instructions, and connected sources, but it does not expose the local folder by
itself. If the selected Project chat reports that DevSpace is unavailable,
preserve the exact terminal `TASK_OUTCOME: BLOCKED`; do not resubmit, move the
task to a root chat, attach the mission, or weaken the DevSpace contract.

## Gate parallel web sessions

Use Web Multi only when the user explicitly asks for parallel or Web Multi work.
Include this object in `codex.chatgpt.oracle-multi/v1`:

```json
{
  "parallel_policy": {
    "when": "explicit-user-request",
    "max_total_sessions": 5,
    "max_concurrency": 4
  }
}
```

Count every solver plus the merger in `max_total_sessions`. Keep concurrency at
five or fewer. Report the dry-run `parallel_plan` before requesting the exact
manifest hash authorization. Do not invent a heuristic that spends web sessions
without the user's explicit multi-session request.

## Recover exact sessions

If a submitted run is incomplete or attention-required, use the persisted exact
run directory and `chatgpt_oracle_run.py recover --action harvest`. Never
resubmit, create a replacement conversation, reset workflow identity, or weaken
the project lock.

## Natural-language examples

- `DevSpace Oracle로 이 저장소의 구현 계획을 Extra High로 세워줘.`
- `devspace-oracle 프로젝트 메모리를 사용해서 이 변경을 검토해줘.`
- `웹 멀티 GPT 3개 solver와 merger 1개, 동시성 3으로 비교해줘.`
- `로컬 멀티 GPT로 대안을 병렬 분석해줘.`
- `Pro로 첨부 자료만 검토해줘.`
- `DevSpace 접근 경로를 C:\work로 바꿀 수 있는지 preview해줘.`
