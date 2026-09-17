#!/bin/bash
# Read-only Mac inventory for Zork. No installs.
set -u
BaseUrl="${1:-${ZORK_BASE:-}}"
SessionId="${2:-${ZORK_SID:-}}"
Secret="${3:-${ZORK_SECRET:-}}"
if [[ -z "$BaseUrl" || -z "$SessionId" || -z "$Secret" ]]; then
  echo "Zork Helper: mangler tilkobling."
  exit 1
fi
BaseUrl="${BaseUrl%/}"

json_str() {
  local s=${1-}
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\n'/ }
  s=${s//$'\r'/}
  printf '"%s"' "$s"
}

json_bool() {
  if [[ "${1:-}" == "1" ]]; then printf true; else printf false; fi
}

send() {
  local path=$1
  local body=$2
  curl -fsS -X POST \
    -H "Content-Type: application/json; charset=utf-8" \
    -H "X-Helper-Secret: $Secret" \
    --data-binary "$body" \
    "$BaseUrl$path" >/dev/null 2>&1 || true
}

progress() {
  local text=$1
  printf '%s\n' "  $text"
  send "/api/helper/$SessionId/progress" "{\"text\":$(json_str "$text")}"
}

fail() {
  local msg=$1
  echo "  Feil: $msg"
  send "/api/helper/$SessionId/error" "{\"message\":$(json_str "$msg")}"
  exit 1
}

app_path() {
  local name
  for name in "$@"; do
    if [[ -d "/Applications/$name" ]]; then
      printf '%s' "/Applications/$name"
      return 0
    fi
    if [[ -d "$HOME/Applications/$name" ]]; then
      printf '%s' "$HOME/Applications/$name"
      return 0
    fi
  done
  return 1
}

app_ver() {
  local path=${1-}
  if [[ -z "$path" || ! -d "$path" ]]; then
    printf ''
    return
  fi
  defaults read "$path/Contents/Info" CFBundleShortVersionString 2>/dev/null || printf ''
}

ITEMS=""
add_item() {
  local id=$1 name=$2 found=$3 ver=$4 ok=$5 note=$6
  local json
  json="{\"id\":$(json_str "$id"),\"name\":$(json_str "$name"),\"found\":$(json_bool "$found"),\"version\":$(json_str "$ver"),\"ok\":$(json_bool "$ok"),\"note\":$(json_str "$note"),\"reboot\":false}"
  if [[ -n "$ITEMS" ]]; then
    ITEMS="$ITEMS,"
  fi
  ITEMS="$ITEMS$json"
}

add_app() {
  local id=$1 name=$2
  shift 2
  local path ver found=0 ok=0 note="må installeres"
  path="$(app_path "$@" || true)"
  if [[ -n "$path" ]]; then
    found=1
    ok=1
    ver="$(app_ver "$path")"
    note="i orden"
  else
    ver=""
  fi
  add_item "$id" "$name" "$found" "$ver" "$ok" "$note"
}

progress "Kobler til..."
send "/api/helper/$SessionId/connect" "{}"

progress "Sjekker programmer..."
WORD_PATH="$(app_path "Microsoft Word.app" || true)"
if [[ -n "$WORD_PATH" ]]; then
  add_item "office" "Microsoft 365 / Office" 1 "$(app_ver "$WORD_PATH")" 1 "i orden"
else
  add_item "office" "Microsoft 365 / Office" 0 "" 0 "må installeres"
fi
add_app word Word "Microsoft Word.app"
add_app excel Excel "Microsoft Excel.app"
add_app powerpoint PowerPoint "Microsoft PowerPoint.app"
add_app outlook Outlook "Microsoft Outlook.app"
add_app onenote OneNote "Microsoft OneNote.app"
add_app onedrive OneDrive "OneDrive.app" "Microsoft OneDrive.app"

progress "Sjekker Teams..."
TEAMS_WORK="$(app_path "Microsoft Teams (work or school).app" || true)"
TEAMS_ANY="$(app_path "Microsoft Teams.app" "Microsoft Teams classic.app" || true)"
if [[ -n "$TEAMS_WORK" ]]; then
  add_item "teams" "Teams (jobb eller skole)" 1 "$(app_ver "$TEAMS_WORK")" 1 "i orden"
elif [[ -n "$TEAMS_ANY" ]]; then
  add_item "teams" "Teams (jobb eller skole)" 1 "$(app_ver "$TEAMS_ANY")" 0 "personlig Teams er installert — skolekonto trenger Teams (jobb eller skole)"
else
  add_item "teams" "Teams (jobb eller skole)" 0 "" 0 "må installeres (personlig Teams teller ikke)"
fi

progress "Sjekker AppsAnywhere..."
add_app appsanywhere AppsAnywhere "AppsAnywhere.app"

progress "Sjekker printer..."
PR_PATH="$(app_path "YSoft SafeQ Client.app" "SafeQ.app" "FlexiSpooler.app" "Viken Print.app" || true)"
if [[ -z "$PR_PATH" ]]; then
  if [[ -d "/Library/Application Support/YSoft" || -d "/Library/Application Support/SafeQ" || -d "/usr/local/safeq" ]]; then
    PR_PATH="safeq"
  fi
fi
if [[ -n "$PR_PATH" ]]; then
  add_item "printer" "printer" 1 "$(app_ver "$PR_PATH")" 1 "i orden"
else
  add_item "printer" "printer" 0 "" 0 "må installeres"
fi

progress "Sjekker macOS-oppdateringer (kan ta et minutt)..."
SU_OUT=""
SU_STAT=0
set +e
SU_OUT="$(softwareupdate -l 2>&1)"
SU_STAT=$?
SU_NOTE="Ingen ventende oppdateringer"
SU_OK=1
if echo "$SU_OUT" | grep -qiE 'No new software available|Ingen ny programvare'; then
  SU_OK=1
  SU_NOTE="Ingen ventende oppdateringer"
elif echo "$SU_OUT" | grep -qiE 'Software Update found|found the following|Label:|\* Label'; then
  SU_OK=0
  SU_N="$(echo "$SU_OUT" | grep -cE '^\s*\*|Label:' || true)"
  SU_HEAD="$(echo "$SU_OUT" | grep -E 'Label:|Title:' | head -n 5 | tr '\n' ';' | sed 's/; *$//')"
  if [[ "${#SU_HEAD}" -gt 160 ]]; then
    SU_HEAD="${SU_HEAD:0:160}"
  fi
  if [[ -n "$SU_N" && "$SU_N" -gt 0 ]]; then
    SU_NOTE="$SU_N ventende"
  else
    SU_NOTE="Ventende oppdateringer"
  fi
  if [[ -n "$SU_HEAD" ]]; then
    SU_NOTE="$SU_NOTE. $SU_HEAD"
  fi
elif [[ "$SU_STAT" -ne 0 ]]; then
  SU_OK=0
  SU_CLIP="$SU_OUT"
  SU_CLIP="$(echo "$SU_CLIP" | tr '\n' ' ')"
  if [[ "${#SU_CLIP}" -gt 160 ]]; then
    SU_CLIP="${SU_CLIP:0:160}"
  fi
  SU_NOTE="Klarte ikke å sjekke: $SU_CLIP"
fi
add_item "macupdate" "macOS-oppdateringer" 1 "" "$SU_OK" "$SU_NOTE"

HOST="$(scutil --get ComputerName 2>/dev/null || hostname)"
OS="$(printf '%s %s' "$(sw_vers -productName 2>/dev/null)" "$(sw_vers -productVersion 2>/dev/null)" | sed 's/  */ /g')"
ADMIN=0
if id -Gn 2>/dev/null | grep -qw admin; then
  ADMIN=1
fi
USER="$(id -un 2>/dev/null || printf '')"
NOW="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

BODY="{\"data\":{\"host\":$(json_str "$HOST"),\"os\":$(json_str "$OS"),\"admin\":$(json_bool "$ADMIN"),\"user\":$(json_str "$USER"),\"scanned_at\":$(json_str "$NOW"),\"items\":[$ITEMS]}}"
progress "Sender resultatet..."
if ! curl -fsS -X POST \
  -H "Content-Type: application/json; charset=utf-8" \
  -H "X-Helper-Secret: $Secret" \
  --data-binary "$BODY" \
  "$BaseUrl/api/helper/$SessionId/result" >/dev/null; then
  fail "Klarte ikke å sende resultatet."
fi
progress "Ferdig"
echo ""
echo "  Sjekken er ferdig. Du kan lukke dette vinduet."
sleep 2
exit 0
