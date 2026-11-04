#!/bin/bash
# hermes-safe-update.sh: update Hermes in place with a snapshot, a real health check and automatic rollback.
#
#   --now            snapshot, update, restart gateway, verify (from chat / Terminal tab)
#   --boot           snapshot + update only (run.sh, before the gateway starts); leaves a marker
#   --verify         verify a pending update; roll back on failure (run.sh, after the gateway starts)
#   --rollback [dir] restore the newest (or given) snapshot and the previous code revision
#   --snapshot-only  take the snapshot and stop
#
# Personal data is never touched by the update itself: it lives in $HERMES_HOME, outside the git checkout
# ($AGENT_DIR). The snapshot is a second copy of it, so a failed update cannot lose memory, wiki or scripts.
set -uo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH}"
HERMES_HOME="${HERMES_HOME:-/data/hermes}"
AGENT_DIR="${AGENT_DIR:-$HERMES_HOME/agent-code}"
SNAP_ROOT="$HERMES_HOME/backups/pre-update"
MARKER="$HERMES_HOME/.update-pending"
PAUSE="$HERMES_HOME/.update-running"
LOG="$HERMES_HOME/logs/safe-update.log"
KEEP=3
API_PORT="${API_PORT:-8642}"
HERMES_BIN="${HERMES_BIN:-/usr/local/bin/hermes}"
HEALTH_SCRIPT="${HEALTH_SCRIPT:-$(dirname "$0")/addon_health.py}"
HEALTH_ATTEMPTS="${HEALTH_ATTEMPTS:-180}"
HEALTH_INTERVAL="${HEALTH_INTERVAL:-5}"
export HERMES_HOME
mkdir -p "$SNAP_ROOT" "$(dirname "$LOG")"

log()    { echo "$(date '+%F %T') $*" | tee -a "$LOG" >&2; }
notify() { [ -x "$HERMES_HOME/tools/bin/tg-notify" ] && "$HERMES_HOME/tools/bin/tg-notify" "$1" >/dev/null 2>&1; log "NOTIFY: $1"; }
hermes_cmd() { "$HERMES_BIN" "$@" 9>&-; }
legacy_source() { [ ! -f "$AGENT_DIR/pm/lock.json" ]; }
version() {
  # Old/broken venvs need not import successfully to snapshot and migrate them.
  if legacy_source; then git -C "$AGENT_DIR" describe --tags --always; return; fi
  local out; out=$(hermes_cmd --version 2>/dev/null) || return 1; printf '%s\n' "${out%%$'\n'*}"
}

# Everything personal that must survive. Single source of truth: the backups skill's include list.
include_list() {
  /usr/bin/python3 - <<'EOF'
import json, os
home = os.environ["HERMES_HOME"]
fallback = ["config.yaml", "SOUL.md", ".env", "auth.json", "memories", "wiki", "skills", "scripts", "cron",
            "hooks", "platforms", "secrets", "tracking", "receipts", "bill-analysis", "channel_directory.json",
            "pairing", "state.db", "response_store.db", "kanban.db", "tools/bin", "tools/gog-home",
            "tools/ha-update", "whatsapp/session", "profiles", "plugins", "desktop-plugins", "webapp/session-token", "vault",
            "projects.db", "ui-preferences.json", "verification_evidence.db", "webui", "sessions",
            "agent-code/.hermes", "tools/facts.json", "pm-reconstruction.json",
            "memory_store.db", "kanban/boards", "mcp-tokens", "native-sign-in-tokens.json", "sign-in-registration.json"]
# PM dependency selection and inputs are small metadata; never snapshot tool binaries/environments.
from pathlib import Path
for selection in Path(home, "installs").glob("*/facts.json"):
    fallback.append(str(selection.relative_to(home)))
for declined in Path(home, "installs").glob("*/declined-extras.json"):
    fallback.append(str(declined.relative_to(home)))
for inputs in Path(home, "installs").glob("*/inputs"):
    fallback.append(str(inputs.relative_to(home)))
try:
    cfg = json.load(open(os.path.join(home, "skills/devops/backups/config.json")))
    items = (cfg.get("hermes_include") or []) + (cfg.get("hermes_optional_include") or [])
except Exception:
    items = []
seen = []
for p in list(items) + fallback:
    if (isinstance(p, str) and not os.path.isabs(p) and ".." not in p.split("/")
            and "\n" not in p and "\r" not in p and p not in seen and os.path.exists(os.path.join(home, p))):
        seen.append(p)
print("\n".join(seen))
EOF
}


