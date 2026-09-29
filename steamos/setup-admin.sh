#!/usr/bin/env bash
# One-time admin setup for the Switch 2 controller bridge on SteamOS.
#
#   sudo bash setup-admin.sh            apply
#   sudo bash setup-admin.sh --uninstall   revert
#
# Changes (both live under /etc, which survives SteamOS updates):
#   1. /etc/udev/rules.d/70-switch2-bridge.rules
#        lets the logged-in user open /dev/uhid (create virtual HID devices),
#        needed so Steam can see a native controller with gyro.
#   2. /etc/bluetooth/main.conf  [LE] Min/MaxConnectionInterval
#        default Bluetooth LE link interval 7.5-11.25 ms instead of the kernel's
#        30-50 ms, which cuts controller input delay. Original is backed up.
#      Bluetooth restarts once, so connected Bluetooth devices drop briefly.
set -euo pipefail

RULE=/etc/udev/rules.d/70-switch2-bridge.rules
CONF=/etc/bluetooth/main.conf
BAK=/etc/bluetooth/main.conf.bak-switch2-bridge
MIN=6   # x 1.25 ms = 7.5 ms
MAX=9   # x 1.25 ms = 11.25 ms

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$RULE"
  if [ -f "$BAK" ]; then cp -p "$BAK" "$CONF" && rm -f "$BAK"; echo "restored $CONF"; fi
  udevadm control --reload
  udevadm trigger --action=change /dev/uhid || true
  systemctl restart bluetooth
  echo "Reverted."
  exit 0
fi

echo ">> udev rule for /dev/uhid"
cat > "$RULE" <<'RULE_EOF'
# Switch 2 controller bridge: allow the active local user to create virtual HID devices.
KERNEL=="uhid", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uhid"
RULE_EOF
udevadm control --reload
udevadm trigger --action=change /dev/uhid || true

echo ">> Bluetooth LE connection interval"
grep -q '^\[LE\]' "$CONF" || { echo "no [LE] section in $CONF; not touching it" >&2; exit 1; }
[ -f "$BAK" ] || cp -p "$CONF" "$BAK"
set_key() {  # set_key NAME VALUE : uncomment/replace inside the file, else add under [LE]
  if grep -qE "^#?\s*$1\s*=" "$CONF"; then
    sed -i -E "0,/^#?\s*$1\s*=.*/s//$1=$2/" "$CONF"
  else
    sed -i -E "0,/^\[LE\]/s//[LE]\n$1=$2/" "$CONF"
  fi
}
set_key MinConnectionInterval "$MIN"
set_key MaxConnectionInterval "$MAX"
grep -nE '^(Min|Max)ConnectionInterval=' "$CONF"

echo ">> restarting Bluetooth"
systemctl restart bluetooth

echo
ls -l /dev/uhid
echo "Done. Backup of the Bluetooth config: $BAK"
