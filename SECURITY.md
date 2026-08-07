# Security policy

Report vulnerabilities privately through GitHub Security. Never include credentials, cookies, browser profiles, private MCP URLs, DevSpace owner secrets, OAuth tokens, allowed-root inventories, or user prompt/output data in a public issue.

The active process guard redacts authorization bearer values, `api-key`, `access-key`, `secret`, `password`, `token`, and generic `*_TOKEN` assignments. Keep this generic coverage when changing process evidence or logs.

DevSpace can read and execute within configured project roots. Register only required roots, preserve canonical/symlink boundaries, and verify the HTTPS hostname and Tailscale Funnel exposure. Ordinary GPT runs never mutate ChatGPT app settings or the Funnel configuration.

Local Multi-GPT accepts file inputs only from narrow host-configured `MULTI_GPT_ALLOWED_ROOTS_JSON` roots. Do not publish actual root values, private state, or credential-bearing artifacts.