snapshot() {
  local ts dir; ts=$(date +%Y%m%d-%H%M%S-%N); dir="$SNAP_ROOT/$ts"
  ( umask 077; mkdir -p "$dir" ) || return 1
  include_list > "$dir/.include" || return 1
  # Copy every SQLite file (including profiles) through its backup API, never raw WAL pages.
  /usr/bin/python3 - "$HERMES_HOME" "$dir" <<'PYEOF' || { rm -rf "$dir"; return 1; }
import os, shutil, sqlite3, sys
from pathlib import Path
home, dest = map(Path, sys.argv[1:])
def copy(src, dst):
    if str(src).endswith(('.db-wal', '.db-shm', '.db-journal')):
        return str(dst)
    if str(src).endswith('.db'):
        with sqlite3.connect('file:%s?mode=ro' % src, uri=True, timeout=30) as source:
            with sqlite3.connect(str(dst)) as target:
                source.backup(target)
                if target.execute('pragma integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('SQLite snapshot integrity failed')
        shutil.copystat(src, dst)
        return str(dst)
    return shutil.copy2(src, dst)
for name in (dest / '.include').read_text().splitlines():
    src, dst = home / name, dest / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True, copy_function=copy, dirs_exist_ok=True)
    else:
        copy(src, dst)
PYEOF
  /usr/bin/python3 "$(dirname "$0")/addon_runtime.py" export --home "$HERMES_HOME" --source "$AGENT_DIR" --target "$dir/pm-reconstruction.json" || { rm -rf "$dir"; return 1; }
  # An earlier failed boot may have written a modern default recipe for old source.
  # It is not legacy reconstruction intent and must not trap rollback in PM.
  if legacy_source; then rm -f "$dir/pm-reconstruction.json"; fi
  if [ -f "$dir/pm-reconstruction.json" ]; then
    grep -qx 'pm-reconstruction.json' "$dir/.include" || printf '%s\n' pm-reconstruction.json >> "$dir/.include"
  fi
  git -C "$AGENT_DIR" rev-parse HEAD > "$dir/.code-rev" 2>/dev/null || { rm -rf "$dir"; return 1; }
  git -C "$AGENT_DIR" diff --binary HEAD > "$dir/.code-diff" || { rm -rf "$dir"; return 1; }
  version > "$dir/.version" || { rm -rf "$dir"; return 1; }
  [ -f "$dir/config.yaml" ] || { log 'ERROR: snapshot missing config.yaml'; rm -rf "$dir"; return 1; }
  # Prune only complete snapshots, after a new valid snapshot exists.
  /usr/bin/python3 - "$SNAP_ROOT" "$KEEP" <<'PYEOF'
import shutil, sys
from pathlib import Path
items = sorted(p for p in Path(sys.argv[1]).iterdir() if (p / '.code-rev').is_file())
for path in items[:-int(sys.argv[2])]:
    shutil.rmtree(path)
PYEOF
  echo "$dir"
}

ensure_git_main() {
  # The image pins a release tag (detached HEAD), so `hermes update` finds no branch. Give it one.
  git -C "$AGENT_DIR" config --get-all remote.origin.fetch | grep -q 'heads/main' \
    || git -C "$AGENT_DIR" config --add remote.origin.fetch '+refs/heads/main:refs/remotes/origin/main'
}

