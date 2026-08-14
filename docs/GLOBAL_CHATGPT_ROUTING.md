# Global ChatGPT routing

The supported English names are `GPT`/`direct`, `plan`, `review`, `edit`,
`orchestrator`, `deep research`/`deep-research`, `Web Multi-GPT`,
`comprehensive mode`, and `Pro`. Korean names documented in the main README map
to the same runners; language never selects a different backend.

Use this routing in the Codex global `AGENTS.md` after installing the package.

- New regular ChatGPT work, including direct, plan, review, edit,
  orchestrator, research, comprehensive, and Web Multi-GPT, uses Oracle plus
  the manually registered DevSpace app.
- Regular web work selects `GPT-5.6 Sol` with Oracle `extra-high` and verifies the
  visible `Extra High` tier. It does not silently fall back to High or another
  model, and it never promotes to Pro automatically.
- The regular composer contains only `@DevSpace` and an absolute UTF-8 mission
  path. It does not attach the task body and does not inspect or mutate ChatGPT
  app settings per question.
- Pro also uses Oracle and is selected only on an explicit user request.
  Qualified Pro uses the `pro-devspace` transport: it mentions DevSpace, writes
  mission-directed files, and runs the mission's commands inside the exact
  project root. Explicit immutable-evidence Pro uses the `pro-attachment-only`
  transport with hash-frozen attachments and no app.
- Comprehensive stages author the next semantic mission and a bound hash
  receipt. Local Codex owns transport, immutable identity, host safety, and one
  final deterministic gate rather than rewriting web output. A comprehensive
  workflow schedules a Pro stage only when its manifest sets `allow_pro: true`,
  a value the host writes only after an explicit user request.
- An optional Oracle Pro stage returns one strict identity-bound JSON envelope;
  the host materializes its output and next-mission strings byte-for-byte.
  Qualified `pro-devspace` stages inherit the DevSpace exact-root and
  outcome-contract rules; `pro-attachment-only` stages keep their forced
  legacy `not_applicable` classification.
- Genuine Web Multi-GPT uses distinct Oracle sessions. Windows lanes use
  independent throwaway copies of the signed-in Oracle profile, run in waves
  of at most five, and hand compact files to one merger.
- Local Multi-GPT is a self-installed, PC-local read-only lane. It runs
  parallel-reasoning jobs on this machine through the local `multi_gpt` MCP
  tools and is not a web submission or an Oracle/DevSpace route.

## Standalone Pro versus comprehensive

`chatgpt-pro-browser` is the visible standalone Pro skill. It submits one
explicit immutable-evidence Oracle Pro session (`pro-attachment-only`), saves
the durable result, returns it to the calling Codex task, and stops. Qualified
Pro (`pro-devspace`) is the companion write route: it uses the `@DevSpace`
mention and the absolute mission path and performs mission-scoped writes and
commands inside the exact project root. Neither route starts implementation or
another web stage automatically.

`chatgpt-pro-plan-handoff` owns comprehensive mode. Only that staged runner may
place an optional Pro decision between plan and review and continue afterward
to implementation and gates. Natural-language `Pro` or `GPT Pro` requests route
to the standalone skill; explicit comprehensive-mode requests route to the
handoff skill.

## Orchestrator versus comprehensive

These two are often confused because both let the web GPT own implementation.
They differ in structure, not in ambition.

| | `orchestrator` (지휘) | comprehensive (종합) |
|---|---|---|
| Runner | `chatgpt_oracle_dispatch.py --mode orchestrator` | `chatgpt_oracle_comprehensive.py` |
| Web submissions | one | several, one per stage |
| Stage receipts | none | hash-bound per workflow/stage/attempt/input |
| Independent review | no | yes, review repairs and finalizes the plan |
| Pro / Web Multi stage | not available | selectable |
| Completion | the answer itself | final web PASS plus zero-exit local gate |
| Recovery unit | one run | workflow plus stage identity |

Comprehensive mode runs orchestrator-equivalent work as its implementation
stage, so it contains that mode rather than competing with it.

Pick `orchestrator` when the goal and approach are settled and one authorized
pass should finish the work at the lowest local and web cost. Pick comprehensive
when the plan needs independent review, when Pro or Web Multi must participate,
or when completion must be proven by a deterministic local gate. Do not hand-chain
`orchestrator` submissions to imitate staging; same-project submissions stay
serialized and the workflow engine owns stage identity and recovery.

The package does not overwrite an existing user `AGENTS.md` automatically.
Apply this block deliberately so unrelated personal rules are preserved.
