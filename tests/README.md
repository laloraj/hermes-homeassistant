# Reliability tests

Run with Python 3.11+ and PyYAML installed:

```sh
python -m unittest discover -s tests -v
for script in hermes_assistant/*.sh; do bash -n "$script"; done
```

The suite uses disposable homes, mock providers/CLIs and loopback ports. With
`/usr/sbin/nginx` and `ttyd` installed it also executes real ingress, terminal,
helper-down and sanitized `env -i` startup rigs. Those tests are explicitly
skipped when the binaries are absent; inspect skips before claiming integration
coverage. No test should mutate the live add-on or use its credentials.

Coverage includes strict gateway-running/Webapp/bundle readiness; bounded startup
and update grace; broken/malformed HTTP; ticket timeout/nonzero; helper/gateway
failure with independent recovery/logs/terminal; invalid YAML/optional MCP;
command/build/health/update soft failures; scoped gateway termination; SQLite WAL
snapshot/integrity/refusal; config/auth/plugin/old-chat/wiki-git restoration; dynamic
required/optional backup paths; portable PM intent, declines and reconstruction.

Pre-PM migration regressions use archived source from the actual original image
seed (`v2026.8.13`) and a local-only git origin. They execute manual and one-shot
boot upgrades, snapshot/PM/build failure rollback and retries, including the
old boot's stale recipe and preserved tracked edits. Fixture provenance and the
supported-bootstrap stand-in boundary are documented in `fixtures/README.md`.
Legacy recovery needs a successful snapshot plus network access to upstream
main and PM packages: use `auto_update_agent` or `hermes-safe-update.sh --now`.
Do not delete `/data`, reset the checkout, or run the old core's pip updater.
After failed migration/legacy rollback, code and personal data are restored but
the gateway stays held behind recovery until an explicit upgrade succeeds.

## Disposable image gate

Both workflows invoke `image_boot.py` inside freshly built disposable containers
with `HERMES_IMAGE_SMOKE=1`. It requires an empty `/data/hermes` and starts the
actual `/run.sh` with sanitized boot PATH and no credentials. It verifies seed
relocation, supported runtime, YAML config, real bundle build, running gateway,
HTTP JS asset, one-use launch ticket, terminal and recovery. CLI help/version
alone is not adequate. The retag release workflow verifies **both architectures
before pushing either version/latest image**. This smoke cannot run on an HAOS
host without Docker; CI remains a release prerequisite.

## Opt-in real PM cold reconstruction

```sh
HERMES_PM_SMOKE=1 /usr/bin/python3 tests/pm_cold_boot.py
```

This network/disk-intensive test reads only the installed source/PM intent and a
supported managed Python bootstrap from `PM_SOURCE_HOME` (default `/data/hermes`).
It archives tracked source and exports the portable recipe into a private sterile
home, then exercises the add-on's actual `addon_runtime.py` to download/rebuild
all recorded tools and extras. It verifies selected extras/tool versions, cold
imports, and a second run with an unavailable bootstrap (must be a fast no-op).
It never copies credentials/config/chats, launches a gateway or mutates live
source/state. All temporary source/environments/downloads are removed on exit.
