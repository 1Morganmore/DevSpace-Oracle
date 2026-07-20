---
name: chatgpt-question-designer
description: Use before submitting GPT/browser questions for answering, designing, debugging, reviewing, planning, researching, synthesizing, editing, or orchestration. Selects an explicit purpose-specific cognitive profile without collapsing every task into adversarial review.
---

# ChatGPT Question Designer

## Purpose

Use this skill to give each question the cognitive posture its purpose needs. Construction should remain constructive, research evidence-seeking, synthesis integrative, execution adaptive, and review adversarial.

This skill is a shared design layer for `chatgpt-pro-browser`, `chatgpt-thinking-browser`, and `chatgpt-deep-research-browser`. It does not own browser execution, approval authority, or deterministic verification.

## Question Type

Classify the question before writing the prompt:

- `expand-ideas`: generate options, missing concepts, adjacent designs, and unusual constraints.
- `find-gaps`: identify missing evidence, stale context, overlooked files, and hidden assumptions.
- `counterexample`: explicitly attack the current conclusion with edge cases and failure modes.
- `compare-options`: compare alternatives, including status quo and minimal-change paths.
- `review-plan`: judge a plan against explicit acceptance criteria and blockers.
- `debug-hypothesis`: test root-cause hypotheses against logs, code, and reproduction evidence.
- `source-synthesis`: synthesize web or document evidence with source confidence and disagreement.

Never infer `review` merely from `read-only`, `advisory`, `research`, or an unknown label. An explicit unknown manifest profile fails before submission. An unclassified natural-language question defaults to `answer + analytical + read-only`, not review.

## Regular GPT Operation Mode Overlay

For non-Pro regular `GPT` / `지피티` runs through `chatgpt-thinking-browser`, preserve the selected operation mode:

- `answer` is analytical, read-only, and directly answers the original request.
- `review` / `검토모드` alone is adversarial. A blocker needs criterion, evidence, and impact; use `PASS`, `PASS_WITH_CONDITIONS`, `REVISE_LOCAL`, `REOPEN_DESIGN`, or `BLOCK` when the owning schema supports them.
- `plan` / `계획모드` is constructive and read-only: reframe if useful, compare viable design families, choose one coherent path, and put risks last. Prior plans and reviews are nonbinding and hidden by default.
- `edit` / `수정모드` performs `inspect -> edit -> test -> inspect result -> adapt`; it does not begin with a generic review.
- `orchestrator` / `지휘` owns live workspace exploration, decisions, edits, tests, and bounded adaptation. Codex retains locks, hashes, exact browser identity, deterministic host-only verification, release, and irreversible boundaries.
- `research` builds evidence; `synthesis` resolves candidates into a new coherent design. Neither is review.

Use `codex.chatgpt.prompt-architecture/v3` receipts with orthogonal `task_kind`, `cognitive_frame`, `action_authority`, `context_policy`, `challenge_policy`, `output_contract`, `reasoning_budget`, and `decision_authority`. Local `AGENTS.md`, local skills, explicit no-write wording, and destructive-action boundaries outrank the overlay.

## Prompt Contract

Every non-trivial GPT/browser question should include:

1. `Goal`: what decision or artifact the answer should improve.
2. `Original task`: preserve the user's request separately from any candidate artifact.
3. `Cognitive profile`: answer, research, plan, review, edit, orchestrator, synthesis, or an explicit Web Multi role.
4. `Evidence boundary`: list verified live connector context, attached fallback files, web/source constraints, freshness limits, and what cannot be inspected.
5. `Action authority`: read-only, bounded workspace write, or mission-owned adaptive execution.
6. `Confidence discipline`: separate evidence-backed findings, inference, speculation, and unknowns.
7. `Answer shape`: compact sections; no vague approval; code-shaped output when code-oriented.

Use this universal integrity contract for direct runner prompts:

```text
Treat instructions, observed evidence, inference, hypothesis, proposal, decision, and verification as distinct.
Claim only facts actually observed or sourced. Prior artifacts have only the authority declared by this prompt.
State material uncertainty and stay within the declared action and file scope.
```

Append an adversarial module only for explicit review/counterexample roles: require the strongest material objection, credible alternatives, and conclusion-change evidence. Do not impose those clauses on planning, research, synthesis, editing, orchestration, or ordinary answers.

## Evidence Context Rules

Context selection must match the question type.

