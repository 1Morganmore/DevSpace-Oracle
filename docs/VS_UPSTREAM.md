# Differences from upstream

This file binds package compatibility to exact public artifacts. The local fork ref is the commit containing this document; obtain its immutable value with `git rev-parse HEAD` and use the commit reported by the release evidence, not a working-tree snapshot.

## A. Fork versus parent project

- Local ref: the exact commit containing this document in `1Morganmore/DevSpace-Oracle`.
- Parent main: `ventianima-lab/codexpro-automation@9542abeef6aa544f4ee6af03bab61cef3474f9e4`.
- Merge-base before this release commit: `250b839a559cb61442feeb64bff6d49dfa185169`.
- Pre-release ancestry: 20 commits ahead, 21 behind; the release commit adds one local commit.
- Final fork-versus-parent tree difference: 212 files, 17,124 insertions, and 53,968 deletions at the pre-release head. The exact release commit is reported externally because a commit cannot contain its own hash.

The fork keeps Oracle-only browser submission, DevSpace workspace transport, exact-slug recovery, per-project locks, receipt-owned installation, Windows process/profile isolation, and bounded Web Multi sessions. Parent sync must preserve monotonic terminal authority, exact package/hash gating, host-only state, mutex/schema/receipt identities, and the absence of automatic Web Multi or alternate-backend fallback.

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

- npm package: `@steipete/oracle@0.17.1`
- npm integrity: `sha512-bq4SqMvRtT5Im+R57UPSXTV5p/BFTU24OXgGXqx2ckABWFX9uLDuKeJLoOdfBm7RzllrzjrlSSGgiMsrrvh+9Q==`
- npm tarball SHA-256: `05ee6f2d7f0d2ca95b5114747fdb44181d8c79cbe68ecaaed947c0a028f3a802`
- source tag: `v0.17.1` at `a835b0129ccb879b6a15628640b4eebb6aa66294`
- recovery versions: exact `0.16.1`, `0.17.0`, and `0.17.1`; only `0.17.1` may create a new run.

The exact 0.17.1 npm dist uses these hash-gated patches:

| Dist target | Pristine SHA-256 | Patched SHA-256 |
|---|---|---|
| `dist/src/browser/chromeLifecycle.js` | `312b45c44d4cd69a3a057e7bd1584b58182b4b37bc88f6ce6c7d11e216267c81` | `61440e467d51031efb7bfc319aef05de7c9061585e5eec148d0e353938eb2093` |
| `dist/src/browser/recoverConversation.js` | `d7e39d21acf07e6d227e761944519e11cd8d93930629cc87555d7de75a42d1ca` | `cc2a036f6e2409ae7edceee1f381a5062cd6cc5cd1618af465a1b384081ed69e` |
| `dist/src/browser/profileCopy.js` | `06c692861f8a4c1a8769f957b9c582426a13bf4972262c47c1f24a87b239064f` | `71459a25b7c46f57bae6f23a5498301f6f6a1d39addf0c1cd4eee1d99b03372c` |
| `dist/src/cli/browserConfig.js` | `989f14399c8aa51913752306135e11d97e4f1c55b2baf984907f1b54959cc340` | `bd18d11e4770fa5335c889b7856622f2da4199351ec65bc17a5ec1f472e2506f` |
| `dist/src/browser/index.js` | `335f29c8864399cf2795333e4da8b87bc1b3591c30862eb9e82ea12cd3b37d11` | `9a78695ba89a6e7eb6761dd06b9be74d500ac65b585158d75f8fd3c7a6eb8895` |
| `dist/src/browser/actions/assistantResponse.js` | `0bbc106f79c6abf253690c83794a2dab1b432378f57e16542d15cfcd5365e16d` | `18661304c7fb545bc327876d38045818cbd23257488137836d43661be8742af4` |
| `dist/src/browser/actions/promptComposer.js` | `db090a5fb6d13c4c88a68b5e474a53a19c3857295a64c3ba4a0eef1868d06000` | `3767d8a6702e42191e8195641ad2f0834882bed9cda1362a723c906249402d96` |
| `dist/src/browser/actions/thinkingTime.js` | `508f1fbc175b82e6bfd4c978da6199306800615f432e28d7721c155c402795ca` | `3f969712b184588d1f34ef4f55b439c86256d112bb0fa1688bb473b61fd3dcc3` |

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

The thinking-time row is the final upstream Power-slider patch from the
audited donor `9542abee` (`thinkingTime.strict.patch`).  All package hashes
are computed over canonical LF bytes: a fresh npx install keeps LF dist bytes
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

Oracle 0.17.1 now treats GPT-5.6 Sol effort as a visible Power slider.
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

## C. DevSpace compatibility layer

- npm package: `@waishnav/devspace@1.0.6`
- npm integrity: `sha512-lLwUip5Wv1mwpEmAbpms7bourW5g0a0US1PDHCD2CITgCK6DnMTh5++6z8ODIEY+T30oxoTQlxdH4T+VkWlbNA==`
- npm tarball SHA-256: `1148a45afd70668ead498671eb47e080bad9cf36cf37ee2382add01612163b4a`
- source tag: `v1.0.6`; tag object `074292acf19a7fe3407bdf6c7565ffd28c17656c`, peeled commit `3bd0378b128c048add810dff00efeff4e7326eb9`

| Dist target | Pristine SHA-256 | Patched SHA-256 | Meaning |
|---|---|---|---|
| `dist/server.js` | `84cd96ad4a021abd29dc028c0fb74acce17ab92a4a653d033d5dd830630c2096` | `fbe241bc6ef1c91e9aa4866637d9b3890de20adef30fd4d5d0920bf5306e5f1b` | expose the MCP-path OAuth authorization-server discovery route without weakening listener/public URL authority |
| `dist/workspaces.js` | `0da528d01555ab3cda0ddc71b749ff30db74497165fffb78e36ca84c97c38d8f` | `6f2610f22bb678ab768dde9ab4558296f65bf8cbcc247aa9a9d03b4133fab21d` | skip transient trees and traverse in bounded concurrent batches while preserving filesystem boundaries |

Conversation reuse metadata is a ChatGPT host boundary. The local Oracle runner does not create or inject `_meta["openai/session"]`. If the host supplies it, DevSpace may reuse the conversation binding; otherwise explicit existing `workspaceId` reuse is the supported fallback. Live reuse/reconnect/restart observations require separate submission approval.

For a new DevSpace release, verify exact registry/tarball identity, regenerate both patches against the dist bytes, inspect router/middleware and traversal changes, test OAuth/listener/restart/root behavior, then update the pin and manifest. The read-only `scripts/check_upstream.py` reports drift but never promotes compatibility.
