# Pre-PM image seed fixture

`legacy-v2026.8.13.tar.gz` is an unmodified, narrow `git archive` of
NousResearch/hermes-agent tag `v2026.8.13`, commit
`4e693dc685b5716e7da22656eccc6ece37c5db72` (the original add-on seed):

```sh
git archive --format=tar v2026.8.13 hermes hermes_cli/main.py pyproject.toml | gzip -n > legacy-v2026.8.13.tar.gz
```

SHA-256: `fc758c217d5ab31f2a57b831ed642d6cac014fd9ed12806c446dc9513e22d19e`.
The archived CLI has neither `webapp` nor `--no-supervise`; source has no PM
lock. Only these explicitly named regular files are read from the archive.

`test_legacy_migration.py` puts them in a disposable git checkout, adds a failing
Python-3.11/legacy-CLI stand-in, and creates a local git origin containing an
executable modern PM fixture. It runs the actual safe updater, runtime helper
and run.sh migration section, without network, credentials or live data.
The supported Python-3.14 bootstrap is a subprocess stand-in (not an actual
Python-3.14 installation); real supported-Python/tool downloads and full boot
remain covered by the separate PM/image smoke gates. These tests prove
snapshot-before-mutation, manual/one-shot boot success, PM and bundle failure
rollback, preservation of tracked edits/personal data, stale-recipe cleanup,
and released update/bootstrap locks on retry. They do not claim a real
upstream dependency installation or provider/gateway completion.
