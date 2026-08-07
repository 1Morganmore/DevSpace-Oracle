# Differences from upstream

This file binds package compatibility to exact public artifacts. The local fork ref is the commit containing this document; obtain its immutable value with `git rev-parse HEAD` and use the commit reported by the release evidence, not a working-tree snapshot.

## A. Fork versus parent project

- Local ref: the exact commit containing this document in `1Morganmore/DevSpace-Oracle`.
- Parent main: `ventianima-lab/codexpro-automation@250b839a559cb61442feeb64bff6d49dfa185169`.
- Merge-base before this release commit: `250b839a559cb61442feeb64bff6d49dfa185169`.
- Pre-release ancestry: 15 commits ahead, 0 behind; the release commit adds one local commit.
- Final fork-versus-parent tree difference: 211 files, 16,631 insertions, and 53,955 deletions. The local release slice itself changes 178 files with 1,807 insertions and 52,844 deletions relative to its pre-release HEAD. The exact release commit is reported externally because a commit cannot contain its own hash.

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
| `dist/src/browser/actions/promptComposer.js` | `db090a5fb6d13c4c88a68b5e474a53a19c3857295a64c3ba4a0eef1868d06000` | `a3882c7881a7e787a33092350c494d950a6f67c38e6801cd1eaff20ac317532f` |
| `dist/src/browser/actions/thinkingTime.js` | `508f1fbc175b82e6bfd4c978da6199306800615f432e28d7721c155c402795ca` | `300e910c1f592ccdda933d865525f303a6d255b43c71c6bcaff33d8186dccd0d` |

Oracle 0.17.1 avoids selecting the Pro model row for GPT-5.6, but approval-gated live runs exposed two option misses: regular `extra-high` and Pro `heavy`, both while the UI remained on `GPT-5.6 Sol` with Effort `Pro`. The local thinking-time patch therefore fails closed for both contracts instead of treating the visible `Pro` effort as an `Extra High` alias or silently submitting the wrong Pro model/effort. Pro remains attachment-only `gpt-5.5-pro` with `heavy` pending a successful live proof.

For a new Oracle release, query registry metadata, download the exact npm tarball, verify integrity, calculate every pristine hash, dry-apply each patch, review changed upstream sources, calculate patched hashes, and only then update the version table and manifest. Source tags never substitute for npm dist bytes.

## C. DevSpace compatibility layer

- npm package: `@waishnav/devspace@1.0.6`
- npm integrity: `sha512-lLwUip5Wv1mwpEmAbpms7bourW5g0a0US1PDHCD2CITgCK6DnMTh5++6z8ODIEY+T30oxoTQlxdH4T+VkWlbNA==`
- npm tarball SHA-256: `1148a45afd70668ead498671eb47e080bad9cf36cf37ee2382add01612163b4a`
- source tag: `v1.0.6` at `074292acf19a7fe3407bdf6c7565ffd28c17656c`

| Dist target | Pristine SHA-256 | Patched SHA-256 | Meaning |
|---|---|---|---|
| `dist/server.js` | `84cd96ad4a021abd29dc028c0fb74acce17ab92a4a653d033d5dd830630c2096` | `fbe241bc6ef1c91e9aa4866637d9b3890de20adef30fd4d5d0920bf5306e5f1b` | expose the MCP-path OAuth authorization-server discovery route without weakening listener/public URL authority |
| `dist/workspaces.js` | `0da528d01555ab3cda0ddc71b749ff30db74497165fffb78e36ca84c97c38d8f` | `6f2610f22bb678ab768dde9ab4558296f65bf8cbcc247aa9a9d03b4133fab21d` | skip transient trees and traverse in bounded concurrent batches while preserving filesystem boundaries |

Conversation reuse metadata is a ChatGPT host boundary. The local Oracle runner does not create or inject `_meta["openai/session"]`. If the host supplies it, DevSpace may reuse the conversation binding; otherwise explicit existing `workspaceId` reuse is the supported fallback. Live reuse/reconnect/restart observations require separate submission approval.

For a new DevSpace release, verify exact registry/tarball identity, regenerate both patches against the dist bytes, inspect router/middleware and traversal changes, test OAuth/listener/restart/root behavior, then update the pin and manifest. The read-only `scripts/check_upstream.py` reports drift but never promotes compatibility.
