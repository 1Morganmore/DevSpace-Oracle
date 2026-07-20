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
  host-only verification, release, and irreversible boundaries.
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

Every non-Pro role uses the exact drive-scoped CodexPro app with
`app_policy: required`; Pro remains attachment-only with `app_policy:
forbidden`. Complete instructions live in the immutable UTF-8 prompt file.
The composer receives only the fixed prompt-file handoff.

## Comprehensive mode

The compatibility v2 state/recovery topology remains stable, while new stage
prompts use v3 profiles:

1. optional evidence/Deep Research gate;
2. constructive fresh plan;
3. optional app-only Web Multi-GPT solution-space expansion;
4. adversarial fresh review;
5. compact revision delta on `REVISE`;
6. `ExecutionMission` compilation;
7. adaptive orchestrator implementation;
8. deterministic local verification.

`PASS` is only a transition token. It does not turn a plan into immutable truth.
