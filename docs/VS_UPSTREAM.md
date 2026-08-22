# Differences from upstream

This file binds package compatibility to exact public artifacts. The local fork ref is the commit containing this document; obtain its immutable value with `git rev-parse HEAD` and use the commit reported by the release evidence, not a working-tree snapshot.

## A. Fork versus parent project

- Local ref: the exact commit containing this document in `1Morganmore/DevSpace-Oracle`.
- Parent main observed and audited on 2026-08-20: `ventianima-lab/codex-web-gpt-automation@731aec0a2d76c3c1c02815344accd118c177daff` (parent 1.15.1). Previous audited head: `66c23f170661e36bfc9d0f970c01dd4234863d6c` (2026-08-14).
- Last direct integration baseline: `9542abeef6aa544f4ee6af03bab61cef3474f9e4`.
- Merge-base and ahead/behind counts are release-time evidence from the local refs reported by `scripts/check_upstream.py`; this source document does not freeze a working-tree-dependent count.
- The exact release tree difference is reported externally after commit because a commit cannot contain its own final hash.

The fork keeps Oracle-only browser submission, DevSpace workspace transport, exact-slug recovery, per-project locks, receipt-owned installation, Windows process/profile isolation, and bounded Web Multi sessions. Parent sync must preserve monotonic terminal authority, exact package/hash gating, host-only state, mutex/schema/receipt identities, and the absence of automatic Web Multi or alternate-backend fallback.

The nine commits after `9542abee` were reviewed individually. The selector-proof changes in `51675967` and `d8f8fac1` are self-developed on the active fork Power patch: only visible picker candidates count, all three independent Pro signals are required, and two consecutive observations are required before an already-selected result. The existing final diagnostic-race fallback remains intact. The recovered-lane and merger-resume changes in `39f750f4` and `916aeffb` are deferred because they require adaptation to this fork's manifest hashes, parent lock, terminal seal, and monotonic exact-session authority. The persisted `allowedRoots` doctor readback from `69ad58c2` is selected. The `075b3719` read-only-DevSpace Pro default remains rejected: it was audited against the then attachment-only Pro contract, and the parent 1.14.0 adoption below creates a DevSpace-based Pro *write* route (`pro-devspace`), not a read-only one. The later `6d24cce` Funnel recovery is self-developed rather than cherry-picked: this fork's explicit `ensure` command waits for the exact local `/healthz` identity, creates only an absent exact mapping, refuses conflicts, reads back any change, and proves the public `/healthz` identity. It does not accept permissive `/mcp` status codes or claim automatic login recovery without a supervised startup path. The `ae3b3caf` OAuth fix is also self-developed on the pinned 1.0.6 launch path: all four managed `serve` entries advertise `devspace,offline_access`, while ChatGPT reconnect or recreation remains a manual settings action. The `77bb79af` task-outcome change is not applied: it governs the `pro-devspace-readonly` transport this fork does not have and did not create. This fork's `pro-devspace` inherits the `devspace` outcome policy (the caller chooses `legacy` or `v1`; dispatch uses `v1` and comprehensive uses `legacy` like every other stage), while `pro-attachment-only` keeps its forced legacy `not_applicable` classification.

The 30 commits between `77bb79af` and `54a6b6e` were audited on 2026-08-13. Selected: the manual-login pre-submit proof (`b9cdf382` releases the proven Oracle pre-submit profile locks) and the bounded reference-footer classification (`07ca3394`). Deferred until receipt-bound absolute runtimes, minimal-authority environment, live root/runtime attestation, and a hard private first-init boundary exist: the explicit post-register stabilization with exact slot recycle (`63a61fad` settles DevSpace only after ChatGPT app registration and `8ea49f2b` recovers the stale Funnel onto the exact slot after reconnect), the auth-preserving existing-DevSpace-config persistence and CDP bootstrap fragments of `fc454da1`, and the `MULTI_GPT_CODEX_CLI_PATH`/`CODEX_CLI_PATH` runtime override fragment of `e8105e6d`. Also deferred: the drive-root persistence hygiene of `94f7dc70`, the fail-closed ultra-economy mode (`26595b76`), and the onboarding/global-agent installs and the Local Multi-GPT optional-install flip (`0f4dcb36`, `f75c9cc6`, `e8105e6d`) — Local Multi remains an always-installed manifest member. Rejected: the POSIX command-line launcher identity of `51d2e415` as-is (including its DevSpace 1.0.4/hash-downgrade fragment) — a command-line-shaped launcher identity must not carry launch authority; Oracle/DevSpace downgrade acceptance (`4c3938a9`), configurable workspace app names (`b7b1766a`), `pro-devspace-readonly` (`eb423a53`), and the macOS/Cloudflare/rebrand/legacy-installer work (`9da21bbc`, `6af6d17e`, `c78302c7`, `84f3e027`) which targets platforms and packaging this fork does not ship.

