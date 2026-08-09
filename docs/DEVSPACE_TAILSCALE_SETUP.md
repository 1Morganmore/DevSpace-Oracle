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

The helper pins DevSpace `1.0.6` and applies its exact hash-validated Windows
compatibility patch before starting the service.

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

## Manual ChatGPT registration

Enable Developer Mode in ChatGPT and manually create the connector:

- Name: `DevSpace`
- MCP URL: `https://your-device.your-tailnet.ts.net/mcp`

Approve the initial Owner-password page when DevSpace asks. This tooling never opens settings, creates/deletes apps, picks permissions, inspects app lists, or selects an app in the composer.

## Read-only diagnosis

```powershell
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py doctor --root C:\projects\one --hostname your-device.your-tailnet.ts.net
```

Diagnosis checks local DevSpace `/mcp`, then `tailscale funnel status --json`, then the public `/mcp` endpoint. If the endpoint is healthy but a ChatGPT tool call fails, keep the server running and re-check the same manual connector URL; do not automate deletion or re-registration.

Tailscale Funnel makes the endpoint public. It requires Tailnet permissions and uses the device's stable MagicDNS name. Review Tailscale's policy and exposure rules before `--apply`.
