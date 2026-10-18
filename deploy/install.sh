#!/usr/bin/env bash
# One-shot installer for the Shuttle server on macOS.
#
#   deploy/install.sh            # deps + venv + config.yaml + LaunchDaemon (starts at boot)
#   deploy/install.sh --agent    # LaunchAgent instead (starts at login; needs auto-login)
#   deploy/install.sh --no-brew  # skip Homebrew packages
#   deploy/install.sh --uninstall
#
# Idempotent: re-run after `git pull` to update dependencies and restart the service.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SERVER="$REPO/server"
VENV="$SERVER/.venv"
CONFIG="$SERVER/config.yaml"
LOG_DIR="$HOME/Library/Logs/shuttle"
MODE=daemon
BREW=1
UNINSTALL=0
for a in "$@"; do
  case "$a" in
    --agent) MODE=agent ;;
    --no-brew) BREW=0 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n 2,10p "$0"; exit 0 ;;
    *) echo "unknown option $a"; exit 2 ;;
  esac
done

if [[ $MODE == daemon ]]; then
  PLIST=/Library/LaunchDaemons/com.shuttle.server.plist
  WD_PLIST=/Library/LaunchDaemons/com.shuttle.watchdog.plist
  DOMAIN=system
  SUDO=sudo
else
  PLIST="$HOME/Library/LaunchAgents/com.shuttle.server.plist"
  WD_PLIST="$HOME/Library/LaunchAgents/com.shuttle.watchdog.plist"
  DOMAIN="gui/$(id -u)"
  SUDO=
fi

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

unload() {
  for label in com.shuttle.watchdog com.shuttle.server; do
    $SUDO launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    # bootout is asynchronous: an immediate bootstrap fails with "Input/output error".
    for _ in $(seq 1 50); do
      $SUDO launchctl print "$DOMAIN/$label" >/dev/null 2>&1 || break
      sleep 0.2
    done
  done
}

if [[ $UNINSTALL == 1 ]]; then
  say "Removing launchd jobs"
  unload
  $SUDO rm -f "$PLIST" "$WD_PLIST"
  sudo rm -f /etc/newsyslog.d/shuttle.conf 2>/dev/null || true
  echo "Removed. Config ($CONFIG), venv and logs ($LOG_DIR) were kept."
  exit 0
fi

[[ $(uname) == Darwin ]] || echo "WARNING: not macOS - launchd steps will fail (use the venv manually)."

if [[ $BREW == 1 ]]; then
  say "Homebrew packages (python, opus, adb, cloudflared)"
  if ! command -v brew >/dev/null; then
    echo "Homebrew missing: https://brew.sh  (or re-run with --no-brew)"; exit 1
  fi
  brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
  brew list opus >/dev/null 2>&1 || brew install opus
  command -v adb >/dev/null || brew install --cask android-platform-tools
  command -v cloudflared >/dev/null || brew install cloudflared
fi

PY=""
UV_PY="$(command -v uv >/dev/null && uv python find 3.12 2>/dev/null || true)"
for c in python3.13 python3.12 python3.11 /opt/homebrew/bin/python3.12 $UV_PY python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
[[ -n $PY ]] || { echo "Python >= 3.11 not found"; exit 1; }

say "Python venv ($PY -> $VENV)"
[[ -d $VENV ]] || "$PY" -m venv "$VENV"
# A venv made by `uv venv` has no pip: use uv for it (or bootstrap pip).
if [[ ! -x "$VENV/bin/pip" ]] && command -v uv >/dev/null; then
  uv pip install -q --python "$VENV/bin/python" -r "$SERVER/requirements.txt"
else
  [[ -x "$VENV/bin/pip" ]] || "$VENV/bin/python" -m ensurepip --upgrade >/dev/null
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -r "$SERVER/requirements.txt"
fi
(cd "$SERVER" && "$VENV/bin/python" -c "from shuttle_server.audio.opus import lib; lib(); print('libopus OK')") \
  || echo "WARNING: libopus not loadable (brew install opus, or set SHUTTLE_LIBOPUS=/path/libopus.dylib)"

