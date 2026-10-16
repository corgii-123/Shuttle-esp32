#!/usr/bin/env bash
# Make a headless Mac survive power cuts and never sleep. Run once with sudo.
set -euo pipefail
[[ $(uname) == Darwin ]] || { echo "macOS only"; exit 1; }
echo "== power management (pmset)"
sudo pmset -a sleep 0            # never system-sleep
sudo pmset -a disksleep 0
sudo pmset -a displaysleep 10    # the display may sleep
sudo pmset -a powernap 0
sudo pmset -a autorestart 1      # boot again after a power failure (desktops: Mac mini/Studio)
sudo pmset -a womp 1             # wake on network access
sudo pmset -a tcpkeepalive 1
sudo systemsetup -setrestartfreeze on 2>/dev/null || true   # reboot if the system freezes
sudo systemsetup -setcomputersleep Never 2>/dev/null || true
pmset -g
cat <<'TXT'

Also check manually (GUI):
  * System Settings > Energy (Mac mini): "Prevent automatic sleeping" ON, "Start up automatically
    after a power failure" ON, "Wake for network access" ON.
  * FileVault: when ON, a reboot stops at the pre-boot unlock screen and NOTHING starts (no
    Tailscale, no server) until someone types the password. For unattended reboots disable
    FileVault, or use `sudo fdesetup authrestart` for planned restarts.
  * Automatic login is NOT required (the server is a LaunchDaemon), but is needed if you choose
    install.sh --agent.
  * Software Update: disable automatic macOS installs (they reboot at random times).
TXT
