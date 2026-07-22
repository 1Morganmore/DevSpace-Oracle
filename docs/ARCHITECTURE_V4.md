# Comprehensive workflow v4: web-native relay

V4 minimizes local Codex reasoning across a long comprehensive workflow. The web
stage that understands the current artifact authors the semantic prompt for the
next web stage. The host validates and transports it; the host does not summarize
or rewrite it.

## Stage flow

1. Optional app-backed Deep Research produces an immutable descriptor.
2. Planner returns the plan and `stage-relay/v1`.
3. If the advisory gate runs, the Planner relay starts genuine app-only Web
   Multi-GPT and separately carries the later review prompt.
4. Reviewer returns exactly one branch:
   - `REVISE`: next Planner prompt plus compact revision delta.
   - `PASS`: Orchestrator prompt plus implementation mission.
5. Orchestrator owns workspace exploration, edits, tests, and bounded adaptation.
6. Local Codex performs deterministic final verification and release work only.

## Binding without circular hashes

The relay contains semantic prompt bytes and only hashes known before its source
stage. The host stores those bytes immutably and creates a separate deterministic
binding wrapper after downstream artifacts exist. The wrapper adds their exact
paths and SHA-256 values plus the required output envelope. It cannot change the
semantic instructions.

V4 rejects unknown relay keys, wrong stage/profile/binding values, malformed
UTF-8, replacement characters, `???`, overwrites, and cross-workflow reuse.

## Version and rollback boundary

- V1 and v2: matching persisted recovery only; never converted into v4. A
  pre-v4 state is first validated against its immutable snapshot, archive, and
  stage checkpoints, then upgraded once with the current manifest schema and
  exact SHA-256. Every later recovery requires that identity to match.
- V3: separate parallel-implementation manifest and runner.
- V4: all new comprehensive workflows.

Rollback stops creation of new v4 workflows while active v4 runs continue with
their pinned v4 artifacts until recovery/drain completes. No version is silently
downgraded or migrated.

## Web Multi capacity

Every accepted lane remains an independent ChatGPT session, target, and canonical
URL. Provider-generation concurrency is at most five. Six through ten lanes run
as waves of at most five; each barrier covers only children actually submitted in
that wave. Solver/refiner and merger/refiner pairings and logical result order are
preserved.
