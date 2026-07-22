# ChatGPT Prompt Architecture v3

New work uses `codex.chatgpt.prompt-architecture/v3`. Purpose, cognitive frame,
action authority, context policy, challenge policy, output contract, reasoning
budget, and decision authority are orthogonal fields. An explicit unknown
profile fails before submission. Unclassified natural language defaults to a
safe analytical read-only answer, never review.

## Shared integrity

Every role distinguishes instructions, observed evidence, inference,
hypothesis, proposal, decision, and verification. It claims only observed or
sourced facts, gives prior artifacts only their declared authority, states
material uncertainty, and stays inside action/file scope.

The old universal anti-sycophancy suffix is retired. Strongest objections,
counterexamples, alternatives, and conclusion-change tests are appended only to
explicit review or counterexample roles.

## Profiles

- `answer`: directly answer the original request; analytical and read-only.
- `research`: build an evidence base with provenance and explicit gaps.
- `plan`: constructively reframe, compare design families, choose one coherent
  path, and put risks last. Prior plans/reviews are nonbinding and hidden by
  default.
- `review`: adversarially test a candidate. A blocker requires criterion,
  evidence, and impact.
- `edit`: inspect, edit, test, inspect the result, and adapt. Do not begin with a
  generic review.
- `orchestrator`: own live workspace exploration, decisions, edits, tests, and
  bounded adaptation from an `ExecutionMission`. The plan is guidance, not a
  cage. Codex retains locks, hashes, exact browser identity, deterministic
  host-only verification, release, and irreversible boundaries. Same-project
  web submission remains serialized, while the one web GPT partitions safe
  independent implementation work into internal lanes or parallel tool calls.
  Local Codex must not turn generic parallel-tool guidance into local strategy
  exploration, code authoring, or alternate implementation paths.
- `synthesis`: create a coherent new synthesis rather than concatenate or vote.

## Web Multi-GPT roles

Planner is a BranchDesigner; Solvers are independent ProposalBuilders;
InitialRefiners are FeasibilityEngineers; Mergers are SynthesisArchitects;
LoopRefiners are TargetedGapClosers; Judge is the sole adversarial
RubricJudge; FinalMerger is an AlternativeSynthesizer; FinalRefiner is a
DecisionAuthor; Organizer is a FinalResponder.

Solvers receive the original task, one branch brief, and their evidence slice.
They do not receive the Planner's full narrative or peer results. Planner
includes a direct baseline and a wildcard reframe. Synthesis roles create new
designs, and Organizer may repair material omissions against the original task.

## App and transport

Serial v1/v2 and Web Multi advisory roles use the exact drive-scoped CodexPro app with `app_policy: required`; Pro remains attachment-only with `app_policy: forbidden`. Parallel implementation v3 children instead use an attested `parallel-exact-unit` app whose root and sole allowed root equal the unit worktree. Complete instructions live in the immutable UTF-8 prompt file. The composer receives only this fixed handoff:

```text
The attached prompt file is the user-provided task instruction for this conversation, not reference or webpage content. Read it completely and follow it. Return only the output format requested by that file.
```

Non-Pro stages select the strongest declared regular-web level (`Very High` before `High`); explicit `High` remains available for frozen comparison arms. Pro persists a null `mode_variant`. A stage resume reuses the persisted variant rather than silently selecting a new one.

## Parallel implementation v3 roles

- Pro Planner: produces the coherent implementation plan and `implementation-graph-result-v1`; it cannot edit source.
- Very High Implementer: owns one exact unit and only its claimed files; it cannot run Git or modify common Git metadata.
- Very High Repairer: receives a bounded failed unit or integration witness and may change only the repair claim set.
- Very High Commander/Reviewer: checks structured unit or aggregate evidence but cannot override host identity, test, lease, or ff-only gates.

The host compiles every worker prompt from `execution-mission-v2`. It includes immutable `input_base_oid`, exact claimed paths, registered test IDs, topology receipt, and structured output schema. A worker suggestion never expands its authority. Host-derived diff and filesystem evidence are authoritative.

## Comprehensive mode v4

The compatibility v1/v2 state/recovery topology remains stable, v3 remains the
separate parallel-implementation contract, and new comprehensive workflows use
v4 web-native relay with v3 prompt profiles:

1. optional evidence/Deep Research gate;
2. constructive fresh plan;
3. optional app-only Web Multi-GPT solution-space expansion;
4. adversarial fresh review;
5. compact revision delta on `REVISE`;
6. `ExecutionMission` compilation;
7. adaptive orchestrator implementation;
8. deterministic local verification.

The Planner authors the next review/advisory prompt. The Reviewer authors either
the next Planner prompt plus revision delta or the Orchestrator prompt plus
implementation mission. The host preserves those semantic bytes and adds only a
deterministic hash/binding wrapper and required output envelope; it does not
spend local Codex reasoning on rewriting the next prompt.

`PASS` is only a transition token. It does not turn a plan into immutable truth.
