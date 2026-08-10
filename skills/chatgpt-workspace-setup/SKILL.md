---
name: chatgpt-workspace-setup
description: Part of the current Oracle path, perform the one-time, user-authorized DevSpace and Tailscale Funnel setup or read-only diagnosis for ChatGPT workspace access. Never use this during ordinary GPT runs and never automate ChatGPT settings or app selection.
---

# ChatGPT Workspace Setup

Use this skill only for a first connection, an explicitly requested DevSpace/Tailscale repair, or a read-only endpoint diagnosis. Ordinary ChatGPT modes must not call it.

## One-time setup

The user must provide every allowed project root and the Tailscale MagicDNS hostname. A drive root such as `C:\` is rejected. The setup process is intentionally interactive because DevSpace itself stores the Owner secret in its own standard location; never copy that secret into a manifest, log, or Git file.

Preview the exact setup plan first:

```powershell
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py setup --root C:\projects\example --hostname your-device.your-tailnet.ts.net --dry-run
```

Only after the user approves the interactive DevSpace initialization and public Funnel exposure:

```powershell
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py setup --root C:\projects\example --hostname your-device.your-tailnet.ts.net --apply
```

`--apply` runs DevSpace through Git Bash without a visible Windows console, starts `devspace serve`, registers the per-user `DevSpace MCP Server` Windows login entry, and creates an HTTPS Funnel to `127.0.0.1:7676`. During `devspace init`, enter only the listed roots and the public origin `https://<hostname>` (without `/mcp`).

## Change allowed roots

Do not rerun interactive initialization just to change filesystem access. Read,
preview, and replace the full allowlist with the repository helper:

```powershell
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py roots
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py roots --root C:\projects\one --root D:\work\two --dry-run
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py roots --root C:\projects\one --root D:\work\two --apply --restart
```

The command changes only `allowedRoots` in the active DevSpace `config.json`,
preserves every other config key and `auth.json`, rejects drive roots, verifies
the persisted readback, and restarts only when `--restart` is explicit.

Before starting or restarting DevSpace 1.0.6, run the installed
`bin/chatgpt_devspace_compat.py`. It hash-validates the exact upstream
`dist/workspaces.js`, backs it up, and applies bounded concurrent discovery
that skips transient `.pytest-*` and cache trees. If it reports
`service_restart_required=true`, restart DevSpace before any Oracle
submission. Unknown versions or hashes fail closed.

The only app information to enter manually in ChatGPT Developer Mode is:

- Recommended app name: `DevSpace`
- URL: `https://<hostname>/mcp`
- Complete the first Owner-password approval page that DevSpace presents.

Never open ChatGPT settings, register/delete an app, change permissions, inspect app lists, select an app name, or press Tab in the ChatGPT UI.

## Diagnosis

This is read-only and checks local DevSpace, confirms every requested root is
still authorized by the persisted `allowedRoots`, then checks Funnel status and
the public `/mcp` endpoint:

```powershell
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py doctor --root C:\projects\one --hostname your-device.your-tailnet.ts.net
```

If the public endpoint is healthy but a ChatGPT call still fails, report the same registration URL and stop. Do not re-register the app automatically.
