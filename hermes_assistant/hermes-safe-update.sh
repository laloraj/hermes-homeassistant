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
set -u
HERMES_HOME="${HERMES_HOME:-/data/hermes}"
AGENT_DIR="${AGENT_DIR:-$HERMES_HOME/agent-code}"
SNAP_ROOT="$HERMES_HOME/backups/pre-update"
MARKER="$HERMES_HOME/.update-pending"
PAUSE="$HERMES_HOME/.update-running"
LOG="$HERMES_HOME/logs/safe-update.log"
KEEP=3
API_PORT="${API_PORT:-8642}"
export HERMES_HOME
mkdir -p "$SNAP_ROOT" "$(dirname "$LOG")"

log()    { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }
notify() { [ -x "$HERMES_HOME/tools/bin/tg-notify" ] && "$HERMES_HOME/tools/bin/tg-notify" "$1" >/dev/null 2>&1; log "NOTIFY: $1"; }
hermes_cmd() { if [ -x "$AGENT_DIR/venv/bin/python" ]; then "$AGENT_DIR/venv/bin/python" "$AGENT_DIR/hermes" "$@"; else hermes "$@"; fi; }
version() { hermes_cmd --version 2>/dev/null | head -1; }

# Everything personal that must survive. Single source of truth: the backups skill's include list.
include_list() {
  python3 - <<'EOF'
import json, os
home = os.environ["HERMES_HOME"]
fallback = ["config.yaml", "SOUL.md", ".env", "auth.json", "memories", "wiki", "skills", "scripts", "cron",
            "hooks", "platforms", "secrets", "tracking", "receipts", "bill-analysis", "channel_directory.json",
            "pairing", "state.db", "response_store.db", "kanban.db", "tools/bin", "tools/gog-home",
            "tools/ha-update", "whatsapp/session", "profiles"]
try:
    cfg = json.load(open(os.path.join(home, "skills/devops/backups/config.json")))
    items = cfg.get("hermes_include") or []
except Exception:
    items = []
seen = []
for p in list(items) + fallback:
    if p not in seen and os.path.exists(os.path.join(home, p)):
        seen.append(p)
print("\n".join(seen))
EOF
}

backup_db() {
  # A consistent copy of a live SQLite database (a plain cp can catch it mid-write).
  python3 - "$1" "$2" <<'PYEOF'
import sqlite3, sys
src = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True, timeout=30)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
src.close()
PYEOF
}

