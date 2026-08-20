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

After reviewing the plan, use `--apply`. It invokes `devspace init` through Git Bash, then starts `devspace serve`, configures a Tailscale HTTPS Funnel to the local default port (7676), and starts the persistent login watchdog described below. DevSpace asks you to select roots and enter the public origin. Enter exactly the reviewed roots and `https://your-device.your-tailnet.ts.net`, without `/mcp`.

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

## Persistent Windows watchdog

Setup replaces the existing per-user
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` value named exactly
`DevSpace MCP Server`; it does not create another startup value. That value
launches `pythonw.exe ... devspace_tailscale_setup.py watchdog`, so the process
has no console window. Setup also starts the same watchdog immediately. A
Windows named mutex permits only one watchdog in the login session.

The production watchdog runs continuously with a 300-second interval. Every
cycle re-reads the live `~/.devspace/config.json`, including the complete
`allowedRoots`, local port, and `publicBaseUrl`; the Run command does not embed
stale copies. The local `/healthz` response is necessary but never sufficient:
before the watchdog reports `READY`, keeps an existing Funnel, or creates one,
the compatibility probe must prove that the listening PID command names the
tested DevSpace package root, that the pinned files have their validated
patched hashes, and that no restart marker remains.

A healthy cycle is read-only. If the exact listener is absent or verified but
needs repair, the watchdog prepares pinned `@waishnav/devspace@1.0.7`, applies
the hash-validated compatibility lifecycle, stops only a process independently
proven to be that exact service, starts it hidden, and confirms the restart. It
creates and reads back an absent exact Funnel slot only after another listener
proof. A spoofed health response, unknown listener, conflicting mapping, or
failed proof fails closed. An unknown listener is never stopped. If the exact
configured Funnel slot maps to its untrusted local port, only that slot is
turned off with the matching `tailscale funnel ... off` command; unrelated
Serve and Funnel slots are never reset or changed.

After every cycle, including permanent failures, the hidden process atomically
replaces `%CODEX_HOME%\state\devspace-watchdog\status.json` (defaulting to
`%USERPROFILE%\.codex`) with restrictive permissions. The heartbeat contains
only its schema, timestamp, success flag, result code, verification statuses,
and the last verified PID/port/Funnel identity. It never contains allowed or
package roots, commands, URLs with credentials, stderr, tokens, passwords, or
Owner secrets. Fatal watchdog startup, configuration, and mutex failures are
also recorded before a nonzero exit. Failures remain paced at the normal
interval and are not repeatedly logged.

The watchdog never rewrites `config.json`, `auth.json`, Owner credentials,
OAuth clients or tokens, allowed roots, ChatGPT registration, or ChatGPT
settings. The hidden `--interval-seconds` and `--max-cycles` switches exist only
to bound focused diagnostics and tests; the registered production command uses
the persistent defaults.

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

## Request an immediate approved Funnel check

The watchdog ordinarily restores an absent reviewed route on its next cycle.
To request the same check immediately, run:

```powershell
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py ensure --root C:\projects\one --hostname your-device.your-tailnet.ts.net
```

`ensure` waits up to 30 seconds for the local DevSpace health identity, then
independently requires the same exact PID/command/package-hash proof used by the
watchdog. It reuses a matching Funnel or creates the exact mapping only after
that proof, reads back any change, verifies again before `READY`, and turns off
only the exact matching slot if listener identity becomes untrusted. It refuses
a conflicting mapping and does not start DevSpace, change roots, register
startup tasks, or touch ChatGPT settings or app registration.

## Manual ChatGPT registration

Enable Developer Mode in ChatGPT and manually create the connector:

- Name: `DevSpace`
- MCP URL: `https://your-device.your-tailnet.ts.net/mcp`

Approve the initial Owner-password page when DevSpace asks. This tooling never opens settings, creates/deletes apps, picks permissions, inspects app lists, or selects an app in the composer.

## Read-only diagnosis

```powershell
python skills/chatgpt-workspace-setup/scripts/devspace_tailscale_setup.py doctor --root C:\projects\one --hostname your-device.your-tailnet.ts.net
```

Diagnosis checks the exact local DevSpace `/healthz` identity, then `tailscale funnel status --json`, then the exact public `/healthz` identity. If the endpoint is healthy but a ChatGPT tool call fails, keep the server running and re-check the same manual connector URL; do not automate deletion or re-registration.

Tailscale Funnel makes the endpoint public. It requires Tailnet permissions and uses the device's stable MagicDNS name. Review Tailscale's policy and exposure rules before `--apply`.
