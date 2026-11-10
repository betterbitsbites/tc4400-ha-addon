# Changelog

All notable changes to this app (add-on) are documented here.

## 1.1.0 — 2026-11-10

### Added
- **Summary sensors** across all channels, published to `.../summary`:
  - Downstream channels / locked channels, upstream channels / locked channels
  - Worst and average downstream SNR
  - Downstream receive power min/max, upstream transmit power min/max
  - Total corrected and uncorrectable errors (`total_increasing`, usable for long-term statistics)
- Example dashboard using only built-in cards: `examples/dashboard.yaml`
- Documentation tab: setup, options, and how to reach the modem through
  pfSense / OPNsense / OpenWrt

## 1.0.1 — 2026-11-10

### Fixed
- Sensors showed **Unknown** after the first start and after every Home Assistant
  restart: state messages are now published as *retained*.
- An empty table cell shifted all following values of a channel into the wrong
  sensors: channel rows are now split strictly on tab boundaries.
- Empty values are sent as `null`, so the JSON payload always stays valid.

## 1.0.0 — 2026-11-10

First release as a Home Assistant app (add-on), based on
[phrxmd/Technicolor_modem_scrape](https://github.com/phrxmd/Technicolor_modem_scrape)
by Philipp Reichmuth.

### Added
- Home Assistant app: configured from the UI, finds the Mosquitto broker and its
  credentials automatically, scrapes in a loop (`scan_interval`, default 300 s).
- One-command installer for plain Linux hosts (`install.sh`, systemd timer).
- `DRY_RUN=1` prints MQTT messages instead of publishing them.
- Support for an MQTT port and for brokers without authentication.
- Exit code 23 when the modem page cannot be parsed.

### Changed
- All settings come from environment variables or a config file; the script no
  longer needs to be edited.
- Uses `mosquitto_pub` from `PATH`; copying binaries into `/config` and the
  `shell_command` + automation setup are no longer needed.

### Fixed
- The script shipped in debug mode (`mqtt_pub_exe="echo mosquitto_pub"`) and did
  not publish anything by default.
- MQTT errors inside the channel loops only ended a subshell; the script now
  aborts properly.
