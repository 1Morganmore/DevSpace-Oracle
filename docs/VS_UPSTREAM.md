# Differences from upstream

This file binds package compatibility to exact public artifacts. The local fork ref is the commit containing this document; obtain its immutable value with `git rev-parse HEAD` and use the commit reported by the release evidence, not a working-tree snapshot.

## A. Fork versus parent project

- Local ref: the exact commit containing this document in `1Morganmore/DevSpace-Oracle`.
- Parent main observed and audited on 2026-08-11: `ventianima-lab/codexpro-automation@77bb79afcde2e5b95b18f5cc0490d1592f8fb954`.
- Last direct integration baseline: `9542abeef6aa544f4ee6af03bab61cef3474f9e4`.
- Merge-base before this release commit: `250b839a559cb61442feeb64bff6d49dfa185169`.
- Observed ancestry immediately before this release commit: 32 commits ahead, 30 behind.
- The exact release tree difference is reported externally after commit because a commit cannot contain its own final hash.

The fork keeps Oracle-only browser submission, DevSpace workspace transport, exact-slug recovery, per-project locks, receipt-owned installation, Windows process/profile isolation, and bounded Web Multi sessions. Parent sync must preserve monotonic terminal authority, exact package/hash gating, host-only state, mutex/schema/receipt identities, and the absence of automatic Web Multi or alternate-backend fallback.

The nine commits after `9542abee` were reviewed individually. The selector-proof changes in `51675967` and `d8f8fac1` are self-developed on the active fork Power patch: only visible picker candidates count, all three independent Pro signals are required, and two consecutive observations are required before an already-selected result. The existing final diagnostic-race fallback remains intact. The recovered-lane and merger-resume changes in `39f750f4` and `916aeffb` are deferred because they require adaptation to this fork's manifest hashes, parent lock, terminal seal, and monotonic exact-session authority. The persisted `allowedRoots` doctor readback from `69ad58c2` is selected. The `075b3719` change is intentionally rejected: making read-only DevSpace the default Pro route violates this fork's Pro contract, which is Oracle attachment-only with no app. The later `6d24cce` Funnel recovery is self-developed rather than cherry-picked: this fork's explicit `ensure` command waits for the exact local `/healthz` identity, creates only an absent exact mapping, refuses conflicts, reads back any change, and proves the public `/healthz` identity. It does not accept permissive `/mcp` status codes or claim automatic login recovery without a supervised startup path. The `ae3b3caf` OAuth fix is also self-developed on the pinned 1.0.6 launch path: all four managed `serve` entries advertise `devspace,offline_access`, while ChatGPT reconnect or recreation remains a manual settings action. The `77bb79af` task-outcome change is not applied because it governs the same rejected `pro-devspace-readonly` transport; this fork has no such route, keeps Pro attachment-only with the legacy non-DevSpace outcome contract, and already requires v1 task outcomes for regular DevSpace runs.

```powershell
git fetch https://github.com/ventianima-lab/codexpro-automation main
git rev-parse HEAD
git rev-parse FETCH_HEAD
git merge-base HEAD FETCH_HEAD
git rev-list --left-right --count HEAD...FETCH_HEAD
git diff --shortstat FETCH_HEAD...HEAD
git diff --name-status FETCH_HEAD...HEAD
```

## B. Oracle compatibility layer

- npm package: `@steipete/oracle@0.17.2`
- npm integrity: `sha512-Y2I/sTML2YPZrmYaw1QbpNd7bt6so9ld1pTjRP/MiEKTWanYjoICkmCpWBplPXq+KzHiVsgyPqUZpwxxOpa2Jg==`
- npm tarball SHA-256: `983a1546d04bac99409124f12dfae32012b0cfd61b084f349a4d9f7d7c5b1350`
- source tag: `v0.17.2` at `4bd5989622532a3de4334a16d64a6ad982217f28`
- recovery versions: exact `0.16.1`, `0.17.0`, `0.17.1`, and `0.17.2`; only `0.17.2` may create a new run.

The exact 0.17.2 npm dist uses these hash-gated patches:

