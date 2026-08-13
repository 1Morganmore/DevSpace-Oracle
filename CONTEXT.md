# DevSpace-Oracle

DevSpace-Oracle delegates development work to web ChatGPT while preserving a usable upstream workflow and explicit authority over local execution, external exposure, and exact-session recovery.

## Language

**Upstream parity**:
The default interpretation that a published upstream workflow and its user-visible outcome are correct unless concrete evidence establishes a harmful defect.
_Avoid_: Blind mirroring, fork contract first

**Authority boundary**:
A transition that grants access to secrets, local execution, external exposure, durable mutation, or session submission and recovery.
_Avoid_: Every validation point, general safety check


**Divergence evidence**:
Evidence sufficient to depart from upstream: a reproduced defect for reversible behavior, or a concrete mechanistic failure scenario at a high-impact authority boundary.
_Avoid_: Architectural preference, theoretical concern

**Control locator**:
UI evidence used only to find a model or effort control; it cannot authorize submission by itself.
_Avoid_: Selection proof, submission proof

**Submission proof**:
The final evidence that the requested model and effort are visibly active before a prompt may be sent.
_Avoid_: Label match, picker hint

**Verified runtime**:
An executable and package set whose identity is bound to the approved installation artifact before native code is loaded.
_Avoid_: Active runtime, PATH runtime, version-matching candidate

**Private checkpoint**:
A one-time human-only interaction surface that receives or displays an Owner secret while the surrounding setup remains one continuous workflow.
_Avoid_: Interactive TTY, manual setup

**Bounded recovery**:
An automatic repair tied to an explicit lifecycle event, limited to the exact failing resource and followed by authoritative readback.
_Avoid_: Reset, best-effort retry

**Registered CLI**:
A user-selected Codex executable whose canonical location, deployment ownership, and required protocol have been verified for Local Multi-GPT.
_Avoid_: CLI command, environment override

**Task-bound runtime proof**:
An authoritative model and reasoning readback bound to one task/thread runtime identity and renewed only when that identity changes.
_Avoid_: Model preference, conversational confirmation

**Lazy activation**:
Installed capability metadata and code that gains execution authority only after the user first invokes it and its runtime dependencies are verified.
_Avoid_: Optional installation, always-on installation

**Registered app**:
The ChatGPT workspace connector whose approved name, public endpoint, and project authorization are bound by one registration receipt.
_Avoid_: App name, picker label

**Exact continuation**:
Recovery that resumes one durably reserved external attempt and never creates a replacement after submission becomes possible or ambiguous.
_Avoid_: Resume, retry, replacement run

**Root registration**:
An additive authorization that preserves existing canonical project roots; removal is a separate explicit revocation.
_Avoid_: Root configuration, allowed-roots replacement

**Temporary workspace**:
Task-scoped disposable storage under the OS temp directory, or under the authoritative repository's gitignored `.codex-tmp` only when a shorter path is required.
_Avoid_: Drive-root temp, scratch root

**Read-only workspace transport**:
A Pro route that can adaptively read one exact user-approved project root through the registered workspace app, but cannot write, edit, invoke a shell, change settings, or mutate external state.
_Avoid_: Pro app access, attachment fallback

**Version-bound pre-submit proof**:
Immutable evidence from one tested Oracle version showing that its exact external attempt disconnected before prompt submission; absent or contradictory evidence preserves the lock.
_Avoid_: No conversation URL, safe retry

**Supported platform**:
An operating system with a complete, representative installation, runtime, recovery, and removal flow that this product verifies and ships; currently Windows 11 only.
_Avoid_: Portable code, CI platform

**Exact-or-stop runtime**:
A mode submits only after proving its exact model and reasoning tier; it never selects a lower tier, alternate model, backend, or transport to keep the workflow moving.
_Avoid_: Best available model, graceful degradation

**Scoped agent installation**:
An explicit post-install merge that owns only receipt-bound agent settings, role files, and one marked global policy block while preserving user model preferences and unmanaged roles.
_Avoid_: Recommended global defaults, installer-managed Codex environment

**Workflow obstruction**:
Local friction or failure that blocks a supported upstream outcome without preventing a concrete harmful scenario.
_Avoid_: Defense in depth, fail-closed behavior

**Safer equivalent**:
A local implementation that preserves the upstream user-visible outcome while replacing a proven unsafe mechanism at an authority boundary.
_Avoid_: Feature rejection, behavioral divergence

## Decisions

- [Preserve upstream outcomes across explicit authority boundaries](docs/adr/0001-upstream-parity-and-authority-boundaries.md)
