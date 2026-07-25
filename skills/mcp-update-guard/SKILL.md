---
name: mcp-update-guard
description: Safely update MCP servers, shared harness helpers, Oracle GPT runners, global skills, plugins, and related automation while preserving local customizations.
---

# MCP update guard

Use this skill for shared/global automation changes. Read the applicable
`AGENTS.md`, identify the authoritative source and installed deployment, and
preserve unrelated local customizations.

## Workflow

1. Classify the exact component and whether the work is an update,
   compatibility repair, policy refresh, or recovery fix.
2. Inspect source Git status and the installed file identity before editing.
   Never overwrite credentials, browser profiles, runtime state, or unrelated
   user changes.
3. For non-trivial GPT automation design or implementation, use the selected
   current GPT workflow only when the user asked for web delegation. Every new
   ChatGPT run uses Oracle:
   - regular modes, Deep Research, comprehensive stages, and Web Multi use
     Oracle plus the manually registered DevSpace app;
   - Pro uses Oracle attachment-only and no app;
   - CodexPro/agbrowse may be used only for exact recovery of an already
     persisted legacy run and never as a fallback.
4. Prefer small compatibility changes over wholesale replacement. Preserve
   local ports, names, roots, tokens, routing, and hooks unless the task
   explicitly changes them.
5. Batch coherent edits, inspect the final diff once, run focused regression
   tests, then broader tests according to blast radius.
6. Synchronize reusable GPT automation changes to the authoritative
   `codexpro-automation` source, install the verified bytes, commit with a
   descriptive message, push public-safe changes, and check CI.

## Safety boundaries

- Do not delete or recreate credential-bearing state during a normal update.
- Do not use resource pressure as authority to block, terminate, downgrade, or
  duplicate user-visible work.
- Do not silently switch Oracle model, reasoning level, transport, or browser
  backend.
- Do not create a new legacy agbrowse/CodexPro run while repairing recovery
  code.
- Stop and report exact dirty files when authoritative persistence, push, or CI
  cannot be completed.

## Report

Report updated components, preserved customizations, focused and broad
verification, installed/source synchronization, commit/push/CI state, rollback
evidence, and any remaining risk.
