#!/bin/bash
# Serve one random file of BYTES bytes over plain HTTP with nginx.
# Run as root on the origin VM by `gnosis_vpn-bench provision`.
# Usage: setup-origin.sh BYTES
set -euo pipefail
BYTES=$1
mkdir -p /srv/gvpn-bench
cat > /etc/nginx/nginx.conf <<'NGINX'
worker_processes 2;
events { worker_connections 1024; }
http {
  access_log /var/log/nginx/gvpn-bench.log combined;
  sendfile on;
  gzip off;
  server {
    listen 80;
    server_name _;
    root /srv/gvpn-bench;
    location = /payload.bin {
      add_header Cache-Control "no-store";
      default_type application/octet-stream;
    }
    location / { return 404; }
  }
}
NGINX
head -c "$BYTES" /dev/urandom > /srv/gvpn-bench/payload.bin
chmod 644 /srv/gvpn-bench/payload.bin
nginx -t
systemctl restart nginx
sha256sum /srv/gvpn-bench/payload.bin | cut -d' ' -f1
