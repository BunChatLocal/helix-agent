#!/bin/sh
set -eu

mkdir -p /root/.helix

# Seed only an empty volume. Never copy a packed home over live config —
# that replaces production user_overrides with whoever packed the image.
if [ ! -f /root/.helix/config.json ]; then
  if [ -d /seed/helix-home ] && [ -f /seed/helix-home/config.json ]; then
    cp -a /seed/helix-home/. /root/.helix/
  fi
fi

python /app/deploy/prepare_runtime.py

# Optional: join the owner's tailnet for zorken.chat/aihub (web/aihub.py).
# Userspace mode needs no extra container permissions; the app reaches the
# home PC through tailscaled's local proxy on 127.0.0.1:1055.
if [ -n "${TS_AUTHKEY:-}" ] && command -v tailscaled >/dev/null 2>&1; then
  mkdir -p /root/.helix/tailscale
  tailscaled --tun=userspace-networking \
    --state=/root/.helix/tailscale/tailscaled.state \
    --socket=/tmp/tailscaled.sock \
    --socks5-server=127.0.0.1:1055 \
    --outbound-http-proxy-listen=127.0.0.1:1055 >/tmp/tailscaled.log 2>&1 &
  sleep 3
  tailscale --socket=/tmp/tailscaled.sock up --authkey="${TS_AUTHKEY}" \
    --hostname="${TS_HOSTNAME:-zorken-railway}" --accept-dns=false \
    || echo "[aihub] tailscale up failed; /aihub will show 'not reachable'"
  export AIHUB_HTTP_PROXY="${AIHUB_HTTP_PROXY:-http://127.0.0.1:1055}"
fi

exec python /app/main.py