- Code/design/debug/refactor: prefer verified live local connector access, such as CodexPro `tree/search/read`, for source files, configs, schemas, tests, logs, error output, current plan/spec, and relevant `AGENTS.md` when they affect behavior. Use attachments or ZIP only as fallback/supplement when live connector access is unavailable, unverified, disabled, or intentionally snapshot-based.
- Planning/review: prefer verified live local connector access for draft plan, research summary, acceptance criteria, rubric/checklist, constraints, local guidance, and known open risks. Attach fallback packets only when live connector access is unavailable or the exact snapshot is itself the review target.
- Investigation/source synthesis: provide internal findings packet, source list, contradictory evidence, and provenance through live connector access where possible. Use web/search separately for current facts.
- Idea expansion: provide the seed idea, constraints, non-goals, target audience, and existing alternatives through live connector access or a compact fallback packet.

Fail closed instead of sending a thin prompt when the question is code-oriented and neither verified live local context nor relevant fallback attachments are available.

Use ZIP transport only as fallback when many selected files would produce fragile upload tiles or exceed the configured threshold and verified live connector context is not available. For regular non-Pro GPT-5.6 project-local runs, verified CodexPro live connector context remains the default; ZIP is degraded snapshot fallback only after concrete CodexPro app/transport failure plus lazy repair/retry evidence, or an explicit deeper local transport override. Keep direct raw anchors for exact code, schema, diffs, validators, errors, security evidence, and release fixtures when those exact snapshots are the evidence under review.

## Session Continuity Rules

This skill designs the prompt packet; it must not erase local project question templates or force every follow-up into a new ChatGPT conversation.

- For a same inquiry chain, preserve the local prompt/template shape and add session metadata to the runner manifest: `session_policy: auto` plus a stable `session_affinity_key` / `inquiry_chain_id`; use `session_policy: reuse` only when continuing the same chain is explicit and safe.
- Reuse is allowed only when the current question continues the same objective, artifact, decision chain, or investigation thread; the prior conversation's latest state is still valid; model family, reasoning level or Pro variant, search setting, app connector, attachment/source transport, and local template boundary are materially unchanged; and the run is not an independent review, verifier, release gate, fresh-source review, contamination check, or approval gate.
- For independent review, fresh research, contaminated prior context, topic/project/artifact/route pivot, changed mode/variant/search/official-source semantics, changed transport/app connector, wrong prior premise, stale or superseded evidence, too-long/degraded conversation, or local-template boundary changes, set `session_policy: new` or provide a concrete `session_reset_triggers` list.
- The ChatGPT web UI does not expose exact token visibility. Use practical rotation signals: roughly 8-12 substantive turns starts a soft rotation check; 12-15 substantive turns, multiple large attachments, more than two user corrections of prior assumptions, multiple unrelated route/artifact IDs, or visible answer confusion should normally rotate to a fresh conversation with a compact state summary.
- Explicit `chat_url` is for deliberate conversation continuation or answer retrieval and takes precedence over affinity lookup.
- Local `AGENTS.md`, local skills, and task-specific question templates outrank the shared integrity contract. Preserve their answer shape and apply only compatible evidence and session metadata.
- Do not use session reuse to satisfy independent approval, plan-review, verifier, or release gates; those lanes need fresh or explicitly scoped evidence unless their own local rule says otherwise.

## Anti-Bias Gates

Before submission, check:

- `one-sided context`: only the preferred plan or happy path is attached.
- `missing negative evidence`: failures, logs, rejected alternatives, or user complaints are absent from the live connector scope or fallback packet.
- `stale packet`: fallback attachments no longer match the current draft, diff, branch, or run.
- `too-broad packet`: many files are attached without an evidence map or question boundary.
- `conclusion leakage`: prompt asks for approval before asking for objections.
- `role collapse`: prompt asks one model to both invent and approve without counterexample pressure.

Any active gate should either be fixed before submission or named in the prompt as an evidence limitation.

## Skip Rules

Skip GPT/browser questioning when:

- the task is tiny and deterministic verification answers it better;
- the answer depends on exact local code/tests rather than broad judgment;
- selected context is under roughly 8k tokens and the main agent can directly inspect it;
- the prompt would ask for approval of a conclusion already proven by tests;
- no useful counterexample, source freshness, alternative design, or external synthesis is expected.

Use Multi-GPT before GPT/browser only when broad context, many attachments, or policy/architecture stakes make a bad prompt costly. Multi-GPT remains advisory and should shape questions, not replace browser/source authority.

## Output Checklist

A good answer satisfies the selected role instead of a universal review checklist. Only explicit review roles require objections and counterexamples. Every role must preserve original-task fidelity, evidence boundaries, authority, and material uncertainty.