ensure_copilot_plugin() {
  # v0.22 moved the Copilot provider into a plugin; without it `model.provider: copilot` is rejected.
  grep -q 'provider: copilot' "$HERMES_HOME/config.yaml" 2>/dev/null || return 0
  [ -d "$HERMES_HOME/plugins/github-copilot" ] || hermes_cmd plugins install github-copilot >>"$LOG" 2>&1 || true
  hermes_cmd plugins enable github-copilot >>"$LOG" 2>&1 || true
}

fix_launcher() {
  # `venv/bin/hermes` is a pip entry point that imports hermes_cli from the image's baked-in tree
  # (/opt/hermes-agent-code), NOT from the updated checkout in $AGENT_DIR. With the venv first on PATH, every bare
  # `hermes` (the gateway loop, the terminal tab, scripts, the agent's own shell) silently ran the OLD code after an
  # update. Make it hand off to the updater-maintained launcher. Idempotent; safe to run on every boot and update.
  local shim=/usr/local/bin/hermes v="$AGENT_DIR/venv/bin/hermes"
  [ -x "$shim" ] && [ -f "$v" ] || return 0
  grep -q 'venv/bin/hermes' "$shim" 2>/dev/null && return 0     # the shim itself points here: would loop
  grep -q '/usr/local/bin/hermes' "$v" 2>/dev/null && return 0   # already repaired
  printf '#!/bin/sh\nexec /usr/local/bin/hermes "$@"\n' > "$v.new" && chmod +x "$v.new" && mv -f "$v.new" "$v" \
    && log "launcher: venv/bin/hermes now hands off to /usr/local/bin/hermes"
}

do_update() {
  ensure_git_main
  if legacy_source; then
    # Never ask old core's pip updater to bootstrap PM under Python 3.11.
    # Caller has completed the personal/code snapshot and stopped the gateway.
    log 'migration: fetching latest main before supported-bootstrap PM reconstruction'
    git -C "$AGENT_DIR" fetch origin '+refs/heads/main:refs/remotes/origin/main' >>"$LOG" 2>&1 || return 1
    git -C "$AGENT_DIR" checkout -q --detach origin/main >>"$LOG" 2>&1 || return 1
    [ -f "$AGENT_DIR/pm/lock.json" ] || { log 'ERROR: update target has no supported PM lock'; return 1; }
    rm -f "$HERMES_HOME/pm-reconstruction.json"
    /usr/bin/python3 "$(dirname "$0")/addon_runtime.py" ensure --home "$HERMES_HOME" --source "$AGENT_DIR" >>"$LOG" 2>&1 || return 1
    ensure_copilot_plugin
    fix_launcher
    return 0
  fi
  local out rc=0; out=$(hermes_cmd update 2>&1) || rc=$?; echo "$out" >> "$LOG"
  if echo "$out" | grep -qiE 'install failed|did not finish|extractall.*unexpected keyword'; then
    log 'ERROR: updater reported incomplete dependency installation'; return 1
  fi
  if [ "$rc" -ne 0 ]; then
    log "ERROR: updater exited $rc"; return "$rc"
  fi
  /usr/bin/python3 "$(dirname "$0")/addon_runtime.py" export --home "$HERMES_HOME" --source "$AGENT_DIR" --target "$HERMES_HOME/pm-reconstruction.json" || return 1
  ensure_copilot_plugin
  fix_launcher
  return 0
}

gateway_pids() {
  /usr/bin/python3 - <<'PYEOF'
import os
from pathlib import Path
for p in Path('/proc').glob('[0-9]*'):
    try:
        args = (p / 'cmdline').read_bytes().split(b'\0')
        env = (p / 'environ').read_bytes().split(b'\0')
        if any(args[i:i+2] == [b'gateway', b'run'] for i in range(len(args)-1)) and (
            ('HERMES_HOME=' + os.environ['HERMES_HOME']).encode() in env):
            print(p.name)
    except (OSError, PermissionError):
        pass
PYEOF
}