The single commit after `54a6b6e`, `9bd6843e` ("feat: complete safe first-install onboarding"), was audited on 2026-08-13. Selected: the strict UTF-8 Tailscale status decoding — `encoding="utf-8"` with `errors="strict"` on the `tailscale status` and hostname-discovery captures — which only controls deterministic JSON decoding of the Tailscale status fragment and adds no launch authority. Deferred: the native-runtime probe (`--check-native-runtime`/`DEVSPACE_NATIVE_BINDING_UNAVAILABLE`) until it loads better-sqlite3 through a receipt-bound Node runtime and fully verified package extraction instead of ambient `shutil.which("node")` resolution against candidate package roots. Rejected: the TTY-as-human-boundary for first init (`DEVSPACE_FIRST_INIT_REQUIRES_INTERACTIVE_TTY`) because an agent-owned PTY can still capture the Owner password that the follow-on review prints; the owner-password review concept remains human-only/manual, run by the person in their own terminal (`owner-password`), and is not adopted as an automation step. Rejected/N-A: the ultra-economy one-time conversational handshake (it replaces the exact runtime-identity proof with an unverifiable conversation acknowledgment), the configurable app name (`--app-name` relaxing the fixed `codex` identity), and upstream versioning (the install-manifest/package `1.12.1` → `1.13.0` bump is upstream's own versioning and is not adopted).

The parent head was advanced and re-audited on 2026-08-14 at
`66c23f17` (parent 1.14.0), and its explicit Pro read/write policy was
adopted as release 1.9.0:

(i) The explicit Pro read/write policy is adopted. Regular web work stays on the
supported top-tier `extra-high` (`Power 4 of 5`) and never promotes to Pro
automatically; Pro has a limited daily allowance and is selected only on an
explicit user request. Qualified Pro runs use the new `pro-devspace` transport
with mission-scoped writes and commands inside the exact project root, and the
standard comprehensive workflow schedules a Pro stage only when its manifest
sets `allow_pro: true` (written only after an explicit user request).

(ii) This fork has never had a `pro-devspace-readonly` transport and does not
create one. This fork's legacy Pro history is `pro-attachment-only`; its
meaning and exact recovery contract are preserved as the explicit
immutable-evidence route, and it is never an automatic fallback.

(iii) `task_outcome_contract` intentionally differs from the parent: on this
fork `pro-devspace` inherits the same policy as `devspace` — the caller chooses
`legacy` or `v1` (dispatch uses `v1`; comprehensive uses `legacy` like every
other stage) — because our comprehensive workflow binds every stage to the
legacy contract. Only `pro-attachment-only` keeps the forced legacy
`not_applicable` classification.

(iv) Parent 1.13.1's long-running state-checkpoint change is not mirrored: the
fork's existing structure — one overall `--browser-timeout` answer budget, the
host watchdog's `attention_required` outcome, and monotonic exact-session
ownership — already delivers the same user-visible result.

The 13 commits from `392c3e6f` through `731aec0a` were audited individually on
2026-08-20. Release 1.10.0 makes these decisions:

(v) Selected and adapted: the DevSpace resident-repair intent from `a8ffa570`
is implemented with this fork's stricter Windows boundary. One existing HKCU
Run value, `DevSpace MCP Server`, launches a hidden single-instance watchdog.
Each cycle rereads live `~/.devspace/config.json`; it repairs only the pinned
DevSpace service with exact `{ok:true,name:"devspace"}` identity and the exact
Funnel mapping. It never changes Owner credentials, OAuth clients/tokens,
ChatGPT registration/settings, or allowed roots, and conflicts fail closed.

(vi) Selected and adapted: the terminal-recovery and OAuth-diagnosis pieces of
`b313f0d2`, the consumed-refresh replay from `ffeca2f4`, and exact-run recovery
serialization from `9814d6aa`. Persisted nonempty `blocked` and
`not_executed` outcomes are classified before lifecycle `complete`; unresolved
same-artifact `OAuth token request failed` plus `503` evidence has a distinct
registered-app signature. The DevSpace 1.0.7 replay is in-memory, limited to
30 seconds and 32 entries, and requires the same client, scope, and resource;
revoke, expiry, and mismatch remain fail-closed without credential or database
schema mutation.

(vii) Exact recovery now has one writer mutex derived from the exact run
directory and never waits for the project submission mutex. This does not open
a second submission path: unresolved project ownership continues to block
fresh work. The original observer rereads durable state after its wait and
cannot overwrite a terminal harvested result. The fork retains
`host_watchdog`; it does not introduce the parent's `browser_observer`, an
automatic retry, or a replacement submission.

(viii) Selected only as reporting semantics: the publication distinctions in
`646e01ea` and `6a4ced2b` inform the read-only upstream checker. npm latest
version/integrity/`gitHead`, the annotated source-tag object and peeled commit
plus signature status, GitHub `releases/latest`, and the default-branch head
are separate signals. A GitHub Release lag does not relabel itself as a tag or
replace exact npm dist authority. The parent's tag-push publication workflow
is not copied; release synchronization, commit, push, and CI remain this
fork's own release gate.

(ix) Preserved rather than broadened: the settlement changes in `392c3e6f`,
`ff00d5ef`, and `731aec0a` do not grant new Oracle 0.17.1 attachment-timeout or
model-selector settlement authority in this release. Existing local
version/hash/evidence-bound no-submission contracts remain authoritative, and
ambiguous output stays unresolved. The installer WAL change in `fc664837` and
the parent-only `1148aa59` release metadata are not donor changes for this
release.

(x) Deliberate non-adoptions: `324f1f33`'s extra exact-root composer prose is
not adopted. A regular run continues to send the bare
`@DevSpace <absolute mission path>`; the immutable mission and manifest carry
the exact project root. `54f1e2f2`'s isolated-worktree Ultra GPT mode is also
deferred. No Ultra GPT route, skill, manifest profile, or startup entry is
added in 1.10.0, and Pro remains explicit-only.

The parent head was advanced and re-audited on 2026-08-22 at
`bcf78feffb39970991adc64d3ff053338fdc2f7f` (parent 1.16.1). All 16 commits in
`731aec0a`..`bcf78fef` were reviewed individually against the fork invariants
in AGENTS.md and this document. Verdicts: 2 ADOPT, 8 ADAPT (separate fork
implementation), 6 REJECT. Decisions are recorded here for the next release;
no donor code was integrated in this audit.

(i) ADOPT `559d3041` (preserve slow Oracle live recovery). The fork already
carries this exact patch for 0.16.1 with identical pristine/patched hashes
(`05256692...`/`9329e259...`), and the parent proves 0.17.1's published
`browserTabs.js` is byte-identical to 0.16.1's, so the same row extends to
0.17.1 and, after pristine-hash verification, to 0.17.2/0.17.3/0.18.0. This
closes a real fork gap: live recovery on every version except 0.16.1 currently
ignores the `ORACLE_LIVE_TERMINAL_TIMEOUT_MS` the fork itself sets
(`run.py:1639`) and drops slow recovered tabs at the stock 60s stall limit.
Implementation is one hash-gated row per version set plus manifest and compat
tests; the 0.16.1 legacy-migration machinery must not be disturbed.

(ii) ADOPT `9698fe74` (isolate DevSpace restart marker tests). Test-only
autouse fixture that redirects `CODEX_DEVSPACE_COMPAT_STATE_ROOT` to a tmp
path. The fork's `compat_state_root()`/`restart_marker_path()` names are
identical, so it ports verbatim; only four fork tests currently set the env
var individually, leaving the user's real `~/.codex/state/devspace-compat/
1.0.7/restart-required.json` writable by any unguarded future test.

(iii) ADAPT `2289ae3a` (release stale Oracle observer after exact recovery).
The fork has the same stale-observer window: after a separate `recover_run`
durably harvests terminal output, `execute_run` keeps waiting on the still-live
original Oracle process and holds the project submit mutex until the host
watchdog expires. Port the durable-terminal probe (`status` complete +
`session_authority` terminal + `terminal_harvested` true + nonempty output)
plus owned-tree termination (`taskkill /T /F`) onto the fork's
`wait_for_oracle_process` contract; the monotonic guard already exists as
`monotonic_race_preserved`. Termination is allowed only after durable harvest;
partial observations are never authority to terminate.

(iv) ADAPT `9cc8b59b` (prevent Oracle recursive self-observation). The fork
shares the theoretical recursion surface (a DevSpace web worker with host
shell access reading its own controller run's `state.json`). Port the bounded
detection signature, `settle-recursive-self-observation` CLI (append-only,
hash-bound sidecar modeled on `user-confirmed-no-submission`), the diagnose
signature before the OAuth-503 branch, and comprehensive terminalization —
with fork-native phrase groups (`observe-or-recover-exact-session-only`).
The prompt-level guard goes only into comprehensive stage-mission protocol
text; the regular one-line composer remains untouched (invariant 14).

(v) ADAPT `fd597fef` (restore Luna Max and Oracle version preflight). The
fork's multi-gpt server already pins `gpt-5.6-luna`/`max`
(`mcp_servers/multi-gpt/server.mjs:16-25`), so only the fail-closed
pre-persist CLI-accepts-contract canary (`LUNA_MAX_UNSUPPORTED_BY_CODEX_CLI`)
is new. Port `cached_oracle_version` re-pinned to the fork's exact 0.18.0
shape as the `--version` fallback, and add the `ORACLE_VERSION_FAILED`
branch to `proven_pre_submit_host_failure` with the fork's strict bindings so
the run settles instead of hanging unclassified. The setup-script half has no
fork counterpart and is skipped.

(vi) ADAPT `9456f8b5` (settle user-stopped comprehensive workflows). Generic
settlement, not Ultra-only. Fork gap: a provider-UI user stop ends as a
terminal harvest whose legacy outcome is `blocked`/`not_executed`/
`legacy_unclassified`; the workflow stays `attention_required` with the scope
locked and no settlement path (retirement refuses terminal records). Implement
as a `--cancel-user-stopped` command modeled on `retire_workflow`
(`comprehensive.py:2429-2533`) with confirmation
`user-confirmed-provider-stop`, exact workflow/scope/run path+hash binding,
the legacy outcome set `{blocked, not_executed, legacy_unclassified}`, and
scope release through the fork's `released` status + `_released_scope_is_valid`.

(vii) ADAPT `7ac2cb29` (settle DevSpace restart preflight failures). The fork
emits the identical evidence string (`run.py:1130`) and auto-settles at the
run level, but the comprehensive workflow lands `attention_required` with the
scope locked. Fold into (vi): accept confirmation
`user-confirmed-pre-submit-workflow-cancel` when run state is the auto-settled
restart failure (`pre_submit` authority, `failed_pre_submit`, `pending`).
Settlement never restarts the service; restart stays the separate
`confirm_service_restarted` path.

(viii) ADAPT `8ed6308c` (terminalize failed Ultra reviews). Generic
review-stage fix. The fork already stops on FAIL/REVISE
(`comprehensive.py:1075-1077`, `1578-1597`) but leaves the scope locked
forever. Extend `_terminal_review_state` to write a hash-bound
`codex.chatgpt.oracle-comprehensive-review-failed/v1` receipt (bound to the
review receipt path+sha256 and critical findings) and release the scope via
`released` + `_released_scope_is_valid`, keeping the existing
`attention_required` workflow state and `_terminal_attention` blocker text.

(ix) ADAPT `64f40b14` (safe delete and trash file tools). The fork exposes no
delete/trash surface today and pins 1.0.7 (invariant 15); the parent's
implementation is an upgrade-chain of 1.0.4 patches. Rework delete/trash onto
the fork's 1.0.7 hash-gated patch set preserving the guards verbatim
(`isStrictChildPath` lstat walk, `.git`/symlink rejection, trash byte
identity inside the workspace root), and optionally port the upgrade-chain
while-loop with `DEVSPACE_PATCH_CYCLE` detection. Drop the `read_chunk`/
file-safety half — the fork bounds reads via `workspaces.patch` and never
adopted the read bridge.

(x) ADAPT `a9c64e14` (persist ChatGPT local network onboarding). The fork has
no Local Network handling at all, and the throwaway-profile model loses the
grant every run (invariant 12). Implement on the fork setup surface
(`skills/chatgpt-workspace-setup`) as a consent-gated one-time step: HKCU
Chrome policy `LocalNetworkAccessAllowedForUrls` for the exact
`https://chatgpt.com` origin with readback verification, plus a fail-closed
seed-profile `Preferences` check; app registration stays manual and no
watchdog path is extended.

(xi) REJECT `1a99ef04`, `b2e8764d`, `3fc9bad4`
(actual sha `3fc9bad4f2161393a48dec80703c0c51173cc2bf`), `1d5c8b54`, and
`bcf78fef`: the read-only web-stage bridge, bridge preflight isolation,
strict workflow audit contract, lane receipts, and Strict Ultra guide are
Ultra-profile machinery (invariant 3 — deferred in 1.10.0); the DevSpace
patch parts also target the parent's 1.0.4 dist (invariant 15), and the
fork's Web Multi v1 already binds run identities
(`multi.py:345-356`, `560-588`). REJECT `191a8960`: the fork already settles
`DEVSPACE_SERVICE_RESTART_REQUIRED` end-to-end through its readiness schema
(`run.py:1090-1124`, `1362-1372`; `state.py:2520-2535`, `2936-2969`;
`diagnose.py:191-192`) with the same outcome shape; the parent's stderr-text
path and `oracle-pre-submit-host/v1` eligibility would duplicate fork
architecture without fixing any fork bug.

```powershell
git fetch https://github.com/ventianima-lab/codex-web-gpt-automation main
git rev-parse HEAD
git rev-parse FETCH_HEAD
git merge-base HEAD FETCH_HEAD
git rev-list --left-right --count HEAD...FETCH_HEAD
git diff --shortstat FETCH_HEAD...HEAD
git diff --name-status FETCH_HEAD...HEAD
```

## B. Oracle compatibility layer

- npm package: `@steipete/oracle@0.18.0`
- npm integrity: `sha512-o8KFd66zNt36jw5zdtQAV74bgrOlJibbyvnLsVikIWDamesYtez/dIUhQ4zqtD9jkx+7A6vcP9+JgcJt0H5pOw==`
- npm tarball SHA-256: `9bc8d08cb8d28473010c5a7b1f61bcb139f394e8de12a88344edcc993288ffcb`
- source tag: annotated `v0.18.0` object `5ca86d756d54582205682d9476b13bc6be9d2584`, peeled to release commit `083bba7e61f487ad3d99b42039d9f603f61dc4ff`; npm `gitHead` and observed `main` are the same commit.
- recovery versions: exact `0.16.1`, `0.17.0`, `0.17.1`, `0.17.2`, `0.17.3`, and `0.18.0`; only `0.18.0` may create a new run.

The exact 0.18.0 npm dist uses these hash-gated patches:

| Dist target | Pristine SHA-256 | Patched SHA-256 |
|---|---|---|
| `dist/src/browser/chromeLifecycle.js` | `312b45c44d4cd69a3a057e7bd1584b58182b4b37bc88f6ce6c7d11e216267c81` | `61440e467d51031efb7bfc319aef05de7c9061585e5eec148d0e353938eb2093` |
| `dist/src/browser/recoverConversation.js` | `d7e39d21acf07e6d227e761944519e11cd8d93930629cc87555d7de75a42d1ca` | `cc2a036f6e2409ae7edceee1f381a5062cd6cc5cd1618af465a1b384081ed69e` |
| `dist/src/browser/profileCopy.js` | `06c692861f8a4c1a8769f957b9c582426a13bf4972262c47c1f24a87b239064f` | `71459a25b7c46f57bae6f23a5498301f6f6a1d39addf0c1cd4eee1d99b03372c` |
| `dist/src/cli/browserConfig.js` | `52ddb9d0289849301f83863ed0b5209b8d9f071358e7784fcf4a5c8724b1c147` | `63920f771c36b34b95b67c54d49d3187bf7144f01651f567cdde41068b4a6e0e` |
| `dist/src/browser/index.js` | `d0f4f8972e3f755fe0f54d74a24a8e04346c9bc01509196b4ce625e4816f7b79` | `748cc9d20c4efca4942652c4631dc130819dbcdfd95f3267671c4a514f4ed3c3` |
| `dist/src/browser/actions/assistantResponse.js` | `93d2465ed7dce43d8093a91bada7656bc9ba7ba3729d2fcc43229fa8aa6e36de` | `aff8f7cb4e926b0e56c4b02456f54983b14fffa9e01f595fed4fd44a338d41f4` |
| `dist/src/browser/actions/promptComposer.js` | `db090a5fb6d13c4c88a68b5e474a53a19c3857295a64c3ba4a0eef1868d06000` | `3767d8a6702e42191e8195641ad2f0834882bed9cda1362a723c906249402d96` |
| `dist/src/browser/actions/thinkingTime.js` | `3d9d06b08417bca3b2d646eb4d46887d26c5de7c068d1e995c73b6b6e2f61199` | `e58fcd1f50cac2fdfb9334df485e035896586182acddbb46d846c12bdbdeb424` |

All eight targets have explicit 0.18.0 assets even though five pristine dist
files are byte-identical to 0.17.3. The three rebased targets are
`thinkingTime.js`, `browser/index.js`, and `cli/browserConfig.js`; unknown
bytes remain a hard compatibility failure. The regular composer contract
remains the bare `@DevSpace <absolute mission path>` string. This release
deliberately does not adopt an Ultra GPT mode or any automatic tier promotion.

The promptComposer row emits the bare `@` through CDP `Input.dispatchKeyEvent`,
then uses one fixed `delay(250)` settle before inserting the app name. Two live
fail-closed runs showed that both one-shot and split `Input.insertText` leave
literal `@DevSpace` with no suggestion UI; CDP documents `insertText` as input
that does not come from a key press. The resolver accepts an exact visible app
label inside the current Plugins result group, then requires both that unique
action receipt and ChatGPT's exact semantic app pill before the initial and
final pre-send gates pass. The diagnostic no-submission switch suppresses every
send path, including prompts that do not parse as app routes. Its census reports
generic visible action surfaces only as observations, not as proof that the
mention picker opened or that an app is unavailable. The five deployed
legacy levels `a3882c7881...`, `bb85c6f09f23...`, `87911b46026d...`,
`e9f28f36f652...`, and `dfbe8bfe8ff...` are
restored to pristine bytes through their exact legacy patches before the new
patch is applied.

The thinking-time row preserves Oracle 0.18.0's upstream Advanced Model/Effort
navigation, Japanese Intelligence labels, and disabled-tier detection with its
row-owned notice, while self-porting the fork's stronger Power proof. The visible current
effort pill must name a visible picker root through `aria-controls`. When that
root is an Advanced subtree, proof may expand to the pill's closest visible menu
only when that menu actually contains the controlled root. That one bound menu
must contain both the matching simple `4 of 5` Extra High or `5 of 5` Pro slider
and the coherent advanced `GPT-5.6 Sol` effort state twice consecutively.
A final diagnostic snapshot can reopen the race fallback only after a separate
read-only two-observation proof with the same containment rule; it cannot
authorize submission by itself. ChatGPT currently renders the exact simple
Power readout with `opacity: 0` while retaining its positive layout box. Its
inner ARIA slider uses the explicit zero-based range `0..4`, so the proof maps
that range to displayed Power `1..5` and rejects any control/text disagreement.
Non-positive opacity is permitted only on the test-id-bound readout node; every
ancestor, the owning pill, and the coherent Advanced view must remain visible
with positive opacity. The 0.18.0 patch accepts only exact 0.18.0 pristine
bytes (`3d9d06b0...`) and fails closed on every other hash. For a disabled
GPT-5.6 tier, the upstream `ThinkingTierUnavailableError` survives, while the
local strict contract extends it to `extra-high` and `heavy` and names the
required visible `Power 4 of 5` or `Power 5 of 5` proof.
The diagnostic race fallback is skipped for `option-disabled`, so a stale
visible Power snapshot cannot override the upstream unavailable-tier outcome.

The 0.17.3 exact-recovery assets remain immutable: thinking-time maps
`6ff4420e...` to `98724eaf...`, browser config maps `13b304a1...` to
`a76f338e...`, and browser index maps `421f15c6...` to `cb7b8289...`.
The five byte-identical targets retain their own explicit 0.17.3 assets and
contracts rather than aliasing 0.18.0. The 0.17.2 canonical patched level
`77d00dad...` and its older deployed hashes `ba5cf86e...`, `7ee4983f...`,
`decfb683...`, `91c5d356...`, `9583e9b4...`, and `fac49260...` belong
exclusively to the 0.17.2 exact-recovery contract. That contract restores each
known level to the 0.17.2 pristine `303d33eb...` through its exact reverse
asset before applying the 0.17.2 patch. Oracle 0.17.1 remains
exact-recovery-only with canonical patched hash `c973d280...`; its deployed
`01ad2aca...` proof level is restored through the exact
`thinkingTime.strict.pre-coherent-picker-proof.patch` reverse asset first.
The prior `fd7e6fcf...` diagnostic-race level and the shipped `5378da62...`
stable-visible and `2cf9f56a...` primary-CSS levels are restored through their
exact reverse assets before the stricter patch is applied.
All package hashes are computed over canonical LF bytes. A fresh npx install keeps LF dist bytes
while an older Windows deployment can carry CRLF bytes for the same patched
result, so canonical hashing makes one contract hash bind both flavors
instead of accepting two ambiguous hashes.  Known fork legacy levels migrate
safely to the final patched result: the previously shipped extra-high
fail-closed patch (deployed raw CRLF
`21027b691a86a3278e6c0b6e69c8b6ce0325b984cda7e4fca3ca284422958b16`) and that
patch plus the Pro-heavy upgrade (deployed raw CRLF
`300e910c1f592ccdda933d865525f303a6d255b43c71c6bcaff33d8186dccd0d`) are both
recognized by their canonical hashes and restored to pristine bytes before
the strict patch is applied; unknown bytes always fail closed.

Oracle 0.18.0 treats GPT-5.6 Sol effort as a visible Power slider.
Regular runs are the single supported `extra-high` tier and require the
visible `Power 4 of 5` proof before send; misleading `Medium` or `High`
aliases are rejected without silent downgrade.  Both Pro transports use the
same account-visible `gpt-5.6-sol` model with `heavy` (Oracle's internal
token) and require the full `Power 5 of 5` (`Pro`) proof including the
hidden-stale-picker and Unicode-label handling from the donor.  The evidence
route (`pro-attachment-only`) is attachment-only with no DevSpace or app; the
qualified write route (`pro-devspace`) sends the DevSpace mention plus the
mission path and adds no attachments.  A proven model-switcher, profile-flag,
or effort
selection-unverified failure settles only while the conversation URL and any
durable output are absent, and monotonic exact-session authority is never
regressed.

Oracle 0.18.0 keeps the bounded Answer-now placeholder predicate and explicit
headless handling from 0.17.3. It also makes Chrome cookie copying
default-false: sync occurs only through explicit `--browser-cookie-sync`,
explicit manual-login cookie sync, or supplied inline cookies. The rebased
patch retains that opt-in policy and warning while preserving the local
copy-profile override that disables the persistent manual-login profile for
the isolated run. Japanese Advanced/Effort labels and upstream disabled-tier
notices remain intact without weakening the local Power proof. The pnpm 11
repository migration does not change the published npm layout, target paths,
or Node `>=24` runtime floor.

For a new Oracle release, query registry metadata, download the exact npm tarball, verify integrity, calculate every pristine hash, dry-apply each patch, review changed upstream sources, calculate patched hashes, and only then update the version table and manifest. Source tags never substitute for npm dist bytes.

Oracle tag `v0.18.0`, npm `gitHead`, and `main` were observed at
`083bba7e61f487ad3d99b42039d9f603f61dc4ff` on 2026-08-20. Source main never
substitutes for the exact released npm dist bytes.

## C. DevSpace compatibility layer

- npm package: `@waishnav/devspace@1.0.7`
- npm integrity: `sha512-kP+Wk52qiMRwdqAP+nV4OZ4HU8feivZQ0k6u4ZUkvqxu8j0Rp/AU8H0K4T43G+zmu9WJKlYLTet7vIUeZHU72A==`
- npm tarball SHA-256: `fa0966d32b1182fe4a0150f1ce1515a2e687c5227bfaec7a064c362841b3ab28`
- source tag: annotated `v1.0.7` object `a625b290c141b826ae704620a09fced3f56f2010`, peeled commit `b5b4ab62a8718e1186aef815538741d9402f92ba`; GitHub reports the tag object unsigned. npm `gitHead` and GitHub Release `v1.0.7` bind the same commit.

| Dist target | Pristine SHA-256 | Patched SHA-256 | Meaning |
|---|---|---|---|
| `dist/server.js` | `42d340924421182eea7f2580f96c8d1d5aae459061a6a90804e6900905ef2d72` | `5bd899c33e5db3afd1f41eb220c6346ee27d29421fb58c47db498ae3b691a8f7` | expose the MCP-path OAuth authorization-server discovery route without weakening listener/public URL authority |
| `dist/workspaces.js` | `e11517f291cac33e37a66e84aeb80e1664a5abd0b6eb1e9bdb933d84c186efad` | `68a4c61ae0f509bd40d2a682e0b9bbbac72cb00dc96693f7646e6a535cc872ed` | skip transient trees and traverse in bounded concurrent batches while preserving filesystem boundaries |
| `dist/oauth-provider.js` | `90ff3fd116735e98af5751de1065538964f6eaae913171223e8e19337b9831b8` | `30790b1c4e83e7865b3519e4c4a99ca3a182264f405f0eea26c80f0c471252dc` | replay one consumed rotation only for the same client, duplicate-free exact scope set, and resource, for at most 30 seconds and never past the source token expiry, within a 32-entry in-memory bound; descendant consumption, expiry, revoke, and mismatch fail closed |

Conversation reuse metadata is a ChatGPT host boundary. The local Oracle runner does not create or inject `_meta["openai/session"]`. If the host supplies it, DevSpace may reuse the conversation binding; otherwise explicit existing `workspaceId` reuse is the supported fallback. Live reuse/reconnect/restart observations require separate submission approval.

For a new DevSpace release, verify exact registry/tarball identity, regenerate all three patches against the dist bytes, inspect router/middleware, traversal, and OAuth rotation changes, test OAuth/listener/restart/root behavior, then update the pin and manifest. The read-only `scripts/check_upstream.py` reports npm, source tag, GitHub Release, and default-branch drift independently but never promotes compatibility.

DevSpace npm latest, npm `gitHead`, source tag, and GitHub Release remain `1.0.7` / `b5b4ab62a8718e1186aef815538741d9402f92ba`. Default-branch `main` was observed separately at `d9855aa5e115d25417ac84f0af807968a3dae063` on 2026-08-20 and does not substitute for the npm dist. The three local contracts bind exact 1.0.7 pristine and patched bytes. The OAuth replay changes no credential or database schema, and this release makes no live registered-app canary claim.
