#!/bin/bash
# Boot step: point the Bitwarden CLI at the user's vault and log in, from add-on options. Never fatal, never logs secrets.
# Server: option bitwarden_server, else auto-discovered Vaultwarden/Bitwarden add-on via the Supervisor (internal network, no DNS/TLS/AdGuard).
# Env (set by run.sh): BW_OPT_SERVER BW_OPT_EMAIL BW_OPT_PASSWORD BW_OPT_CLIENT_ID BW_OPT_CLIENT_SECRET
log() { echo "[bw-setup] $*"; }
SECRETS=/data/hermes/secrets; mkdir -p "$SECRETS" && chmod 700 "$SECRETS"
SUP=${SUPERVISOR_URL:-http://supervisor}
for v in BW_OPT_SERVER BW_OPT_EMAIL BW_OPT_PASSWORD BW_OPT_CLIENT_ID BW_OPT_CLIENT_SECRET; do [ "${!v}" = "null" ] && printf -v "$v" ''; done

if [ -z "$BW_OPT_EMAIL" ] || { [ -z "$BW_OPT_PASSWORD" ]; }; then
  log "Bitwarden not configured (set bitwarden_email and bitwarden_master_password in the add-on options to enable)"; exit 0
fi
command -v bw >/dev/null 2>&1 || { log "bw missing"; exit 0; }
if ! bw --version >/dev/null 2>&1; then
  log "baked Bitwarden CLI not found; installing to /data/hermes/tools/bw (needs internet)"
  mkdir -p /data/hermes/tools/bw && ( cd /data/hermes/tools/bw && PATH=/opt/hermes-agent/node/bin:$PATH npm install @bitwarden/cli >/dev/null 2>&1 )
  bw --version >/dev/null 2>&1 || { log "install failed; Bitwarden disabled"; exit 0; }
fi

# ── server ──
SERVER=$BW_OPT_SERVER
if [ -z "$SERVER" ] && [ -n "$SUPERVISOR_TOKEN" ]; then
  SERVER=$(curl -s -m 10 -H "Authorization: Bearer $SUPERVISOR_TOKEN" "$SUP/addons" | python3 -c '
import sys,json,urllib.request,os
tok=os.environ["SUPERVISOR_TOKEN"];sup=os.environ.get("SUPERVISOR_URL","http://supervisor")
def get(p):
    r=urllib.request.Request(sup+p,headers={"Authorization":"Bearer "+tok});return json.load(urllib.request.urlopen(r,timeout=10))["data"]
try:
    for a in get("/addons")["addons"]:
        n=(a["name"]+" "+a["slug"]).lower()
        if ("vaultwarden" in n or "bitwarden" in n) and a.get("state")=="started":
            i=get("/addons/"+a["slug"]+"/info")
            ports=[k.split("/")[0] for k in (i.get("network") or {})]
            print("http://%s:%s"%(i["hostname"],ports[0] if ports else "80"));break
except Exception: pass' 2>/dev/null)
  [ -n "$SERVER" ] && log "discovered Bitwarden server add-on: $SERVER"
fi
[ -n "$SERVER" ] || { log "no bitwarden_server set and no Vaultwarden add-on found; Bitwarden disabled"; exit 0; }
SERVER=${SERVER%/}
case "$SERVER" in http*) ;; *) SERVER="https://$SERVER";; esac
code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$SERVER/api/config" 2>/dev/null)
[ "$code" = "200" ] || { log "server $SERVER unreachable from the add-on (HTTP $code); check bitwarden_server (needs a valid certificate if https)"; exit 0; }

# ── the CLI only talks https: front an http add-on with the loopback TLS shim ──
case "$SERVER" in
  http://*) /usr/local/bin/bw-shim.sh start "$SERVER" && SERVER="https://127.0.0.1:18443" && log "http add-on fronted by local TLS shim ($SERVER)" \
              || { log "could not start the local TLS shim; Bitwarden disabled"; exit 0; } ;;
  *) rm -f /data/hermes/tools/bw/shim/upstream ;;
esac

# ── switch server if changed ──
cur=$(bw config server 2>/dev/null | tail -1)
if [ "$cur" != "$SERVER" ]; then
  bw logout >/dev/null 2>&1
  bw config server "$SERVER" >/dev/null 2>&1 && log "server set to $SERVER"
fi

# ── master password file (for unattended unlock) ──
if [ ! -f "$SECRETS/bw_master" ] || [ "$(cat "$SECRETS/bw_master")" != "$BW_OPT_PASSWORD" ]; then
  ( umask 077; printf '%s' "$BW_OPT_PASSWORD" > "$SECRETS/bw_master.tmp" ) && mv "$SECRETS/bw_master.tmp" "$SECRETS/bw_master" && chmod 600 "$SECRETS/bw_master"
  rm -f "$SECRETS/bw_session"
fi

# ── login ──
if bw status 2>/dev/null | grep -q unauthenticated; then
  if [ -n "$BW_OPT_CLIENT_ID" ] && [ -n "$BW_OPT_CLIENT_SECRET" ]; then
    BW_CLIENTID=$BW_OPT_CLIENT_ID BW_CLIENTSECRET=$BW_OPT_CLIENT_SECRET bw login --apikey --nointeraction >/dev/null 2>&1 && log "logged in with API key" || log "API key login failed"
  else
    BW_PASSWORD=$BW_OPT_PASSWORD bw login "$BW_OPT_EMAIL" --passwordenv BW_PASSWORD --nointeraction >/dev/null 2>&1 \
      && log "logged in as $BW_OPT_EMAIL" || log "login failed (wrong email/password, or two-step login is on: add bitwarden_client_id/secret)"
  fi
fi
if bw status 2>/dev/null | grep -q unauthenticated; then exit 0; fi
if /usr/local/bin/bw-session >/dev/null 2>&1; then log "vault unlocked and synced; use: export BW_SESSION=\$(bw-session)"; else log "logged in but unlock failed (check bitwarden_master_password)"; fi
exit 0
