#!/bin/sh
# Enables the BCM SoC hardware watchdog + the systemd runtime watchdog.
# Run on the Pi by `make init`. Reboot once to activate.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"

sudo mkdir -p /etc/systemd/system.conf.d
sudo cp "$HERE/watchdog-system.conf" /etc/systemd/system.conf.d/watchdog.conf

CONFIG=/boot/firmware/config.txt
[ -f "$CONFIG" ] || CONFIG=/boot/config.txt
if ! grep -q '^dtparam=watchdog=on' "$CONFIG"; then
    echo 'dtparam=watchdog=on' | sudo tee -a "$CONFIG" >/dev/null
    echo "Added dtparam=watchdog=on to $CONFIG"
fi

echo "Hardware watchdog configured. Reboot the Pi to activate it."
