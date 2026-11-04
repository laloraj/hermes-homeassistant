# Hermes Home Assistant Add-on (fork)

Runs [Hermes Agent](https://hermes-agent.nousresearch.com/docs) with its **gateway Webapp** behind Home Assistant admin Ingress. There is no separate classic WebUI or custom Hermes interface.

This fork of [nuttaruj/hermes-homeassistant](https://github.com/nuttaruj/hermes-homeassistant) adds safe in-place updates, independent recovery, optional Bitwarden CLI setup, and writable access to the real HA `/config` and `/addons` directories. That access is powerful: anyone able to run code in this add-on can modify your HA configuration.

[![Add this repo](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A//github.com/laloraj/hermes-homeassistant)

## Install and use

1. Add `https://github.com/laloraj/hermes-homeassistant` in the HA Add-on Store repositories, then install **Hermes Assistant**.
2. Set your timezone. Keep `enable_terminal` on for setup/recovery. The Supervisor token is used automatically; a manual HA token is optional.
3. Start the add-on. The first Webapp build can take several minutes.
4. Open the sidebar panel. When ready it signs you into the stock Hermes Webapp using a one-use ticket.
5. For initial model/provider setup, open `<ingress URL>/recovery/` → **Setup / recovery terminal** and run `hermes setup`.

No LAN ports or extra UI passwords are configured. All browser access, including the terminal and logs, goes through HA admin Ingress.

## Recovery and upgrades

- `<ingress URL>/recovery/`: static nginx landing page with terminal, logs and rollback instructions. Independent of the gateway, provider, Webapp build and ticket helper.
- `hermes-safe-update.sh --now`: snapshot, update, build, restart, strict readiness check and automatic rollback on failure. Asking the agent to update uses this same script; it should be launched detached because restarting ends the agent chat.
- `auto_update_agent: true`: update once on the next start, then reset this option to false.
- `hermes-safe-update.sh --rollback`: restore the latest pre-update snapshot without requiring a working agent. Stop other CLI writers first.
- `/ready` is strict gateway/Webapp readiness; `/health` adds a finite startup/update grace for the Supervisor watchdog. Enable Watchdog separately in the add-on settings if desired.

Take an HA backup before an add-on update. Version 1.10.0 stops installing/serving classic WebUI but **does not delete old chats or `/data/hermes/webui-app`**. Those chats are not automatically imported into the Webapp.

[Full configuration, data and recovery reference](hermes_assistant/DOCS.md) · [Changelog](hermes_assistant/CHANGELOG.md)

## Development and release gates

- Supported image architectures: `amd64`, `aarch64`.
- `python3 -m unittest discover -s tests -v` (requires PyYAML; nginx + ttyd exercise real ingress tests when available).
- `for script in hermes_assistant/*.sh; do bash -n "$script"; done`.
- `.github/workflows/retag.yml` patches the upstream base with **all** wrapper files, removes only the unused image classic payload, and seeds Webapp-capable Hermes. Both architecture images must pass the disposable `tests/image_boot.py` fresh bootstrap/build/gateway/ticket smoke before either is published.
- The full-build `hermes_assistant/Dockerfile` is kept consistent and also copies every helper. The runtime config helper explicitly depends on image `python3-yaml`, not a WebUI virtualenv.

License: MIT.