snapshot() {
  local ts dir; ts=$(date +%Y%m%d-%H%M%S); dir="$SNAP_ROOT/$ts"; mkdir -p "$dir"
  # Commit and push any wiki edits first so git also holds the latest copy.
  if [ -d "$HERMES_HOME/wiki/.git" ]; then
    git -C "$HERMES_HOME/wiki" add -A >/dev/null 2>&1
    git -C "$HERMES_HOME/wiki" commit -qm "wiki: snapshot before Hermes update" >/dev/null 2>&1 || true
    git -C "$HERMES_HOME/wiki" push -q >/dev/null 2>&1 || true
  fi
  while IFS= read -r p; do
    mkdir -p "$dir/$(dirname "$p")"
    case "$p" in
      state.db|response_store.db|kanban.db) backup_db "$HERMES_HOME/$p" "$dir/$p" || log "WARN: could not back up $p" ;;
      *) cp -a "$HERMES_HOME/$p" "$dir/$p" 2>/dev/null || log "WARN: could not copy $p" ;;
    esac
  done < <(include_list)
  git -C "$AGENT_DIR" rev-parse HEAD > "$dir/.code-rev" 2>/dev/null
  version > "$dir/.version"
  # An integrity check: the snapshot must hold the files that matter most.
  for must in memories wiki skills config.yaml; do
    [ -e "$dir/$must" ] || { log "ERROR: snapshot is missing $must"; echo ""; return 1; }
  done
  # Keep only the newest $KEEP snapshots.
  ls -1d "$SNAP_ROOT"/*/ 2>/dev/null | sort | head -n -"$KEEP" | xargs -r rm -rf
  echo "$dir"
}

ensure_git_main() {
  # The image pins a release tag (detached HEAD), so `hermes update` finds no branch. Give it one.
  git -C "$AGENT_DIR" config --get-all remote.origin.fetch | grep -q 'heads/main' \
    || git -C "$AGENT_DIR" config --add remote.origin.fetch '+refs/heads/main:refs/remotes/origin/main'
}

patch_old_python() {
  # Python < 3.11.4 has no tarfile extraction filters; the package manager needs them. Idempotent.
  local f="$AGENT_DIR/pm/store.py"
  [ -f "$f" ] || return 0
  grep -q 'hasattr(tarfile, "data_filter")' "$f" && return 0
  python3 - "$f" <<'EOF'
import sys
p = sys.argv[1]; s = open(p).read()
old = "        tf.extractall(dest, filter=lambda member, path: _tar_filter(member, real_dest))"
new = ('        if not hasattr(tarfile, "data_filter"):  # Python < 3.11.4: no extraction filters\n'
       '            tf.extractall(dest, members=[m for m in tf if not (m.issym() and os.path.isabs(m.linkname))'
       ' and not os.path.isabs(m.name) and ".." not in m.name.split("/")])\n'
       '        else:\n'
       '            tf.extractall(dest, filter=lambda member, path: _tar_filter(member, real_dest))')
if old in s:
    open(p, "w").write(s.replace(old, new)); print("patched", p)
EOF
}

ensure_copilot_plugin() {
  # v0.22 moved the Copilot provider into a plugin; without it `model.provider: copilot` is rejected.
  grep -q 'provider: copilot' "$HERMES_HOME/config.yaml" 2>/dev/null || return 0
  [ -d "$HERMES_HOME/plugins/github-copilot" ] || hermes_cmd plugins install github-copilot >>"$LOG" 2>&1 || true
  hermes_cmd plugins enable github-copilot >>"$LOG" 2>&1 || true
}

do_update() {
  ensure_git_main
  local out; out=$(hermes_cmd update 2>&1); echo "$out" >> "$LOG"
  if echo "$out" | grep -q -E "extractall|install failed|did not finish"; then
    log "updater hit the old-Python problem; applying the compatibility patch and installing dependencies"
    patch_old_python
    hermes_cmd pm install >>"$LOG" 2>&1 || { log "ERROR: pm install failed"; return 1; }
  fi
  ensure_copilot_plugin
  return 0
}

gateway_pids() {
  # PIDs of the gateway that belongs to THIS $HERMES_HOME (a test copy must never touch the live one).
  local p c
  for p in /proc/[0-9]*; do
    c=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null)
    case "$c" in *"hermes gateway run"*)
      tr '\0' '\n' < "$p/environ" 2>/dev/null | grep -qx "HERMES_HOME=$HERMES_HOME" && echo "${p#/proc/}";;
    esac
  done
}

stop_gateway() {
  # Stop it and WAIT until it has exited, so nothing writes to the databases or imports code while files
  # are swapped. The respawn loop in run.sh waits while $PAUSE exists.
  touch "$PAUSE"
  local i pids
  for i in $(seq 1 30); do pids=$(gateway_pids); [ -z "$pids" ] && return 0; kill $pids 2>/dev/null; sleep 1; done
  pids=$(gateway_pids); [ -n "$pids" ] && { kill -9 $pids 2>/dev/null; sleep 2; }
}

start_gateway() { rm -f "$PAUSE"; }   # the respawn loop starts it again within ~10s

health_check() {
  # A real check: gateway port up, then a real model call through the API server.
  local key_file="$HERMES_HOME/secrets/hermes_api_server_key" i
  for i in $(seq 1 60); do (echo > /dev/tcp/127.0.0.1/"$API_PORT") 2>/dev/null && break; sleep 5; done
  (echo > /dev/tcp/127.0.0.1/"$API_PORT") 2>/dev/null || { log "health: gateway port $API_PORT never opened"; return 1; }
  [ -r "$key_file" ] || { log "health: no API key file, port check only"; return 0; }
  local r; r=$(curl -s -m 120 -H "Authorization: Bearer $(cat "$key_file")" -H "Content-Type: application/json" \
      "http://127.0.0.1:$API_PORT/v1/chat/completions" \
      -d '{"model":"hermes-agent","messages":[{"role":"user","content":"reply with just: ok"}]}')
  echo "$r" | grep -qi '"content": *"ok' && return 0
  log "health: model call failed: ${r:0:200}"; return 1
}

do_rollback() {
  local dir="${1:-$(ls -1d "$SNAP_ROOT"/*/ 2>/dev/null | sort | tail -1)}"; dir="${dir%/}"
  [ -d "$dir" ] || { log "ERROR: no snapshot to restore"; return 1; }
  log "rolling back to $dir ($(cat "$dir/.version" 2>/dev/null))"
  stop_gateway
  local rev; rev=$(cat "$dir/.code-rev" 2>/dev/null)
  [ -n "$rev" ] && git -C "$AGENT_DIR" checkout -qf "$rev" 2>>"$LOG"
  # Restore only the files an update can change. Replace atomically (copy beside, then rename) and drop
  # stale -wal/-shm/-journal files so a restored database is never mixed with the failed one's pages.
  local p
  for p in config.yaml .env auth.json state.db response_store.db kanban.db; do
    [ -e "$dir/$p" ] || continue
    rm -f "$HERMES_HOME/$p-wal" "$HERMES_HOME/$p-shm" "$HERMES_HOME/$p-journal"
    cp -a "$dir/$p" "$HERMES_HOME/$p.restore.tmp" && mv -f "$HERMES_HOME/$p.restore.tmp" "$HERMES_HOME/$p"
  done
  [ -d "$dir/cron" ] && cp -a "$dir/cron/." "$HERMES_HOME/cron/" 2>>"$LOG"
  rm -rf "$HERMES_HOME/plugins/github-copilot"
  python3 - "$HERMES_HOME/state.db" <<'PYEOF' || log "WARN: restored state.db failed its integrity check"
