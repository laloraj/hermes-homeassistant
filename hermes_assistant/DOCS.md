# Hermes Assistant — Webapp-only add-on

This fork uses the stock Hermes **gateway Webapp**, not classic hermes-webui. HA admin Ingress is the browser authentication boundary. No terminal or chat ports are published to the LAN. The fork maps the real HA `/config` and `/addons` writable; treat shell access here as access to your HA installation.

## Setup

1. Set `timezone` and leave `enable_terminal: true` during setup.
2. Start the add-on and open its sidebar panel. The first Webapp build can take several minutes; a retry/recovery page appears meanwhile.
3. Open `<ingress URL>/recovery/`, then **Setup / recovery terminal**. Run `hermes setup` for your model/provider and `hermes model` to switch later. Credentials persist in `/data/hermes`.
4. Return to the root panel. A one-use launch ticket opens the normal Hermes Webapp.

There is no WebUI password, terminal password, floating UI plugin or LAN terminal URL. Terminal and logs are accessible only to admins through Ingress. If disabled, the recovery page explains how to enable the terminal again.

## Add-on options

| Option | Meaning |
|---|---|
| `timezone` | IANA timezone, e.g. `America/Phoenix` |
| `enable_terminal` | Enable the independent setup/recovery terminal (loopback only) |
| `auto_update_agent` | One safe update at next start, then reset to false; not every-start updating |
| `auto_configure_mcp` | Wire HA MCP into Hermes config. Requires HA's MCP Server integration. Other MCP entries are preserved |
| `homeassistant_token` | Optional HA long-lived token. Empty uses the injected Supervisor token |
| `watch_entities` | HA entity IDs to watch; empty watches none |
| `bitwarden_server` | Optional server host/HTTPS URL; empty discovers a running Vaultwarden add-on |
| `bitwarden_email`, `bitwarden_master_password` | Optional Bitwarden CLI login; empty disables it |
| `bitwarden_client_id`, `bitwarden_client_secret` | Optional personal API key if two-step login is enabled |

Vaultwarden remains a **separate add-on**, not an embedded server. The optional CLI uses a separate loopback TLS shim for private HTTP Vaultwarden. Integration failures are logged and do not prevent the gateway or recovery from starting. HA MCP is optional too.

## Routes and health

| Path under the ingress URL | Purpose |
|---|---|
| `/` | Sign into the Webapp when ready; otherwise retry/recovery landing page |
| `/webapp/` | Gateway's stock Hermes Webapp, on loopback `127.0.0.1:9120` |
| `/recovery/` | Static nginx page: retry, terminal, logs, rollback command |
| `/terminal/` | Independent ttyd shell, if enabled |
| `/logs/bootstrap.log` | Runtime reconstruction/bootstrap log |
| `/logs/gateway-addon.log` | Gateway startup/error log |
| `/logs/webapp-build.log` | Webapp build log |
| `/logs/safe-update.log` | Safe-update/rollback log |
| `/logs/webapp-ticket.log` | Front-door helper log |
| `/ready` | Strict readiness: gateway HTTP health, Webapp HTML and a served JS asset |
| `/health` | Watchdog verdict: strict readiness plus finite startup/update grace |

The add-on owns the Webapp's loopback host/port/enabled settings; other Hermes settings are preserved. Configuration is written atomically using system Python + explicitly installed `python3-yaml`. Invalid YAML is not overwritten; repair it in the terminal.

Watchdog is a separate HA toggle and defaults off. `/health` permits at most 15 minutes from initial boot while bootstrap/build starts. Once healthy, normal failures are unhealthy immediately. Safe-update markers permit a bounded update window; stale build or update markers do not grant indefinite grace. Recovery does not depend on these checks and remains available if the gateway, build, provider, or Python ticket helper is down. If nginx itself is down, use HA's add-on Logs and restore an HA backup.

Logs can contain private conversation/tool details; do not share raw logs publicly. Only five fixed log files are served; config/auth/secret files are not HTTP routes. No browser endpoint executes rollback: use the trusted terminal intentionally.

## Safe Hermes updates

Take an HA backup before upgrading the add-on itself. For Hermes core:

- Ask the agent to run `hermes-safe-update.sh --now` detached, or run it in the terminal. The gateway restart ends the current agent chat; Telegram reports the outcome if the existing notification helper is configured.
- Alternatively enable `auto_update_agent`, restart once, and the script resets the toggle afterward.
- The script serializes operations; snapshots data consistently, stops this home's gateway, updates code, rebuilds the Webapp, restarts, then requires `/ready`-equivalent readiness. If an API key file is configured, a real model API request is checked too. An open API port alone never passes.
- Failures attempt automatic rollback and return nonzero; notifications distinguish restored data/code from verified ready service. A failed restore holds the gateway stopped for recovery rather than writing into corrupt databases.
- Do not use bare `hermes update` as a substitute: it bypasses the wrapper's snapshot and rollback safeguards.

Manual recovery, **after stopping other CLI writers**:

```sh
hermes-safe-update.sh --rollback
# Or choose an older complete snapshot:
hermes-safe-update.sh --rollback /data/hermes/backups/pre-update/<snapshot>
```

The last three complete snapshots are retained. Rollback validates snapshot DBs before changing live files, stops and waits for the scoped gateway, restores code/config/auth/personal files/plugins and SQLite data, clears stale DB sidecars, and rebuilds the matching Webapp. The recovery page remains usable throughout. Snapshots are not a substitute for off-device HA backups.

## Data preservation and migration

All persistent state lives under `/data/hermes`. Snapshot selection dynamically incorporates the backups skill's required and optional include lists and always covers config/auth, memories, wiki, skills, scripts, cron, plugins, desktop-plugins, vault, Webapp session-token, UI preferences, canonical/profile SQLite databases, and classic chat history when present. SQLite's backup API includes committed WAL data. PM intent is exported as `pm-reconstruction.json` (enabled/declined extras, tool versions and source-input hashes), alongside small selection facts/inputs and checkout launchers. Startup/rollback reconstruct missing environments with the image’s supported Python 3.14 bootstrap, not system Python 3.11 or weakened tar extraction filters. Runtime binaries are not duplicated; reconstruction requires matching source/locks and may need network access. Legitimate later user extras changes are preserved. Optional existing memory-store/kanban/MCP/native-sign-in state is included too. The wiki working tree and git history are copied without requiring a successful push.

Version 1.10.0 removes the **image** classic seed `/opt/hermes-webui` and no longer mirrors, updates or serves classic WebUI. It never deletes existing `/data/hermes/webui` chats or `/data/hermes/webui-app` code. Old chats are retained for offline recovery; they are not automatically imported into the Webapp. Retired bookmarks open recovery. The deprecated `auto_update_webui` option is removed; if Supervisor reports an unknown old option, remove that option from the Configuration editor before starting.

The agent mirror `/data/hermes/agent-code` remains the active code source and stays in-place upgradable. Existing mirrors are not replaced with the image seed; if yours predates Webapp support (Hermes v0.22.6), use the safe-update command in recovery first. Fresh installs seed v0.22.6, Python 3.14 and Node in the image. Both build paths test a real first mirror, bundle build, running gateway, HTTP asset, launch ticket and terminal—not just CLI help.
