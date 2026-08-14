# Differences from upstream

This file binds package compatibility to exact public artifacts. The local fork ref is the commit containing this document; obtain its immutable value with `git rev-parse HEAD` and use the commit reported by the release evidence, not a working-tree snapshot.

## A. Fork versus parent project

- Local ref: the exact commit containing this document in `1Morganmore/DevSpace-Oracle`.
- Parent main observed and audited on 2026-08-14: `ventianima-lab/codex-web-gpt-automation@66c23f170661e36bfc9d0f970c01dd4234863d6c` (parent 1.14.0). Previous audited head: `9bd6843ee9424b260cdc6968feace2bb46ef1ceb` (2026-08-13).
- Last direct integration baseline: `9542abeef6aa544f4ee6af03bab61cef3474f9e4`.
- Merge-base before this release commit: `250b839a559cb61442feeb64bff6d49dfa185169`.
- Observed ancestry immediately before this release commit: 47 commits ahead, 61 behind.
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

- npm package: `@steipete/oracle@0.17.3`
- npm integrity: `sha512-xoziw8brto9rEtOROHcMr4vHu70DDGQJ41bwMHpkJgA77MIZ11B+IQtGqKpZ48WkihmHkEUVEvWsf+eDwxtwgg==`
- npm tarball SHA-256: `9933f177884d6ca662f1131dbb9c17b95c0b01ccd877a2d93e5ee5f0778b357f`
- source tag: annotated `v0.17.3` object `0cc868ea1f8e769cbed90c71462f6d338ef7520b`, peeled to release commit `6b17e6db0caea40088cc80a741bb314db1cd566c`
- recovery versions: exact `0.16.1`, `0.17.0`, `0.17.1`, `0.17.2`, and `0.17.3`; only `0.17.3` may create a new run.

The exact 0.17.3 npm dist uses these hash-gated patches:

| Dist target | Pristine SHA-256 | Patched SHA-256 |
|---|---|---|
| `dist/src/browser/chromeLifecycle.js` | `312b45c44d4cd69a3a057e7bd1584b58182b4b37bc88f6ce6c7d11e216267c81` | `61440e467d51031efb7bfc319aef05de7c9061585e5eec148d0e353938eb2093` |
| `dist/src/browser/recoverConversation.js` | `d7e39d21acf07e6d227e761944519e11cd8d93930629cc87555d7de75a42d1ca` | `cc2a036f6e2409ae7edceee1f381a5062cd6cc5cd1618af465a1b384081ed69e` |
| `dist/src/browser/profileCopy.js` | `06c692861f8a4c1a8769f957b9c582426a13bf4972262c47c1f24a87b239064f` | `71459a25b7c46f57bae6f23a5498301f6f6a1d39addf0c1cd4eee1d99b03372c` |
| `dist/src/cli/browserConfig.js` | `13b304a1b41cbc85257d9340a620bccd4d18bc52a36285ba46c2f72af84f0f84` | `a76f338e1afb3573c3436cd261ccbcefacd9c879c71a45e110cf7a3602a06d22` |
| `dist/src/browser/index.js` | `421f15c6693799571d586d80b7fc35b10492a63acf78d901e21786bf6ec71a90` | `cb7b828902163bac941f5890f78edd136cf723e17e262c1347e2843df20c3e44` |
| `dist/src/browser/actions/assistantResponse.js` | `93d2465ed7dce43d8093a91bada7656bc9ba7ba3729d2fcc43229fa8aa6e36de` | `aff8f7cb4e926b0e56c4b02456f54983b14fffa9e01f595fed4fd44a338d41f4` |
| `dist/src/browser/actions/promptComposer.js` | `db090a5fb6d13c4c88a68b5e474a53a19c3857295a64c3ba4a0eef1868d06000` | `3767d8a6702e42191e8195641ad2f0834882bed9cda1362a723c906249402d96` |
| `dist/src/browser/actions/thinkingTime.js` | `6ff4420e81570f6c0a4e277bdd993caf66739c3f633a7cdb733ed645bec2acda` | `98724eaf24e27d6f75b3eb7795c49650aee0be6a9a3698e09882c6d3a06c3185` |

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

