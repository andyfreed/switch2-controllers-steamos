#!/usr/bin/env bash
# SteamOS install. No root, nothing written outside your home directory.
#
#   bash steamos/install.sh              set up and start the service
#   bash steamos/install.sh --no-start   set up without (re)starting it
#   bash steamos/install.sh --uninstall  stop and remove the service
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNIT="$HOME/.config/systemd/user/switch2-bridge.service"
cd "$PROJECT_DIR"

if [ "${1:-}" = "--uninstall" ]; then
  systemctl --user disable --now switch2-bridge.service 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload
  echo "Service removed. Paired controllers are listed in ~/.config/nso-gc/config.json."
  exit 0
fi

python3 -c 'import evdev' 2>/dev/null || {
  echo "python-evdev is not available to the system Python. SteamOS ships it by default." >&2
  exit 1
}

echo ">> Python environment (.venv)"
[ -d .venv ] || python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --quiet --disable-pip-version-check --only-binary=:all: "bleak==0.22.3"

echo ">> user service"
mkdir -p "$(dirname "$UNIT")"
sed "s|@PROJECT_DIR@|$PROJECT_DIR|g" steamos/switch2-bridge.service.in > "$UNIT"
systemctl --user daemon-reload
systemctl --user enable switch2-bridge.service
if [ "${1:-}" != "--no-start" ]; then
  systemctl --user restart switch2-bridge.service
fi

cat <<MSG

Installed.
  Pair once:   cd "$PROJECT_DIR" && .venv/bin/python -m ngc pair
               (hold Sync on the controller until the LEDs sweep)
  Then:        systemctl --user restart switch2-bridge.service
  Logs:        journalctl --user -u switch2-bridge.service -f
  Gyro in Steam and a faster Bluetooth link need one admin step:
               sudo bash "$PROJECT_DIR/steamos/setup-admin.sh"
MSG
