#!/bin/bash
# Tiny Mac launcher: fetch the read-only scan and run it.
set -euo pipefail
cd "$(mktemp -d /tmp/zork-helper.XXXXXX)"
BASE='__BASE_URL__'
SID='__SESSION_ID__'
SECRET='__SECRET__'
export ZORK_BASE="$BASE" ZORK_SID="$SID" ZORK_SECRET="$SECRET"
echo "Zork Helper"
echo "Henter sjekken..."
if ! curl -fsSL -H "X-Helper-Secret: $SECRET" "$BASE/api/helper/$SID/script-mac" -o scan.sh; then
  echo "Klarte ikke å hente sjekken. Sjekk at du er på nett og prøv igjen fra Zorken."
  echo "Trykk Enter for å lukke."
  read -r _ || true
  exit 1
fi
/bin/bash scan.sh "$BASE" "$SID" "$SECRET"
