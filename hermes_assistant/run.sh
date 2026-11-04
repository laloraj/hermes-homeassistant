#!/usr/bin/with-contenv bashio
# Webapp-only add-on wrapper. Recovery/nginx/terminal start before Hermes work.
set -e
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH}"
export HERMES_HOME="${HERMES_HOME:-/data/hermes}"
AGENT_DST="${HERMES_HOME}/agent-code"
AGENT_SRC="${AGENT_SRC:-/opt/hermes-agent-code}"
ADDON_BIN="${ADDON_BIN:-/usr/local/bin}"
SAFE_UPDATE="${ADDON_BIN}/hermes-safe-update.sh"
HERMES_LAUNCHER="${HERMES_BIN:-/usr/local/bin/hermes}"
NGINX_CONFIG="${NGINX_CONFIG:-/etc/nginx/nginx.conf}"
BOOTSTRAP_LOCK="${HERMES_HOME}/.bootstrap-lock"
mkdir -p "${HERMES_HOME}/logs"
rm -f "${HERMES_HOME}/.addon-ready" "${HERMES_HOME}/.webapp-building" "${HERMES_HOME}/.webapp-build-failed"
touch "${HERMES_HOME}/.addon-started" "${BOOTSTRAP_LOCK}"

HA_TOKEN_USER=$(bashio::config 'homeassistant_token')
TZNAME=$(bashio::config 'timezone')
ENABLE_TERMINAL=$(bashio::config 'enable_terminal')
AUTO_UPDATE_AGENT=$(bashio::config 'auto_update_agent')
AUTO_CONFIGURE_MCP=$(bashio::config 'auto_configure_mcp')
export TZ="${TZNAME}"

# Supervisors terminate their own child; never signal any other HERMES_HOME.
children=()
shutdown() {
    trap - EXIT TERM INT
    for child in "${children[@]}"; do kill -TERM "${child}" 2>/dev/null || true; done
    for child in "${children[@]}"; do wait "${child}" 2>/dev/null || true; done
}
trap shutdown EXIT
trap 'exit 0' TERM INT
supervise() (
    set +e
    child=""; napper=""
    trap 'trap - TERM INT; kill -TERM ${child:-} ${napper:-} 2>/dev/null; exit 0' TERM INT
    while true; do
        "$@" & child=$!
        wait "$child"; child=""
        sleep 3 & napper=$!; wait "$napper"; napper=""
    done
)
supervise /usr/bin/python3 "${ADDON_BIN}/webapp-ticket.py" >>"${HERMES_HOME}/logs/webapp-ticket.log" 2>&1 &
children+=("$!")
# nginx serves /recovery/ itself, even when this Python helper is unavailable.
/usr/sbin/nginx -c "${NGINX_CONFIG}" &
NGINX_PID=$!; children+=("$NGINX_PID")
if bashio::var.true "${ENABLE_TERMINAL}"; then
    supervise "${ADDON_BIN}/ttyd" --port 7681 --interface 127.0.0.1 --base-path /terminal \
        --writable --check-origin bash -c '
        export PATH="/usr/local/bin:$HERMES_HOME/agent-code/venv/bin:/opt/hermes-agent/node/bin:$PATH"
        cd "$HERMES_HOME"
        printf "Hermes recovery/setup terminal\nUpdate: hermes-safe-update.sh --now\nRollback: hermes-safe-update.sh --rollback\nLogs: logs/\nSetup: hermes setup\n"
        exec bash' &
    children+=("$!")
fi
bashio::log.info 'Recovery available at /recovery/; terminal and logs do not depend on Hermes.'
# A failed bootstrap/config/integration must not tear down the recovery plane.
set +e

# Only these managed .env entries are refreshed. All user credentials remain.
HA_TOKEN=""; HASS_URL=""
if [ -n "${HA_TOKEN_USER}" ]; then
    HA_TOKEN="${HA_TOKEN_USER}"; HASS_URL=http://homeassistant:8123
