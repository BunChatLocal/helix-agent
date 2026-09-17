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

exec python /app/main.py