import sqlite3, sys
r = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True).execute("pragma integrity_check").fetchone()[0]
sys.exit(0 if r == "ok" else 1)
PYEOF
  rm -f "$MARKER"; start_gateway
  log "rollback done; code at ${rev:0:10}"
}

reset_toggle() {
  # One-shot: switch auto_update_agent back off through the Supervisor so the next restart does not retry.
  # Sends the full option set back (the API replaces options), changing only that one key.
  [ -n "${SUPERVISOR_TOKEN:-}" ] || { log "toggle: no SUPERVISOR_TOKEN, leaving auto_update_agent as is"; return 0; }
  python3 - <<'PYEOF'
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

case "${1:-}" in
  --snapshot-only) snapshot ;;
  --rollback)      do_rollback "${2:-}" ;;
  --reset-toggle)  reset_toggle ;;
  --boot)
    before=$(version); snap=$(snapshot) || { notify "Hermes update skipped: snapshot failed"; exit 1; }
    # Normally no gateway exists yet at boot; stop any that does so code never changes under a running one.
    stop_gateway
    do_update || { notify "Hermes update failed before restart; rolling back"; do_rollback "$snap"; exit 1; }
    after=$(version)
    if [ "$after" = "$before" ] && [ "$(git -C "$AGENT_DIR" rev-parse HEAD 2>/dev/null)" = "$(cat "$snap/.code-rev" 2>/dev/null)" ]; then
      start_gateway; notify "Hermes is already up to date ($after); nothing changed"; exit 0
    fi
    printf '%s\n%s\n' "$snap" "$before" > "$MARKER"; start_gateway; log "updated: $before -> $after; verify pending" ;;
  --verify)
    [ -f "$MARKER" ] || exit 0
    snap=$(sed -n 1p "$MARKER"); before=$(sed -n 2p "$MARKER")
    if health_check; then rm -f "$MARKER"; notify "Hermes updated: $before -> $(version). Snapshot kept at $snap"
    else notify "Hermes update to $(version) failed its health check; rolled back to $before"; do_rollback "$snap"; fi ;;
  --now)
    before=$(version); snap=$(snapshot) || { notify "Hermes update skipped: snapshot failed"; exit 1; }
    log "snapshot $snap; updating from $before"
    stop_gateway
    if do_update; then
      printf '%s\n%s\n' "$snap" "$before" > "$MARKER"; start_gateway; sleep 15; "$0" --verify
    else notify "Hermes update failed; rolling back to $before"; do_rollback "$snap"; fi ;;
  *) sed -n 2,9p "$0"; exit 2 ;;
esac
