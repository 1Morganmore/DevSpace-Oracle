# Release checklist

- Run `python scripts/check_portability.py --root .` and the offline lifecycle tests.
- Confirm `install-manifest.json` expands to every shipped `bin` and skill runtime/schema file, including web-multi runtime/upstream and composer additions.
- Confirm MIT copyright is `2026 ventianima-lab` and third-party notices retain the multi-gpt commit/hash attribution.
- Do not vendor agbrowse, Codex, CodexPro, browser binaries, or account data.
- Verify no workflow has `schedule`; CI must use Windows and mocked/offline lifecycle checks.
- Treat agbrowse update as an explicit, reviewed agent action. There is no background checker, scheduled updater, candidate slot, or promotion pointer.
- Exercise `install.ps1`, `doctor.ps1`, `uninstall.ps1`, and `rollback.ps1` with a temporary `CODEX_HOME`; never require Git to bootstrap or verify a release.
- Before a normal install, verify its read-only dependency preflight completes before any managed file mutation. The returned token binds selected version/integrity, prior dependency identity, and observed unlocked state; the subsequent update must reacquire the lock and reject drift. Before an explicit update, confirm no active or uncertain run state exists. The update receipt must preserve the prior npm version/integrity, executable and contract hashes, then capture and validate the reviewed public-command contract before replacing it.
- Future agbrowse versions must be explicit resolved semvers. Pass their exact registry integrity to contract capture/validation, retain 0.1.18 only as the tested baseline, and require the invoking agent/workflow to select the resulting versioned contract explicitly.
- Exercise both file-only install rollback and mocked normal install rollback. Receipt v3 must restore the prior agbrowse package, selected contract bytes, and prior update receipt; the exact inverse must prove registry integrity, installed version, and executable SHA-256 after npm reports success. Dependency drift must fail in preflight before installed files change, and any late inverse failure must report `PARTIAL`.

## Parallel implementation v3

- Confirm all eight v3 schemas parse as draft 2020-12 and retain `additionalProperties: false`, bounded IDs/paths, and registered test IDs rather than free shell strings.
- Verify a missing manifest gate or environment gate creates no lease, parent run, staging repository, exact-unit app, tunnel, or browser send.
- Exercise fixed topology, logical/final overlap, drive/home equality, reparse escape, singleton allowed roots, and sibling isolation tests.
- Verify staging uses `--no-local --no-hardlinks --no-checkout`, has no alternates/reference/shared object store, and detects worker mutation of common Git metadata.
- Verify every dependency and path-conflict edge is unioned into one component, only one unit per component is active, and independent components may continue when another component requires exact-session recovery.
- Verify `send.claim` v2 is immutable and authority-bound; post-send uncertainty must never create a second provider submission.
- Verify exact-unit app identity includes singleton roots, bash off, workspace write, full tool mode, actual listener identity, and separate Cloudflare tunnel identity immediately before send.
- Run `python scripts/run_v3_contract_tests.py`; opt into the live Windows exact-unit integration only in an isolated release environment with test credentials.
- Verify full registered tests and canonical baseline/config/submodule/filesystem revalidation occur before temporary-ref import and ff-only apply. A forced conflict or test failure must leave canonical source unchanged.

# Release lifecycle safety

- `install.ps1` only manages manifest-owned files. CodexPro remains an external runtime acquired by the application bootstrap scripts; `-SkipDependencyInstall` does not change that.
- Retain the unique receipt and backup directory. `uninstall.ps1` is a safe inverse: it removes only unchanged created files and restores only unchanged overwritten files; modified destinations are reported as conflicts.
- Run `doctor.ps1` before an explicit `update.ps1 -AgbrowseVersion <version>`. Updates defer while bridge state is active or uncertain and never terminate it.