elif [ -n "${SUPERVISOR_TOKEN:-}" ]; then
    HA_TOKEN="${SUPERVISOR_TOKEN}"; HASS_URL=http://supervisor/core
fi
ENV_FILE="${HERMES_HOME}/.env"
( umask 077; touch "${ENV_FILE}" )
if [ -n "${HA_TOKEN}" ]; then
    for key in HASS_TOKEN HASS_URL; do sed -i "/^${key}=/d" "${ENV_FILE}"; done
    printf 'HASS_TOKEN=%s\nHASS_URL=%s\n' "${HA_TOKEN}" "${HASS_URL}" >>"${ENV_FILE}"
fi
export CLAUDE_CREDENTIALS_FILE="${CLAUDE_CREDENTIALS_FILE:-/config/claude_credentials.json}"
if [ -f "${CLAUDE_CREDENTIALS_FILE}" ]; then
    OAUTH_TOKEN=$(/usr/bin/python3 -c 'import json, os; print(json.load(open(os.environ["CLAUDE_CREDENTIALS_FILE"])).get("claudeAiOauth", {}).get("accessToken", ""))' 2>/dev/null) || true
    if [ -n "${OAUTH_TOKEN}" ]; then
        sed -i '/^CLAUDE_CODE_OAUTH_TOKEN=/d' "${ENV_FILE}"
        printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "${OAUTH_TOKEN}" >>"${ENV_FILE}"
    fi
fi
chmod 600 "${ENV_FILE}"

# Keep all existing /data, including retired classic chat history and code.
if [ ! -d "${AGENT_DST}" ] && [ -d "${AGENT_SRC}" ]; then
    bashio::log.info 'Mirroring the image agent seed to persistent storage'
    cp -a "${AGENT_SRC}" "${AGENT_DST}"
    for bin in "${AGENT_DST}/venv/bin" "${AGENT_DST}/.venv/bin"; do
        [ ! -d "$bin" ] || find "$bin" -type f -exec sed -i "s|${AGENT_SRC}|${AGENT_DST}|g" {} \; 2>/dev/null || true
    done
fi
AGENT_DIR="${AGENT_DST}"
export AGENT_DIR
export PATH="/usr/local/bin:${AGENT_DIR}/venv/bin:/opt/hermes-agent/node/bin:${PATH}"
RUNTIME_OK=false
if [ -f "${HERMES_HOME}/.update-running" ]; then
    bashio::log.warning 'Interrupted update detected; gateway held. Use recovery terminal: hermes-safe-update.sh --rollback'
elif /usr/bin/python3 "${ADDON_BIN}/addon_runtime.py" ensure --home "${HERMES_HOME}" --source "${AGENT_DIR}" >>"${HERMES_HOME}/logs/bootstrap.log" 2>&1; then
    RUNTIME_OK=true
    rm -f "${BOOTSTRAP_LOCK}"
else
    bashio::log.warning 'Runtime reconstruction failed; gateway held. See recovery bootstrap log.'
fi
if [ -x /opt/hermes-agent/node/bin/node ] && [ ! -e "${HERMES_HOME}/node" ]; then
    ln -s /opt/hermes-agent/node "${HERMES_HOME}/node"
fi
# A pre-PM Python 3.11 checkout cannot reconstruct PM until its source is upgraded.
# Its snapshot-backed updater deliberately does not require the old CLI/runtime.
if [ ! -f "${HERMES_HOME}/.update-running" ] && bashio::var.true "${AUTO_UPDATE_AGENT}" \
        && [ -d "${AGENT_DST}/.git" ] \
        && { [ "$RUNTIME_OK" = true ] || [ ! -f "${AGENT_DIR}/pm/lock.json" ]; }; then
    "${SAFE_UPDATE}" --boot || bashio::log.warning 'Safe update failed; recovery remains available'
    "${SAFE_UPDATE}" --reset-toggle || true
    # Recompute after migration, including failed rollback/hold state. Manual --now
    # clears the same bootstrap hold when recovering after this boot has finished.
    RUNTIME_OK=false
    if [ ! -f "${HERMES_HOME}/.update-running" ] \
            && /usr/bin/python3 "${ADDON_BIN}/addon_runtime.py" ensure --home "${HERMES_HOME}" --source "${AGENT_DIR}" >>"${HERMES_HOME}/logs/bootstrap.log" 2>&1; then
        RUNTIME_OK=true
        rm -f "${BOOTSTRAP_LOCK}"
    fi
