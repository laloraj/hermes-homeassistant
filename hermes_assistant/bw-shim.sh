#!/bin/bash
# The Bitwarden CLI refuses plain http:// servers (hard-coded). The Vaultwarden add-on only speaks http on the private
# add-on network, so this runs a tiny loopback-only TLS front (separate nginx, self-signed cert) that forwards to it.
# Usage: bw-shim.sh start <http-upstream-url> | ensure     (the CLI is told to trust the cert via NODE_EXTRA_CA_CERTS)
export PATH=$PATH:/usr/sbin:/sbin   # the add-on boot PATH has no /usr/sbin (nginx lives there)
D=/data/hermes/tools/bw/shim; PORT=18443
mkdir -p "$D" && chmod 700 "$D"
listening() { (exec 3<>/dev/tcp/127.0.0.1/$PORT) 2>/dev/null; }
start() {
  up=${1%/}; echo "$up" > "$D/upstream"
  [ -s "$D/cert.pem" ] || openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=bw-shim" \
     -addext "subjectAltName=IP:127.0.0.1,DNS:localhost" -keyout "$D/key.pem" -out "$D/cert.pem" >/dev/null 2>&1
  cat > "$D/nginx.conf" <<C
worker_processes 1; pid $D/nginx.pid; error_log $D/error.log warn;
events { worker_connections 64; }
http {
  access_log off; client_max_body_size 100m;
  client_body_temp_path $D/tmp_body; proxy_temp_path $D/tmp_proxy; fastcgi_temp_path $D/tmp_f; uwsgi_temp_path $D/tmp_u; scgi_temp_path $D/tmp_s;
  server { listen 127.0.0.1:$PORT ssl; ssl_certificate $D/cert.pem; ssl_certificate_key $D/key.pem;
    location / { proxy_pass $up; proxy_set_header Host \$host:\$server_port; proxy_set_header X-Forwarded-Proto https; proxy_http_version 1.1; } }
}
C
  if [ -s "$D/nginx.pid" ] && kill -0 "$(cat "$D/nginx.pid")" 2>/dev/null; then nginx -c "$D/nginx.conf" -s reload 2>/dev/null; else nginx -c "$D/nginx.conf" 2>/dev/null; fi
  for i in 1 2 3 4 5 6 7 8 9 10; do listening && return 0; sleep 0.5; done; return 1
}
case "$1" in
  start) start "$2" ;;
  ensure) [ -s "$D/upstream" ] || exit 0; listening || start "$(cat "$D/upstream")" ;;
esac