| Dist target | Pristine SHA-256 | Patched SHA-256 |
|---|---|---|
| `dist/src/browser/chromeLifecycle.js` | `312b45c44d4cd69a3a057e7bd1584b58182b4b37bc88f6ce6c7d11e216267c81` | `61440e467d51031efb7bfc319aef05de7c9061585e5eec148d0e353938eb2093` |
| `dist/src/browser/recoverConversation.js` | `d7e39d21acf07e6d227e761944519e11cd8d93930629cc87555d7de75a42d1ca` | `cc2a036f6e2409ae7edceee1f381a5062cd6cc5cd1618af465a1b384081ed69e` |
| `dist/src/browser/profileCopy.js` | `06c692861f8a4c1a8769f957b9c582426a13bf4972262c47c1f24a87b239064f` | `71459a25b7c46f57bae6f23a5498301f6f6a1d39addf0c1cd4eee1d99b03372c` |
| `dist/src/cli/browserConfig.js` | `8a355cd8828a5025ea66c401b54140152bd1fe5538254893d577d52bc4a0f852` | `78d022150b959aa4cb26f2e2a743f88277246979f96813d91a4bcc55835dec18` |
| `dist/src/browser/index.js` | `335f29c8864399cf2795333e4da8b87bc1b3591c30862eb9e82ea12cd3b37d11` | `9a78695ba89a6e7eb6761dd06b9be74d500ac65b585158d75f8fd3c7a6eb8895` |
| `dist/src/browser/actions/assistantResponse.js` | `0bbc106f79c6abf253690c83794a2dab1b432378f57e16542d15cfcd5365e16d` | `18661304c7fb545bc327876d38045818cbd23257488137836d43661be8742af4` |
| `dist/src/browser/actions/promptComposer.js` | `db090a5fb6d13c4c88a68b5e474a53a19c3857295a64c3ba4a0eef1868d06000` | `3767d8a6702e42191e8195641ad2f0834882bed9cda1362a723c906249402d96` |
| `dist/src/browser/actions/thinkingTime.js` | `303d33ebe915b27407ca22ec0da1d18729464ce50417f405ddb628c31f6fb867` | `91c5d356a597fbf1a8e08cde922fd468a94f8cd3a9e441d7534fb7877a117828` |

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

The thinking-time row preserves Oracle 0.17.2's upstream Advanced Model/Effort
navigation and self-ports the fork's stronger Power proof. The visible current
effort pill must name one picker root through `aria-controls`; that same visible
root must contain both the matching simple `4 of 5` Extra High or `5 of 5` Pro
slider and the coherent advanced `GPT-5.6 Sol` effort state twice consecutively.
A final
diagnostic snapshot can reopen the race fallback only after a separate
read-only two-observation proof; it cannot authorize submission by itself.
Proof visibility rejects non-positive computed opacity on the candidate or any
ancestor, so a visually hidden stale picker subtree cannot authorize selection.
Oracle 0.17.1 remains exact-recovery-only with canonical patched hash
`c973d280...`; its deployed `01ad2aca...` proof level is restored through the
exact `thinkingTime.strict.pre-coherent-picker-proof.patch` reverse asset before
that recovery contract is applied.
The prior `fd7e6fcf...` diagnostic-race level and the shipped `5378da62...`
stable-visible and `2cf9f56a...` primary-CSS levels are restored through their
exact reverse assets before the stricter patch is applied.
All package hashes are computed over canonical LF bytes: a fresh npx install keeps LF dist bytes
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

Oracle 0.17.2 treats GPT-5.6 Sol effort as a visible Power slider.
Regular runs are the single supported `extra-high` tier and require the
visible `Power 4 of 5` proof before send; misleading `Medium` or `High`
aliases are rejected without silent downgrade.  Pro remains attachment-only
with no DevSpace or app: it uses the same account-visible `gpt-5.6-sol` model
with `heavy` (Oracle's internal token) and requires the full `Power 5 of 5`
(`Pro`) proof including the hidden-stale-picker and Unicode-label handling
from the donor.  A proven model-switcher, profile-flag, or effort
selection-unverified failure settles only while the conversation URL and any
durable output are absent, and monotonic exact-session authority is never
regressed.

For a new Oracle release, query registry metadata, download the exact npm tarball, verify integrity, calculate every pristine hash, dry-apply each patch, review changed upstream sources, calculate patched hashes, and only then update the version table and manifest. Source tags never substitute for npm dist bytes.

Oracle main was observed at `f5b9c8106cf6b826b3d48fc5a0fb19de26ee584b` on 2026-08-12. It remains newer than the `v0.17.2` release tag. The only runtime-source delta since the prior audit is upstream Japanese Intelligence effort-label recognition; it is not adopted because the local compatibility proof uses different Power-selector authority and must be ported and verified independently. No unreleased source-main code is adopted.

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

DevSpace main and release `v1.0.7` were observed at `b5b4ab62a8718e1186aef815538741d9402f92ba` on 2026-08-12. The release changes workspace-reuse guidance and the unknown-workspace error only; allowed roots, OAuth/healthz routing, and workspace traversal semantics are unchanged. Both local patches apply cleanly to the exact npm dist bytes, so the tested pin is promoted to `1.0.7`.
