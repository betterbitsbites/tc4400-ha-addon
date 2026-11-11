# Changelog

All notable changes to this app (add-on) are documented here.

## 1.7.1 — 2026-11-11

### Added
- App options **`power_switch`** and **`power_sensor`** (entity IDs): an
  alternative to the dropdowns on the TC4400 device page. If set, they take
  precedence; empty = the dropdowns apply. Any on/off entity works for the
  restart (switch, input_boolean, light, …).

## 1.7.0 — 2026-11-11

### Added
- **Restart the modem via a smart plug**: choose the switch under *Modem Power
  Switch* on the TC4400 device; the new **Restart Modem** button switches it
  off, waits `restart_off_seconds` (new option, default 15) and on again,
  checks the result, retries switching on and raises an alarm if the modem
  stays off. 3-minute cooldown. Warning for switches of cloud integrations.
- **Last Power Cycle** sensor: every off → on of the chosen switch, whoever
  triggered it (button, automation/script, user, the plug itself), with the
  source and the off time as attributes. The app follows the switch live and
  fills in the last power cycle from the history after an app restart.
- **Modem Power Sensor** select for the modem's power draw.
- Dashboard section **Modem power**: restart button with confirmation, switch
  state (read-only), power draw with 24 h graph, last power cycle and source,
  last modem boot.

## 1.6.1 — 2026-11-11

### Fixed
- **Modem restarts and counter resets** no longer cause gaps or wrong spikes in
  the error rates. Rates are now calculated per channel: a counter that went
  down was reset, so its current value counts as the errors since the reset
  (never a negative value). A modem reboot is detected from the smaller uptime;
  the rate is then based on the time since the boot. Channels that appear or
  disappear no longer distort the sum.
- Channel table trend: a reset now shows the errors since the reset instead of
  a gap.
- Dashboard error charts (ApexCharts) are built from the reset-safe rate sensors
  instead of differences of the totals, which turned negative after a reboot.
  Press *Create Dashboard* again to update them.

## 1.6.0 — 2026-11-11

### Added
- **Trend sparklines in the channel table**: every downstream row shows its SNR
  and its uncorrectable errors per scrape over the last 30 scrapes, every
  upstream row its transmit power — coloured by the latest value, OFDM in blue.
  New option `history_length` (default 30, `0` = off) and a *Channel History*
  sensor that carries the data.

### Changed
- Channel table: the type is shown next to the ID (only for non-SC-QAM channels)
  to make room; with history enabled the table section is two columns wide.
- Error-rate counters and the channel history are stored in `/data`, so they
  survive app restarts.

## 1.5.0 — 2026-11-11

### Changed
- **SC-QAM and OFDM are summarised separately.** OFDM channels count codewords
  at a far higher rate (billions) and the TC4400 reports their SNR differently,
  which made *Corrected/Uncorrectable Total*, *… per Minute* and the worst SNR
  meaningless. These summary sensors now cover SC-QAM channels only (names end
  in "(SC-QAM)"); downstream power still covers all channels.
- Dashboard: SNR gauge and charts labelled SC-QAM; OFDM SNR shown in blue in the
  spectrum and with 🔵 in the channel table instead of red; status card and
  error charts include the new OFDM sensors.

### Added
- OFDM sensors: *OFDM Channels*, *OFDM SNR Min*, *OFDM Corrected / Uncorrectable
  Codewords* and *… per Minute*.

### Note
- The SC-QAM error totals drop once after the update (OFDM no longer included).
  Home Assistant treats this as a counter reset; error rates show no value for
  one scrape.

## 1.4.1 — 2026-11-11

### Added
- App icon and logo for the app store; screenshots in the README.

### Fixed
- **Internet latency stuck at "Down" / 100 % loss** after a network interruption
  (e.g. WAN or VPN reconnect) while ping itself worked. The probe used the same
  ICMP identifier for every ping, so router/NAT state for it never expired and
  kept pointing to the old path. Every ping now uses a new random identifier,
  and replies are matched by a random token in the payload (robust against NAT
  rewriting the identifier).
- The probe now heals itself: after 30 s without any reply it recreates the
  socket and re-checks the method (falls back to TCP if ICMP stops working).
  Outages and recoveries are written to the app log.

## 1.4.0 — 2026-11-10

### Added
- **Dashboard Target** select on the TC4400 device: choose one of your existing
  dashboards or a new one. The app writes its own **TC4400** view into it and
  leaves all other views untouched (an existing TC4400 view is replaced).
  Auto-generated and YAML dashboards are detected and reported.
- **Reaching the modem's web interface** guide (`MODEM_ACCESS.md`): browser and
  `curl` tests, router setup for pfSense / OPNsense / OpenWrt, static ARP,
  troubleshooting. Linked from the requirements.

### Changed
- Internet latency chart: smoothed 1-minute curves, P95 shading instead of max
  spikes, so single spikes no longer flatten the chart (max, jitter and loss
  stay in the header).
- Channel spectrum: blocks as wide as the channels (like a real spectrum),
  OFDM channels semi-transparent, dashed threshold lines, fixed value ranges,
  tighter layout, hover shows channel ID, type, modulation and width; channels
  without a value are grey.
- Dashboard notifications link directly to the TC4400 view; errors are reported
  as a notification too.

## 1.3.0 — 2026-11-10

### Added
- **Internet latency probe**: pings configurable targets (default `1.1.1.1`) via
  ICMP, with automatic TCP fallback, and publishes average, P95, min, max,
  jitter, loss, base latency, congestion threshold and a status (Line clear /
  Unstable / Congested / Packet loss / Down) per window, as a new
  *Internet Latency* device. New options `latency_*`.
- Dashboard: **Internet latency** section with a status line per target and a
  chart with min–max shading, average, base latency and congestion threshold.

### Changed
- Dashboard: the modem TCP latency section is now called *Modem latency*.

## 1.2.0 — 2026-11-10

### Added
- **Create Dashboard** button on the TC4400 device: builds a complete dashboard
  with one click (also usable as an action: `button.press`, or `mqtt.publish`
  with custom settings). Entity IDs are looked up from the registry by unique ID.
  Uses *plotly-graph* and *apexcharts-card* when installed, built-in cards
  otherwise. New options: `dashboard_title`, `dashboard_url`, `dashboard_cards`,
  `dashboard_hours`.
- Fritz!Box-style **channel spectrum**: power and SNR per channel along the
  frequency band, coloured by thresholds (plotly-graph).
- **Channel table** for all down- and upstream channels with 🟢🟡🔴 markers
  (built-in markdown card).
- New sensors: **TCP latency** to the modem, **last boot** (timestamp),
  **uptime in seconds**, **DOCSIS version**, **software version**,
  **corrected / uncorrectable errors per minute**.
- **Downstream / Upstream Channel List** sensors with all channels as attributes.

### Changed
- The app now has Home Assistant API access (needed to create the dashboard)
  and includes Python 3 (standard library only).

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
