# CodexPro Automation Repository Rules

## GPT Automation Change Persistence

- Any durable change to GPT or ChatGPT skills, browser runners, prompt or mode routing, recovery, state, locks, tabs, app registration, Web Multi-GPT, or their tests must include focused verification and a descriptive Git commit before the work is reported complete.
- The installed files under `%USERPROFILE%\.codex` are deployment copies, not the sole source of truth. Synchronize reusable fixes back into this repository instead of leaving them only in the global installation.
- Public-safe reusable changes must be committed to the clean public `main`, pushed, and checked in CI. Never copy credentials, host-only values, sensitive artifacts, or private Git history into this repository.
- Never push a private-history development branch to the public repository. If commit, push, or CI verification is blocked, report the exact dirty files and blocker and do not claim completion.

## Comprehensive-mode ownership

- Every new ChatGPT submission uses Oracle. Regular GPT uses the manually registered DevSpace app. Qualified Pro uses the `pro-devspace` transport: the `@DevSpace` mention plus the absolute mission path, with mission-scoped file writes and command execution confined to the exact project root. Explicit immutable-evidence Pro uses the `pro-attachment-only` transport and never selects an app. Pro runs only on an explicit user request; no route promotes itself to Pro automatically.
- GPT comprehensive workflows use `codex.chatgpt.oracle-comprehensive/v1`. Retired backend state is host-only historical data and must not be submitted, recovered, or automatically deleted.
- The completing web GPT stage authors the next stage's semantic prompt. Local Codex may validate UTF-8, hashes, stage identity, immutable bindings, transport, recovery, and deterministic final tests, but must not rewrite the next prompt or take over expensive exploration/implementation.
- A selected Web Multi advisory uses genuine independent Oracle sessions. Provider generation is limited to at most five concurrent children; larger accepted topologies run in capacity waves without reducing their logical lane count.
- Comprehensive review owns plan repair and finalization. It fixes every locally resolvable defect inline, writes the corrected final plan and implementation mission, then returns PASS or PASS_WITH_NOTES. New work never loops review back to plan; legacy REVISE is terminal compatibility only, and FAIL requires a concrete external blocker.
- Every regular Oracle stage is bound to one exact project root and one exact mission path. DevSpace may retry that same root once after listing registered workspaces, but must never substitute a parent, child, similarly named, active workspace, or shell boundary workaround.
- Transport and runner recovery retain the exact workflow/stage identity. They must not create a replacement workflow or reset the semantic revision budget.
- Oracle is the only GPT browser backend. Retired backend code and routing are absent and must not be recreated as a fallback.
- Every new Oracle run must use a throwaway copy of the manually signed-in profile and an Oracle-owned hidden window. Never share the manual-login Chrome process across concurrent projects.
- Exact-slug recovery may relaunch a bounded recovery browser from the persisted profile seed and open only the recorded conversation URL. It must never restart, resubmit, or create a replacement conversation.
- A nonzero Oracle exit after submission, including a browser response timeout, is attention-required rather than web-terminal failure. It retains exact-session ownership and allows only exact-slug live/harvest recovery.
- Exact session authority is monotonic. `terminal_observed` cannot regress to `live`; observer disagreement remains attention-required under the same project lock until a later exact terminal harvest produces fresh nonempty durable output.
- Exact-run recovery writers serialize on a run-scoped mutex and never wait for the project submission mutex. Unresolved exact sessions still block every fresh submission, and a late original observer must not overwrite terminal harvested state.
- Persisted `blocked` and `not_executed` task outcomes outrank lifecycle `complete` in diagnosis. Malformed or ambiguous outcome markers and persisted `unknown` remain unresolved; OAuth token request failure with 503 is reported as a distinct registered-app signature.
- The Windows DevSpace watchdog uses the one existing HKCU Run value `DevSpace MCP Server`, runs as a hidden singleton, and rereads live DevSpace config every health cycle. It may repair only the exact managed service and exact Funnel mapping; it never mutates Owner credentials, OAuth clients/tokens, ChatGPT registration/settings, or allowed roots.
