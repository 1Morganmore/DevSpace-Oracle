---
name: chatgpt-deep-research-browser
description: Use for ChatGPT Deep Research web automation, including 딥 리서치, 심층 리서치, deep research, and 리서치 모드, through explicitly selected, contract-validated, unmodified agbrowse.
---

# ChatGPT Deep Research via agbrowse

Use an unmodified agbrowse installation selected by an exact validated contract as the only browser engine. `0.1.18` is the tested default. Do not use the in-app Browser, browser-use, custom Playwright/CDP, or `@chrome` fallback.

## Contract

- `mode_label: Deep Research`.
- `app_policy: required`.
- Deep Research first selects the exact CodexPro app, then selects `@심층 리서치` on the same run-owned composer target. Either missing proof blocks before submission.
- Attach requested local files through agbrowse `--file`.
- Use `--research deep`, fresh `--parallel` session, and the highest supported reasoning contract.
- Supply explicit `--url https://chatgpt.com/` so the fresh parallel target cannot inherit an active `/c/<id>` conversation.
- Do not downgrade to ordinary GPT or Pro.
- Preserve the immutable manifest, prompt, attachments, session ID, target ID, and exact conversation URL.

Before a non-trivial submission, use `chatgpt-question-designer` to make the research question evidence-aware and counterexample-seeking.

## Manifest

Required:

- `project_root`
- `question` or `prompt`
- `mode_label: Deep Research`
- `app_policy: required`
- `chatgpt_app_name`

Optional:

- `files`
- `search_enabled`
- `timeout_seconds`
- `goal`, `constraints`, and `output`

Do not declare another browser backend.

## Execute

```powershell
python <CODEX_HOME>\skills\chatgpt-deep-research-browser\scripts\run_chatgpt_deep_research.py --config <manifest>
```

Dry run:

```powershell
python <CODEX_HOME>\skills\chatgpt-deep-research-browser\scripts\run_chatgpt_deep_research.py --config <manifest> --dry-run
```

## Recovery and completion

- One active or uncertain run per normalized project root.
- Distinct projects may run through separate agbrowse `--parallel` sessions.
- On interruption, poll or recover only the recorded agbrowse session.
- Reject any canonical conversation URL already owned by another run.
- Job identity is the exact canonical conversation URL plus the run-owned `prompt-<run_id>.txt` filename. Never mix another URL/run into it; target/PID/heartbeat/lock/poll state are diagnostic only and cannot override exact terminal web evidence. Recover the persisted canonical URL first and observe its unique exact live target without navigation. Never run `poll --navigate` or a navigating doctor against a run with a known canonical URL. Run `agbrowse web-ai sessions doctor <session> --json` (without `--navigate`) and bounded read-only history adjudication only when the canonical URL is missing.
- Accept only the exact current-run `https://chatgpt.com/c/<id>` URL.
- Never submit a replacement while the first submission is uncertain.
- Keep the submitted research tab open while it is active, streaming, uncertain, or user-stopped-but-unconfirmed. After durable `COMPLETE` and nonempty immutable research capture, automatically close its exact run-owned target only when the canonical URL has one unique live match, no foreign owner exists, and absence is re-verified. Manual/unowned, foreign, and ambiguous tabs remain protected.
- Complete only after terminal research state and nonempty final answer capture are both proven.