stop_gateway() {
  # Stop it and WAIT until it has exited, so nothing writes to the databases or imports code while files
  # are swapped. The respawn loop in run.sh waits while $PAUSE exists.
  touch "$PAUSE"
  local i pids
  for i in $(seq 1 30); do pids=$(gateway_pids); [ -z "$pids" ] && return 0; kill $pids 2>/dev/null; sleep 1; done
  pids=$(gateway_pids); [ -z "$pids" ] || { kill -9 $pids 2>/dev/null; sleep 2; }
  [ -z "$(gateway_pids)" ] || { log "ERROR: gateway would not stop; refusing data writes"; return 1; }
}

start_gateway() { rm -f "$PAUSE" "$HERMES_HOME/.bootstrap-lock"; }   # release recovery's boot hold too

health_check() {
  local i deadline=$((SECONDS + ${HEALTH_TIMEOUT_SECONDS:-720}))
  for i in $(seq 1 "$HEALTH_ATTEMPTS"); do
    /usr/bin/python3 "$HEALTH_SCRIPT" --ready >/dev/null 2>&1 && break
    if [ "$i" = "$HEALTH_ATTEMPTS" ] || [ "$SECONDS" -ge "$deadline" ]; then
      log 'health: gateway/Webapp HTTP or bundle not ready'; return 1
    fi
    sleep "$HEALTH_INTERVAL"
  done
  # Optional configured API server: exercise inference too, but never accept API-only readiness.
  # No API key means this platform is not configured; Webapp remains mandatory.
  local key_file="$HERMES_HOME/secrets/hermes_api_server_key"
  if [ -r "$key_file" ]; then
    API_PORT="$API_PORT" /usr/bin/python3 - "$key_file" <<'PYEOF' || { log 'health: configured model API check failed'; return 1; }
import json, os, sys, urllib.request
key = open(sys.argv[1]).read().strip()
data = json.dumps({'model':'hermes-agent', 'messages':[{'role':'user','content':'reply with just: ok'}]}).encode()
try:
    req = urllib.request.Request('http://127.0.0.1:%s/v1/chat/completions' % os.environ['API_PORT'],
        data=data, headers={'Authorization':'Bearer '+key, 'Content-Type':'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=120) as response:
        content = json.load(response)['choices'][0]['message']['content']
    sys.exit(0 if isinstance(content, str) and content.strip().lower().rstrip('.') == 'ok' else 1)
except Exception:
    sys.exit(1)  # Provider responses and keys must not leak into logs.
PYEOF
  fi
  return 0
}

do_rollback() {
  local dir="${1:-}"
  if [ -z "$dir" ]; then
    dir=$(/usr/bin/python3 - "$SNAP_ROOT" <<'PYEOF'
import sys
from pathlib import Path
items = sorted(p for p in Path(sys.argv[1]).iterdir()
               if (p / '.code-rev').is_file() and (p / 'config.yaml').is_file())
print(items[-1] if items else '')
PYEOF
)
  fi
  dir="${dir%/}"
  [ -d "$dir" ] && [ -f "$dir/.code-rev" ] || { log 'ERROR: no valid snapshot to restore'; return 1; }
  # Validate every snapshot DB before changing code or live files. Missing optional DBs are fine.
  /usr/bin/python3 - "$dir" <<'PYEOF' || { log 'ERROR: snapshot database integrity failed; restore refused'; return 1; }
import sqlite3, sys
from pathlib import Path
for db in Path(sys.argv[1]).rglob('*.db'):
    with sqlite3.connect('file:%s?mode=ro' % db, uri=True) as conn:
        if conn.execute('pragma integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('invalid snapshot')
PYEOF
  log "rolling back to $dir"
  stop_gateway || return 1
  local rev; rev=$(cat "$dir/.code-rev")
  git -C "$AGENT_DIR" checkout -qf "$rev" 2>>"$LOG" || { log 'ERROR: code rollback failed; gateway held for recovery'; return 1; }
  if [ -s "$dir/.code-diff" ]; then
    git -C "$AGENT_DIR" apply "$dir/.code-diff" 2>>"$LOG" || { log 'ERROR: local code restore failed; gateway held for recovery'; return 1; }
  fi
  # Restore saved personal data as well as credentials/DBs. No deletion of old classic chats or code.
  /usr/bin/python3 - "$dir" "$HERMES_HOME" <<'PYEOF' || { log 'ERROR: restore failed; gateway held for recovery'; return 1; }
import os, shutil, sqlite3, sys
from pathlib import Path
src, home = map(Path, sys.argv[1:])
manifest = src / '.include'
names = manifest.read_text().splitlines() if manifest.exists() else [
    p.name for p in src.iterdir() if not p.name.startswith('.') or p.name == '.env']
for name in names:
    if not name or Path(name).is_absolute() or '..' in Path(name).parts:
        raise ValueError('unsafe snapshot path')
    old, target = src / name, home / name
    if not old.exists():
        continue
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(target.name + '.restore.tmp')
    if staged.is_dir():
        shutil.rmtree(staged)
    elif staged.exists() or staged.is_symlink():
        staged.unlink()
    if old.is_dir():
        shutil.copytree(old, staged, symlinks=True)
        # Gateway is stopped. Keep original until replacement is fully copied.
        previous = target.with_name(target.name + '.restore.previous')
        if previous.exists():
            shutil.rmtree(previous)
        if target.exists():
            os.replace(target, previous)
        os.replace(staged, target)
        if previous.exists():
            shutil.rmtree(previous)
    else:
        shutil.copy2(old, staged)
        for suffix in ('-wal','-shm','-journal') if name.endswith('.db') else ():
            Path(str(target) + suffix).unlink(missing_ok=True)
        os.replace(staged, target)
for db in home.rglob('*.db'):
    if 'agent-code' in db.relative_to(home).parts or 'backups' in db.relative_to(home).parts:
        continue
    with sqlite3.connect('file:%s?mode=ro' % db, uri=True) as conn:
        if conn.execute('pragma integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('restored database invalid')
PYEOF
  # If there were no plugins before this update, remove only our introduced provider plugin.
  [ -d "$dir/plugins" ] || rm -rf "$HERMES_HOME/plugins/github-copilot"
  if legacy_source; then
    # Legacy has no Webapp or --no-supervise. Preserve its untouched venv but
    # hold modern launchers behind recovery until an explicit upgrade succeeds.
    [ -d "$dir/agent-code/.hermes" ] || rm -rf "$AGENT_DIR/.hermes"
    rm -f "$HERMES_HOME/pm-reconstruction.json" "$MARKER" "$PAUSE" "$HERMES_HOME/.webapp-building"
    touch "$HERMES_HOME/.bootstrap-lock" "$HERMES_HOME/.webapp-build-failed"
    log "rollback restored; legacy data/code at ${rev:0:10}; gateway held. Retry hermes-safe-update.sh --now or auto_update_agent"
    return 0
  fi
  if [ -f "$HERMES_HOME/pm-reconstruction.json" ]; then
    /usr/bin/python3 "$(dirname "$0")/addon_runtime.py" ensure --home "$HERMES_HOME" --source "$AGENT_DIR" >>"$LOG" 2>&1 || { log 'ERROR: PM reconstruction failed; gateway held for recovery'; return 1; }
  fi
  fix_launcher
  rm -f "$MARKER" "$HERMES_HOME/.bootstrap-lock" "$HERMES_HOME/.webapp-building" "$HERMES_HOME/.webapp-build-failed"
  # Code changed: ensure a matching browser bundle before calling rollback healthy.
  if ! hermes_cmd webapp --build-only >>"$LOG" 2>&1; then
    touch "$HERMES_HOME/.webapp-build-failed"
    log 'WARN: rollback bundle rebuild failed; recovery remains available'
  fi
  start_gateway
  log "rollback restored; code at ${rev:0:10}; readiness pending"
}

verify_update() {
  [ -f "$MARKER" ] || return 0
  local snap before
  snap=$(sed -n 1p "$MARKER"); before=$(sed -n 2p "$MARKER")
  if health_check; then
    rm -f "$MARKER"; notify "Hermes updated: $before -> $(version). Snapshot kept at $snap"
  else
    notify "Hermes update failed its health check; attempting rollback to $before"
    do_rollback "$snap" || { notify 'Hermes rollback failed; use add-on recovery terminal'; return 1; }
    if health_check; then notify "Hermes rolled back to $before; Webapp ready"
    else notify 'Hermes data/code restored, but Webapp is not ready; use add-on recovery'; fi
    return 1
  fi
}

reset_toggle() {
  # One-shot: switch auto_update_agent back off through the Supervisor so the next restart does not retry.
  # Sends the full option set back (the API replaces options), changing only that one key.
  [ -n "${SUPERVISOR_TOKEN:-}" ] || { log "toggle: no SUPERVISOR_TOKEN, leaving auto_update_agent as is"; return 0; }
  /usr/bin/python3 - <<'PYEOF'
import json, os, sys, urllib.request
base = os.environ.get("SUPERVISOR_URL", "http://supervisor")
hdr = {"Authorization": "Bearer " + os.environ["SUPERVISOR_TOKEN"], "Content-Type": "application/json"}
def call(path, body=None):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=hdr,
                                 method="GET" if body is None else "POST")
    return json.load(urllib.request.urlopen(req, timeout=20))
opts = call("/addons/self/info")["data"]["options"]
if opts.get("auto_update_agent") is False:
    print("toggle already off"); sys.exit(0)
opts["auto_update_agent"] = False
r = call("/addons/self/options", {"options": opts})
print("toggle reset:", r.get("result"))
PYEOF
  [ $? -eq 0 ] || log "WARN: could not reset the auto_update_agent toggle"
}

# Kernel-backed lock releases on exit/crash. Keep one updater/rollback per home.
# --verify is called in-process by --now, avoiding recursive lock acquisition.
exec 9>"$HERMES_HOME/.safe-update.lock"
flock -n 9 || { log 'ERROR: another safe update/rollback is running'; exit 1; }
case "${1:-}" in
  --snapshot-only) snapshot ;;
  --health-check) health_check ;;
  --rollback) do_rollback "${2:-}" ;;
  --reset-toggle) reset_toggle ;;
  --fix-launcher) fix_launcher ;;
  --verify) verify_update ;;
  --boot|--now)
    mode=$1
    [ ! -f "$MARKER" ] || { log 'ERROR: pending update needs --verify or --rollback first'; exit 1; }
    [ ! -f "$PAUSE" ] || { log 'ERROR: interrupted update needs --rollback first'; exit 1; }
    before=$(version) || { log 'ERROR: launcher unavailable; use --rollback from recovery'; exit 1; }
    snap=$(snapshot) || { notify 'Hermes update skipped: snapshot failed'; exit 1; }
    stop_gateway || exit 1
    if ! do_update; then
      notify 'Hermes update failed; attempting rollback'
      do_rollback "$snap" || exit 1
      exit 1
    fi
    after=$(version)
    # Always rebuild/check the Webapp, even when the code revision did not move.
    touch "$HERMES_HOME/.webapp-building"
    if ! hermes_cmd webapp --build-only >>"$LOG" 2>&1; then
      rm -f "$HERMES_HOME/.webapp-building"
      notify 'Hermes Webapp build failed; attempting rollback'
      do_rollback "$snap" || exit 1
      exit 1
    fi
    rm -f "$HERMES_HOME/.webapp-building" "$HERMES_HOME/.webapp-build-failed"
    printf '%s\n%s\n' "$snap" "$before" > "$MARKER"
    start_gateway
    if [ "$mode" = --now ]; then verify_update
    else log "updated: $before -> $after; Webapp readiness verification pending"; fi ;;
  *) sed -n 2,9p "$0"; exit 2 ;;
esac
