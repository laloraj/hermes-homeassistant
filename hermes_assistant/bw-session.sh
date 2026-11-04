#!/bin/sh
# Prints a valid BW_SESSION (unlocks unattended from the master password the add-on option wrote to secrets/bw_master).
# Usage in jobs: export BW_SESSION=$(bw-session); bw get password <item>; bw get totp <item>
S=/data/hermes/secrets; F=$S/bw_session
if [ -s "$F" ] && BW_SESSION=$(cat "$F") bw list folders --nointeraction >/dev/null 2>&1; then cat "$F"; exit 0; fi
[ -s "$S/bw_master" ] || { echo "bw-session: no master password (set bitwarden_master_password in the add-on options)" >&2; exit 2; }
case "$(bw status 2>/dev/null)" in *unauthenticated*) echo "bw-session: not logged in (check bitwarden_email / server in the add-on options)" >&2; exit 3;; esac
( umask 077; bw unlock --passwordfile "$S/bw_master" --raw --nointeraction > "$F.tmp" 2>/dev/null ) || { rm -f "$F.tmp"; echo "bw-session: unlock failed (wrong master password?)" >&2; exit 4; }
mv "$F.tmp" "$F"; chmod 600 "$F"; bw sync --nointeraction >/dev/null 2>&1; cat "$F"
