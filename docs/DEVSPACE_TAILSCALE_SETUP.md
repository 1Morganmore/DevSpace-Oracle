# DevSpace + Tailscale Funnel setup

This repository does not modify DevSpace upstream and does not automate the ChatGPT settings UI. DevSpace is a local MCP server; it can read, edit, and run commands inside the roots you approve, so choose narrow project directories rather than an entire drive.

## Prerequisites

- Node.js 24–26.x, npm, and Git Bash on Windows.
- Tailscale with MagicDNS, HTTPS, and Funnel permission enabled for this device.
- A stable MagicDNS hostname, for example `your-device.your-tailnet.ts.net`.

## First connection (explicit and interactive)

From this repository, preview the plan and check the roots:

```powershell
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py setup --root C:\projects\one --root C:\projects\two --hostname your-device.your-tailnet.ts.net --dry-run
```

After reviewing the plan, use `--apply`. It invokes `devspace init` through Git Bash, then starts `devspace serve` and configures a Tailscale HTTPS Funnel to the local default port (7676). DevSpace asks you to select roots and enter the public origin. Enter exactly the reviewed roots and `https://your-device.your-tailnet.ts.net`, without `/mcp`.

If `%USERPROFILE%\.devspace\config.json` already exists (a previous
installation), `--apply` never re-runs interactive init. It backs up that file
and atomically merges the requested roots into it, preserving the Owner
credential, OAuth state, and every other key; symlinked or invalid configs
fail closed without any mutation.

The helper pins DevSpace `1.0.7` and applies its exact hash-validated Windows
compatibility patch before starting the service.

Every managed `serve` entry advertises the `devspace` and `offline_access`
OAuth scopes. DevSpace 1.0.7 already issues and rotates refresh tokens; the
additional discovery scope lets ChatGPT request renewal after the one-hour
access token expires. An app created before this metadata was exposed may need
one manual reconnect or recreation so ChatGPT reads the updated discovery
document. This tooling never performs that settings action.

The helper will not overwrite an existing Funnel mapping. If port 443 is
already owned by another local service, choose an unused supported Funnel port
explicitly, for example `--public-port 8443`; the registration URL then becomes
`https://your-device.your-tailnet.ts.net:8443/mcp`.

```powershell
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py setup --root C:\projects\one --root C:\projects\two --hostname your-device.your-tailnet.ts.net --apply
```

DevSpace prints an Owner password during initialization and stores it in its standard local configuration. Do not put that password in a script, manifest, issue, or repository.

## Change allowed roots without reinitializing

The `roots` command replaces the complete allowed-root list without reading or
rewriting `auth.json`:

```powershell
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py roots
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py roots --root C:\projects\one --root D:\work\two --dry-run
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py roots --root C:\projects\one --root D:\work\two --apply --restart
```

Omit `--restart` to persist the change for the next service start. The command
preserves all other `config.json` keys and verifies the exact saved list.

## Restore an approved Funnel route

After DevSpace or Tailscale restarts, explicitly restore only the reviewed
route:

```powershell
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py ensure --root C:\projects\one --hostname your-device.your-tailnet.ts.net
```

`ensure` waits up to 30 seconds for the exact local DevSpace `/healthz`
identity. It reuses a matching Funnel, creates the exact mapping only when it
is absent, refuses a conflicting mapping, reads the mapping back after any
change, and requires the same exact identity through the public `/healthz`
endpoint. It does not start DevSpace, change roots, register startup tasks, or
touch ChatGPT settings or app registration.

## Recycle after manual registration or reconnect

After a manual first registration or requested reconnect, recycle the managed
DevSpace process exactly once without changing its roots, Owner credential,
OAuth database, or Funnel hostname:

```powershell
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py post-register --root C:\projects\one --hostname your-device.your-tailnet.ts.net
```

`post-register` first requires the exact local DevSpace `/healthz` identity.
It then recycles only the exclusive managed HTTPS slot: the slot is turned off
with a scoped `tailscale funnel --bg --https=<port> off` only when it is a
single `/` handler proxying exactly `http://127.0.0.1:<local_port>`, and is
reasserted to the same target afterwards. It never uses the global
`tailscale funnel reset` and never removes shared path handlers, conflicting
mappings, or other ports — any conflict fails closed before mutation.

Verify the registered app with a fresh regular, non-Pro Oracle `@codex`
read-only probe that opens the exact project root and reads a small directory
listing. Do not substitute Codex Desktop's built-in `DevSpace` plugin tools:
they are a separate connector and do not validate the manually registered
ChatGPT app. Never spend a Pro submission as the first connectivity probe.

## Manual ChatGPT registration

Enable Developer Mode in ChatGPT and manually create the connector:

- Name: `DevSpace`
- MCP URL: `https://your-device.your-tailnet.ts.net/mcp`

Approve the initial Owner-password page when DevSpace asks. This tooling never opens settings, creates/deletes apps, picks permissions, inspects app lists, or selects an app in the composer.

## Read-only diagnosis

```powershell
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py doctor --root C:\projects\one --hostname your-device.your-tailnet.ts.net
```

Diagnosis checks the exact local DevSpace `/healthz` identity, then `tailscale funnel status --json`, then the exact public `/healthz` identity. If the endpoint is healthy but a ChatGPT tool call fails just after manual registration or reconnect, run the explicit `post-register` refresh once and repeat only the regular read-only Oracle probe. If it still fails, keep the server running and report the same connector URL; do not automate deletion, re-registration, or repeated refreshes.

Tailscale Funnel makes the endpoint public. It requires Tailnet permissions and uses the device's stable MagicDNS name. Review Tailscale's policy and exposure rules before `--apply`.