fi
"${SAFE_UPDATE}" --fix-launcher || true

HERMES_CONFIG="${HERMES_HOME}/config.yaml"
if [ ! -f "${HERMES_CONFIG}" ]; then
    # Entity IDs are validated by the add-on schema; existing configs untouched.
    {
        echo 'platforms:'
        echo '  homeassistant:'
        [ -n "$HA_TOKEN" ] && echo '    enabled: true' || echo '    enabled: false'
        echo '    extra:'
        echo '      cooldown_seconds: 60'
        echo '      watch_entities:'
        for entity in $(bashio::config 'watch_entities'); do printf '        - %s\n' "$entity"; done
    } >"${HERMES_CONFIG}"
fi
mcp_args=()
bashio::var.true "${AUTO_CONFIGURE_MCP}" && mcp_args+=(--mcp)
HASS_TOKEN="${HA_TOKEN}" HASS_URL="${HASS_URL}" /usr/bin/python3 "${ADDON_BIN}/addon_config.py" \
    "${HERMES_CONFIG}" "${mcp_args[@]}" || bashio::log.warning 'Config setup failed; see recovery terminal'

# Optional password-manager integration stays separate and cannot block startup.
(
    BW_OPT_SERVER=$(bashio::config 'bitwarden_server') BW_OPT_EMAIL=$(bashio::config 'bitwarden_email') \
    BW_OPT_PASSWORD=$(bashio::config 'bitwarden_master_password') BW_OPT_CLIENT_ID=$(bashio::config 'bitwarden_client_id') \
    BW_OPT_CLIENT_SECRET=$(bashio::config 'bitwarden_client_secret') \
        timeout 120 "${ADDON_BIN}/bw-setup.sh" || bashio::log.warning 'Optional Bitwarden setup unavailable; continuing'
) &
children+=("$!")

# Build once per boot; gateway can serve it as soon as the bundle appears.
(
    touch "${HERMES_HOME}/.webapp-building"
    trap 'rm -f "${HERMES_HOME}/.webapp-building"' EXIT
    if [ "$RUNTIME_OK" != true ]; then
        touch "${HERMES_HOME}/.webapp-build-failed"
    elif ! "${HERMES_LAUNCHER}" webapp --build-only >>"${HERMES_HOME}/logs/webapp-build.log" 2>&1; then
        touch "${HERMES_HOME}/.webapp-build-failed"
        bashio::log.warning 'Webapp build failed; open /recovery/ for logs and rollback'
    fi
) &
children+=("$!")
(
    set +e
    gw=""; napper=""
    trap 'trap - TERM INT; kill -TERM ${gw:-} ${napper:-} 2>/dev/null; exit 0' TERM INT
    while true; do
        while [ -f "${HERMES_HOME}/.update-running" ] || [ -f "${BOOTSTRAP_LOCK}" ]; do sleep 2 & napper=$!; wait "$napper"; done
        cd "${HERMES_HOME}"
        "${HERMES_LAUNCHER}" gateway run --no-supervise >>"${HERMES_HOME}/logs/gateway-addon.log" 2>&1 &
        gw=$!; wait "$gw"; gw=""
        sleep 10 & napper=$!; wait "$napper"; napper=""
    done
) &
children+=("$!")
( "${SAFE_UPDATE}" --verify >>"${HERMES_HOME}/logs/safe-update-verify.log" 2>&1 ) &
children+=("$!")
# Bind container lifetime to nginx while retaining cleanup traps.
wait "${NGINX_PID}"
