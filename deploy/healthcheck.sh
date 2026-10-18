#!/usr/bin/env bash
# Shuttle health check.
#   deploy/healthcheck.sh                     # local check, prints JSON, exit 1 if down
#   deploy/healthcheck.sh https://shuttle.example.com   # check through Cloudflare from anywhere
#   deploy/healthcheck.sh --restart           # (watchdog) restart the service after 3 failures
set -u
URL="${SHUTTLE_HEALTH_URL:-http://127.0.0.1:8780/health}"
RESTART=0
for a in "$@"; do
  case "$a" in
    --restart) RESTART=1 ;;
    http*) URL="${a%/}"; [[ "$URL" == */health ]] || URL="$URL/health" ;;
  esac
done
STATE_FILE="${TMPDIR:-/tmp}/shuttle-health-failures"
if out=$(curl -fsS -m 10 "$URL" 2>&1); then
  echo "$(date '+%F %T') OK $out"
  rm -f "$STATE_FILE"
  exit 0
fi
n=$(( $(cat "$STATE_FILE" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$STATE_FILE"
echo "$(date '+%F %T') FAIL($n) $URL: $out"
if [[ $RESTART == 1 && $n -ge 3 ]]; then
  TARGET="${SHUTTLE_LAUNCHD_TARGET:-system/com.shuttle.server}"
  echo "$(date '+%F %T') restarting $TARGET"
  launchctl kickstart -k "$TARGET" && rm -f "$STATE_FILE"
fi
exit 1
