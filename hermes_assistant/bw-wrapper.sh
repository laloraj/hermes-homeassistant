#!/bin/sh
# Bitwarden CLI launcher: baked copy (/opt/bw) first, boot-time fallback install (/data/hermes/tools/bw) second.
# State lives on /data so the login survives restarts.
export PATH=/opt/hermes-agent/node/bin:/data/hermes/tools/node/bin:$PATH
export BITWARDENCLI_APPDATA_DIR=/data/hermes/tools/bw/appdata
mkdir -p "$BITWARDENCLI_APPDATA_DIR" && chmod 700 "$BITWARDENCLI_APPDATA_DIR"
[ -s /data/hermes/tools/bw/shim/upstream ] && /usr/local/bin/bw-shim.sh ensure
[ -s /data/hermes/tools/bw/shim/cert.pem ] && export NODE_EXTRA_CA_CERTS=/data/hermes/tools/bw/shim/cert.pem
for b in /opt/bw/node_modules/.bin/bw /data/hermes/tools/bw/node_modules/.bin/bw; do
  [ -x "$b" ] && exec "$b" "$@"
done
echo "bw: Bitwarden CLI is not installed" >&2; exit 127
