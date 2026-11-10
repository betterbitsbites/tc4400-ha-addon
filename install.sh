#!/bin/bash
# Original scraper by Philipp Reichmuth (phrxmd):
#   https://github.com/phrxmd/Technicolor_modem_scrape
# based on scripts by mmiller7. License: CC-BY-NC-SA 4.0
# One-command installer for any systemd-based Linux host (Debian/Ubuntu/Proxmox LXC,
# Fedora, Alpine without systemd is not supported).
#
#   curl -fsSL https://raw.githubusercontent.com/coaxx77/tc4400-ha-addon/main/install.sh | sudo bash
#
# Installs the scraper, asks for modem/MQTT settings, and runs it every 5 minutes
# via a systemd timer. Re-run to change settings; run with --uninstall to remove.
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/coaxx77/tc4400-ha-addon/main"
BIN=/usr/local/bin/technicolor_signal_dump.sh
CONF=/etc/technicolor-modem-scrape.conf
UNIT=technicolor-modem-scrape

[ "$(id -u)" -eq 0 ] || { echo "Please run as root (sudo)."; exit 1; }

if [ "${1:-}" == "--uninstall" ]; then
	systemctl disable --now "${UNIT}.timer" 2>/dev/null || true
	rm -f "/etc/systemd/system/${UNIT}.service" "/etc/systemd/system/${UNIT}.timer" "$BIN"
	systemctl daemon-reload
	echo "Removed. Config left at ${CONF}."
	exit 0
fi

echo "==> Installing dependencies"
if command -v apt-get >/dev/null; then
	apt-get update -qq && apt-get install -y -qq curl mosquitto-clients gawk sed
elif command -v dnf >/dev/null; then
	dnf install -y -q curl mosquitto gawk sed
elif command -v apk >/dev/null; then
	apk add --no-cache bash curl mosquitto-clients gawk sed coreutils
else
	echo "Unknown package manager - install curl, mosquitto_pub, gawk and sed manually."
fi

echo "==> Installing scraper to ${BIN}"
here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
local_copy="${here}/technicolor_modem_scrape/rootfs/usr/bin/technicolor_signal_dump.sh"
if [ -n "$here" ] && [ -f "$local_copy" ]; then
	install -m 0755 "$local_copy" "$BIN"
else
	curl -fsSL "${REPO_RAW}/technicolor_modem_scrape/rootfs/usr/bin/technicolor_signal_dump.sh" -o "$BIN"
	chmod 0755 "$BIN"
fi

# Load previous answers as defaults
MODEM_URL="http://192.168.100.1"; MODEM_USERNAME="admin"; MODEM_PASSWORD='bEn2o#US9s'
MQTT_HOST="127.0.0.1"; MQTT_PORT="1883"; MQTT_USERNAME=""; MQTT_PASSWORD=""; INTERVAL="5min"
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"

ask() { # ask VAR "Prompt" [secret]
	local cur="${!1}" ans
	if [ -n "${3:-}" ]; then
		read -r -s -p "$2 [keep current]: " ans </dev/tty; echo
	else
		read -r -p "$2 [${cur}]: " ans </dev/tty
	fi
	[ -n "$ans" ] && printf -v "$1" '%s' "$ans"
	return 0
}

echo "==> Configuration (press Enter to keep the value in brackets)"
ask MODEM_URL      "Modem URL"
ask MODEM_USERNAME "Modem username"
ask MODEM_PASSWORD "Modem password" secret
ask MQTT_HOST      "MQTT broker host"
ask MQTT_PORT      "MQTT broker port"
ask MQTT_USERNAME  "MQTT username (empty = none)"
ask MQTT_PASSWORD  "MQTT password" secret
ask INTERVAL       "Run interval (systemd time, e.g. 5min, 1h)"

umask 077
{
	printf 'MODEM_URL=%q\n' "$MODEM_URL"
	printf 'MODEM_USERNAME=%q\n' "$MODEM_USERNAME"
	printf 'MODEM_PASSWORD=%q\n' "$MODEM_PASSWORD"
	printf 'MQTT_HOST=%q\n' "$MQTT_HOST"
	printf 'MQTT_PORT=%q\n' "$MQTT_PORT"
	printf 'MQTT_USERNAME=%q\n' "$MQTT_USERNAME"
	printf 'MQTT_PASSWORD=%q\n' "$MQTT_PASSWORD"
	printf 'INTERVAL=%q\n' "$INTERVAL"
} > "$CONF"
umask 022

cat > "/etc/systemd/system/${UNIT}.service" <<EOF
[Unit]
Description=Scrape Technicolor TC4400 modem and publish to MQTT
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=${BIN} ${CONF}
EOF

cat > "/etc/systemd/system/${UNIT}.timer" <<EOF
[Unit]
Description=Run Technicolor modem scraper every ${INTERVAL}

[Timer]
OnBootSec=1min
OnUnitActiveSec=${INTERVAL}

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now "${UNIT}.timer"

echo "==> Test run"
if systemctl start "${UNIT}.service"; then
	echo "OK - the TC4400 device should now appear under Settings > Devices > MQTT in Home Assistant."
else
	echo "Test run failed - see: journalctl -u ${UNIT}.service -n 50"
fi
