# Release checklist

- Confirm package metadata remains `1.8.0` with Node.js `>=24 <27`.
- Confirm new runs pin Oracle `0.17.1`; exact recovery accepts only `0.16.1`, `0.17.0`, and `0.17.1`.
- Confirm DevSpace setup pins `1.0.6` and both npm integrities match `install-manifest.json`.
- Confirm regular routes use `GPT-5.6 Sol` and `extra-high` with the visible `Power 4 of 5` proof; Pro remains attachment-only `gpt-5.6-sol` and `heavy` with the `Power 5 of 5` proof.
- Confirm no route enters Web Multi automatically or uses another backend as a fallback.
- Confirm WAL v3 and receipt v4 removal migration, crash recovery, rollback, and uninstall in a temporary `CODEX_HOME`.
- Confirm modified, symlinked, unowned, active-run, and ambiguous-receipt inputs fail before mutation and preserve user bytes.
- Confirm removed paths have zero imports, callers, manifest/package/workflow entries, fixture/schema references, and tests.
- Run `python scripts/check_portability.py --root .`, `python scripts/check_skill_metadata.py --root .`, `python scripts/run_fast_gate.py --enforce-budget`, and `python scripts/run_golden_path_smoke.py`.
- Run `python scripts/run_release_contract_tests.py --focused` and `python scripts/run_release_contract_tests.py --full`.
- Run `npm pack --dry-run` and inspect the actual package tarball inventory.
- Confirm third-party notices, `SECURITY.md`, `docs/VS_UPSTREAM.md`, package inventory, and install manifest agree.
- Keep `.github/workflows/upstream-drift.yml` read-only and non-required. It may use `schedule` and `workflow_dispatch`, but must never mutate source/manifest, promote a patch, commit, push, open a PR/issue, or update a package.
- Do not push, dispatch a workflow, submit a live ChatGPT run, modify the deployed install, or remove user state without the required separate approval.

`install.ps1`, `rollback.ps1`, and `uninstall.ps1` manage only receipt-owned regular non-symlink files. Preserve the receipt, WAL, and durable backups until the operator accepts the lifecycle result.
