# CodexPro Automation Repository Rules

## GPT Automation Change Persistence

- Any durable change to GPT or ChatGPT skills, CodexPro/agbrowse bridges, browser runners, prompt or mode routing, recovery, state, locks, tabs, app registration, Web Multi-GPT, or their tests must include focused verification and a descriptive Git commit before the work is reported complete.
- The installed files under `%USERPROFILE%\.codex` are deployment copies, not the sole source of truth. Synchronize reusable fixes back into this repository instead of leaving them only in the global installation.
- Public-safe reusable changes must be committed to the clean public `main`, pushed, and checked in CI. Never copy credentials, host-only values, sensitive artifacts, or private Git history into this repository.
- Never push a private-history development branch to the public repository. If commit, push, or CI verification is blocked, report the exact dirty files and blocker and do not claim completion.

## Comprehensive-mode ownership

- Every new ChatGPT submission uses Oracle. Regular GPT uses the manually registered DevSpace app; Pro uses Oracle attachment-only and never selects an app.
- New GPT comprehensive workflows use `codex.chatgpt.oracle-comprehensive/v1`. Existing CodexPro/agbrowse comprehensive v1-v4 state remains exact recovery-only.
- The completing web GPT stage authors the next stage's semantic prompt. Local Codex may validate UTF-8, hashes, stage identity, immutable bindings, transport, recovery, and deterministic final tests, but must not rewrite the next prompt or take over expensive exploration/implementation.
- A selected Web Multi advisory uses genuine independent Oracle sessions. Provider generation is limited to at most five concurrent children; larger accepted topologies run in capacity waves without reducing their logical lane count.
- CodexPro and agbrowse are frozen for new work. Their code may be invoked only to recover an exact persisted legacy run, never as an Oracle fallback.