The thinking-time row preserves Oracle 0.17.3's upstream Advanced Model/Effort
navigation, including its Japanese Intelligence labels, and self-ports the
fork's stronger Power proof. The visible current
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
with positive opacity. The 0.17.3 patch accepts only exact 0.17.3 pristine
bytes (`6ff4420e...`) and fails closed on every other hash. The 0.17.2 canonical
patched level `77d00dad...` and its older deployed hashes `ba5cf86e...`,
`7ee4983f...`, `decfb683...`, `91c5d356...`, `9583e9b4...`, and
`fac49260...` belong exclusively to the 0.17.2 exact-recovery contract. That
contract restores each known level to the 0.17.2 pristine
`303d33eb...` through its exact reverse asset before applying the 0.17.2
patch. Oracle 0.17.1 remains exact-recovery-only with canonical patched hash
`c973d280...`. Its
deployed `01ad2aca...` proof level is restored through the exact
`thinkingTime.strict.pre-coherent-picker-proof.patch` reverse asset before
that recovery contract is applied.
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

Oracle 0.17.3 treats GPT-5.6 Sol effort as a visible Power slider.
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

Oracle 0.17.3 also bounds the Answer-now placeholder predicate to short,
whole-string browser chrome so a substantive answer that mentions those labels
is retained. Manual-login reattach can synchronize cookies only through the
upstream explicit opt-in, explicit local `--browser-headless` is honored, and
Japanese Advanced/Effort labels are recognized without weakening the local
Power proof. All four upstream changes are preserved under the local patches.
The pnpm 11 repository migration does not change the published npm layout,
target paths, or Node `>=24` runtime floor.

For a new Oracle release, query registry metadata, download the exact npm tarball, verify integrity, calculate every pristine hash, dry-apply each patch, review changed upstream sources, calculate patched hashes, and only then update the version table and manifest. Source tags never substitute for npm dist bytes.

Oracle main was observed at `3a185f55918a8f0dd36f9c2f0144550616b88803` on 2026-08-14, two commits ahead of `v0.17.3`. Its unreleased cookie-sync-default change is not adopted: the local runner uses Oracle-owned throwaway copies of the signed-in profile, and source main never substitutes for exact released npm dist bytes.

## C. DevSpace compatibility layer

- npm package: `@waishnav/devspace@1.0.7`
- npm integrity: `sha512-kP+Wk52qiMRwdqAP+nV4OZ4HU8feivZQ0k6u4ZUkvqxu8j0Rp/AU8H0K4T43G+zmu9WJKlYLTet7vIUeZHU72A==`
- npm tarball SHA-256: `fa0966d32b1182fe4a0150f1ce1515a2e687c5227bfaec7a064c362841b3ab28`
- source tag: `v1.0.7`; tag object `a625b290c141b826ae704620a09fced3f56f2010`, peeled commit `b5b4ab62a8718e1186aef815538741d9402f92ba`

| Dist target | Pristine SHA-256 | Patched SHA-256 | Meaning |
|---|---|---|---|
| `dist/server.js` | `42d340924421182eea7f2580f96c8d1d5aae459061a6a90804e6900905ef2d72` | `5bd899c33e5db3afd1f41eb220c6346ee27d29421fb58c47db498ae3b691a8f7` | expose the MCP-path OAuth authorization-server discovery route without weakening listener/public URL authority |
| `dist/workspaces.js` | `e11517f291cac33e37a66e84aeb80e1664a5abd0b6eb1e9bdb933d84c186efad` | `68a4c61ae0f509bd40d2a682e0b9bbbac72cb00dc96693f7646e6a535cc872ed` | skip transient trees and traverse in bounded concurrent batches while preserving filesystem boundaries |

Conversation reuse metadata is a ChatGPT host boundary. The local Oracle runner does not create or inject `_meta["openai/session"]`. If the host supplies it, DevSpace may reuse the conversation binding; otherwise explicit existing `workspaceId` reuse is the supported fallback. Live reuse/reconnect/restart observations require separate submission approval.

For a new DevSpace release, verify exact registry/tarball identity, regenerate both patches against the dist bytes, inspect router/middleware and traversal changes, test OAuth/listener/restart/root behavior, then update the pin and manifest. The read-only `scripts/check_upstream.py` reports drift but never promotes compatibility.

DevSpace main and release `v1.0.7` were observed at `b5b4ab62a8718e1186aef815538741d9402f92ba` on 2026-08-12 and re-observed unchanged on 2026-08-13. The release changes workspace-reuse guidance and the unknown-workspace error only; allowed roots, OAuth/healthz routing, and workspace traversal semantics are unchanged. Both local patches apply cleanly to the exact npm dist bytes, so the tested pin is promoted to `1.0.7`.