say "Configuration ($CONFIG)"
mkdir -p "$LOG_DIR"
if [[ ! -f $CONFIG ]]; then
  TOKEN="$(openssl rand -hex 24)"
  ADB_PATH="$(command -v adb || echo adb)"
  sed -e "s|^  token: change-me .*|  token: $TOKEN         # MUST equal CONFIG_SHUTTLE_TOKEN in the firmware|" \
      -e "s|^  console: true .*|  console: false|" \
      -e "s|^  path: adb .*|  path: $ADB_PATH|" \
      "$SERVER/config.example.yaml" > "$CONFIG"
  chmod 600 "$CONFIG"
  echo "Created $CONFIG with a new random token:"
  echo "    $TOKEN"
  echo "Put it into firmware/shuttle.local.conf as CONFIG_SHUTTLE_TOKEN=\"$TOKEN\""
else
  echo "Keeping existing $CONFIG"
fi
(cd "$SERVER" && "$VENV/bin/python" -m shuttle_server --config "$CONFIG" --check >/dev/null) && echo "config OK" \
  || { echo "config check failed: (cd $SERVER && $VENV/bin/python -m shuttle_server --config $CONFIG --check)"; exit 1; }
PORT="$("$VENV/bin/python" -c "import sys; sys.path.insert(0, '$SERVER'); from shuttle_server.config import load_config; print(load_config('$CONFIG').server.port)")"

say "launchd ($MODE: $PLIST)"
if [[ $MODE == daemon ]]; then
  USERBLOCK="<key>UserName</key><string>$(id -un)</string><key>GroupName</key><string>staff</string>"
else
  USERBLOCK=""
fi
render() {
  sed -e "s|@@PYTHON@@|$VENV/bin/python|g" -e "s|@@CONFIG@@|$CONFIG|g" -e "s|@@SERVER_DIR@@|$SERVER|g" \
      -e "s|@@HOME@@|$HOME|g" -e "s|@@LOG_DIR@@|$LOG_DIR|g" -e "s|@@REPO@@|$REPO|g" -e "s|@@PORT@@|$PORT|g" \
      -e "s|@@LAUNCHD_TARGET@@|$DOMAIN/com.shuttle.server|g" -e "s|@@USERNAME_BLOCK@@|$USERBLOCK|g" "$1"
}
TMP="$(mktemp -d)"
render "$REPO/deploy/com.shuttle.server.plist.template" > "$TMP/server.plist"
render "$REPO/deploy/com.shuttle.watchdog.plist.template" > "$TMP/watchdog.plist"
plutil -lint "$TMP/server.plist" "$TMP/watchdog.plist" >/dev/null
unload
$SUDO mkdir -p "$(dirname "$PLIST")"
$SUDO cp "$TMP/server.plist" "$PLIST"
$SUDO cp "$TMP/watchdog.plist" "$WD_PLIST"
if [[ $MODE == daemon ]]; then sudo chown root:wheel "$PLIST" "$WD_PLIST"; sudo chmod 644 "$PLIST" "$WD_PLIST"; fi
$SUDO launchctl bootstrap "$DOMAIN" "$PLIST"
$SUDO launchctl bootstrap "$DOMAIN" "$WD_PLIST"
$SUDO launchctl enable "$DOMAIN/com.shuttle.server"

if [[ $MODE == daemon ]]; then
  sed -e "s|@@LOG_DIR@@|$LOG_DIR|g" -e "s|@@USER@@|$(id -un)|g" "$REPO/deploy/newsyslog-shuttle.conf" \
    | sudo tee /etc/newsyslog.d/shuttle.conf >/dev/null
fi

say "Health check"
for i in $(seq 1 15); do
  if curl -fsS -m 3 "http://127.0.0.1:$PORT/health"; then echo; break; fi
  sleep 1
done || true

cat <<TXT

Done. Useful commands:
  tail -f $LOG_DIR/shuttle.log
  $SUDO launchctl kickstart -k $DOMAIN/com.shuttle.server     # restart
  $REPO/deploy/healthcheck.sh                                    # local health
  $VENV/bin/python $REPO/tools/sim_device.py --url ws://127.0.0.1:$PORT --token <token> --say 打开设置

Next (see docs/README.md): Cloudflare Tunnel (deploy/cloudflared-config.example.yml),
Tailscale SSH, and deploy/macos-unattended.sh (no sleep, auto power-on).
TXT
