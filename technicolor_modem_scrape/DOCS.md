# Technicolor TC4400 Modem Scraper

Reads the status pages of a Technicolor TC4400 cable modem and publishes all
channel values to MQTT. Home Assistant creates a **TC4400** device with its sensors
automatically (MQTT discovery).

Scraper by Philipp Reichmuth ([phrxmd/Technicolor_modem_scrape](https://github.com/phrxmd/Technicolor_modem_scrape)),
based on scripts by mmiller7. License: CC BY-NC-SA 4.0.

## Requirements

- An MQTT broker. With the **Mosquitto broker** app installed, nothing needs to be
  configured: broker address and credentials are picked up automatically.
- Home Assistant must be able to reach the modem's web interface, normally
  `http://192.168.100.1` — test it in a browser first. Behind a router/firewall this
  usually needs one extra step — see [Reaching the modem through your router](#reaching-the-modem-through-your-router).

## Options

| Option | Default | Description |
|---|---|---|
| `modem_url` | `http://192.168.100.1` | Web interface of the modem (plain HTTP) |
| `modem_username` | `admin` | |
| `modem_password` | `bEn2o#US9s` | TC4400 default; change it if you changed it on the modem |
| `scan_interval` | `300` | Seconds between scrapes (30–86400) |
| `mqtt_host` | *(empty)* | Leave empty to use the Mosquitto app automatically |
| `mqtt_port` | *(empty)* | Default 1883 |
| `mqtt_username` / `mqtt_password` | *(empty)* | Only for a manually configured broker |
| `mqtt_topic_prefix` | `homeassistant/sensor` | MQTT discovery prefix |
| `mqtt_uid_prefix` | `cablemodem` | Prefix for unique IDs |
| `verbose` | `false` | Log the full scraper output on every run |
| `power_switch` | *(empty)* | Entity ID of the modem's power switch; empty = dropdown on the device page |
| `power_sensor` | *(empty)* | Entity ID of the modem's power sensor; empty = dropdown on the device page |
| `restart_off_seconds` | `15` | How long *Restart Modem* keeps the power switch off |
| `history_length` | `30` | Scrapes kept per channel for the trend sparklines in the channel table (`0` = off) |
| `dashboard_title` | `Cable modem` | Title of the generated dashboard |
| `dashboard_url` | `cable-modem` | Dashboard that is created when *Dashboard Target* is set to *New: …* (URL must contain a hyphen) |
| `dashboard_cards` | `auto` | `auto`: use plotly-graph / apexcharts-card if installed; `builtin`: only built-in cards; `hacs`: always use them |
| `dashboard_hours` | `48` | Time range of the charts |
| `latency_enabled` | `true` | Run the internet latency probe |
| `latency_targets` | `1.1.1.1` | IP addresses or host names to ping (one entry each) |
| `latency_interval` | `1` | Seconds between probes per target |
| `latency_window` | `10` | Seconds per statistics window (= how often values are published) |
| `latency_threshold` | `30` | ms above base latency that counts as congestion |
| `latency_method` | `auto` | `auto`: ICMP ping, TCP connect if ICMP isn't possible; `icmp`; `tcp` |
| `latency_tcp_port` | `443` | Port for the TCP method |

## Sensors

- **Modem**: connectivity, boot and security state, uptime (text and seconds),
  last boot (timestamp), TCP latency to the modem, DOCSIS and software version,
  IP addresses
- **Down N** / **Up N**: per channel frequency, width, power, SNR (downstream),
  modulation, lock/bonding status, corrected/uncorrectable errors (downstream)
- **Summary**: channel and locked-channel counts, downstream/upstream power ranges,
  and — separately for **SC-QAM** and **OFDM** — worst SNR (SC-QAM also average),
  error totals and errors per minute. Error rates are calculated per channel and
  handle modem restarts and counter resets (a counter that goes down counts as
  reset, never as a negative value). OFDM channels count codewords at a far
  higher rate and the TC4400 reports their SNR differently, so they get their own
  sensors (*OFDM SNR Min*, *OFDM Corrected/Uncorrectable Codewords*, *… per
  Minute*) instead of distorting the SC-QAM values. The dashboard shows OFDM SNR
  in blue (🔵) without SC-QAM thresholds.
- **Downstream / Upstream Channel List**: number of channels, with all channels
  as the `channels` attribute (used by the dashboard's spectrum charts and tables)
- **Channel History**: the last `history_length` scrapes per channel as attributes
  (downstream SNR and uncorrectable errors per scrape, upstream power), shown as
  sparklines in the channel table. With 30 scrapes and ~30 channels this is about
  8 KB per scrape in the recorder; set `history_length` to `0` if you don't need it.
- **Create Dashboard** (button): builds the dashboard, see below
- **Internet Latency** device: one sensor set per latency target, see below

## Modem power: restart and power draw

If the modem hangs on a smart plug (or any `switch` entity), the app can
power-cycle it and show its power draw. On the TC4400 device page, section
*Configuration*:

- **Modem Power Switch** — the switch the modem is plugged into
- **Modem Power Sensor** — optional, a power sensor (W) for the modem's draw

Alternatively enter the entity IDs in the app options `power_switch` and
`power_sensor` (e.g. `switch.modem_plug`). If set, they take precedence over the
dropdowns, which then show them; leave them empty to use the dropdowns. Any
entity that can be switched on and off works (`switch`, `input_boolean`, `light`, …).

Once a switch is chosen, the device gets:

- **Restart Modem** (button) — switches off, waits `restart_off_seconds`, switches
  on and checks that the switch really is on again (up to 6 attempts; if it stays
  off you get an alarm notification). A second restart within 3 minutes is
  refused. The dashboard button asks for confirmation.
- **Last Power Cycle** (timestamp) — the last time the switch went **off → on**,
  **no matter who switched it**: the button, an automation or script, a user in
  the UI, or the button on the plug itself. Attributes: `off_at`, `on_at`,
  `off_seconds` and `source` (*Restart Modem button (app)*, *user <name>*,
  *automation or script*, *device or integration*). The app follows the switch
  continuously and, after a restart of the app, fills in the last power cycle
  from the switch's history (last 30 days).

The modem's own *Last Boot* sensor additionally covers every reboot, including
power failures or a restart by the provider.

> **Use a switch that works locally** (Zigbee, Z-Wave, Shelly, Tasmota, ESPHome,
> Matter, …). A plug that is switched through a cloud may not come back on once
> the modem — and with it the internet — is off. The app warns when the chosen
> switch belongs to a known cloud integration. Never choose a switch that also
> powers Home Assistant, the router or the network path to the switch.

## Internet latency

The app pings each target in `latency_targets` (default `1.1.1.1`, Cloudflare)
every `latency_interval` seconds and publishes, per `latency_window`:

| Sensor | Meaning |
|---|---|
| Average, P95, Min, Max | Round-trip time over the window |
| Jitter | Average difference between consecutive round trips |
| Loss | Lost probes in % |
| Base Latency | 10th percentile of the last 15 minutes — the "idle" latency of the line |
| Congestion Threshold | Base latency + `latency_threshold` |
| Status | **Line clear**, **Unstable** (one loss, or P95 above the threshold), **Congested** (average above the threshold), **Packet loss** (> 10 % lost), **Down** (no replies) |

All values are also attributes of the *Status* sensor. ICMP needs raw sockets;
if they aren't available the probe falls back to a TCP connect to
`latency_tcp_port` and the attribute `method` shows `tcp`.

With the default 10 s window each target writes about 9 values every 10 s to the
recorder. For a smaller database use a larger window (e.g. 30 or 60 s).

## Dashboard

On the TC4400 device page (*Settings → Devices & services → MQTT → TC4400*,
section *Configuration*):

1. **Dashboard Target** — choose where the dashboard goes: one of your existing
   dashboards, or *New: Cable modem (/cable-modem)* to create a separate one
   (shown in the sidebar). The list is refreshed every 10 minutes.
2. Press **Create Dashboard**.

The app writes a view **TC4400** (`/<dashboard>/tc4400`) into the chosen dashboard
and reports the result as a notification. **Other views of that dashboard are not
touched**; an existing TC4400 view is replaced. Press it again any time — e.g. after
installing extra cards — to rebuild the view. Entity IDs are looked up by their
unique ID, so renamed entities and other modem integrations don't get in the way.

Dashboards that Home Assistant generates automatically (e.g. a never-edited
*Overview*) must first be taken over: open the dashboard, *⋮ → Edit dashboard →
Take control*. YAML dashboards can't be changed by the app.

**Contents**

The view is laid out in rows; related things stay together, and on a phone the
sections stack in the same order.

| Row | Section | Built-in cards | With extra cards |
|---|---|---|---|
| 1 | Status | Clickable rows (click = history graph) under **SC-QAM** and **OFDM**: SNR worst/average, uncorrectable and corrected errors per minute and since boot, OFDM channels | same |
| 1 | Connection | Tiles: connectivity, uptime, locked channels, internet status, modem response; gauges: worst SNR SC-QAM and OFDM, upstream and downstream power min/max | same |
| 1 | Modem | Restart button with confirmation, switch state (read-only), power draw, last power cycle and who triggered it, last boot, DOCSIS, software | same |
| 2 | Downstream | SNR and power range history | plus Fritz!Box-style spectrum per channel (*plotly-graph*); charts with coloured good/warning/bad ranges (*apexcharts-card*) |
| 2 | Upstream | Power history | plus spectrum; coloured ranges |
| 2 | Channel history | Downstream power and SNR of every channel over time (one line per channel) | same |
| 3 | Internet latency | Status line per target (average, P95, min/max, jitter, loss) and history graph | Chart with P95 shading, average, base latency and congestion threshold |
| 3 | Stability | Locked channels (7 days), response time of the modem | same, as charts |
| 4 | Errors | Corrected/uncorrectable per hour/day, SC-QAM and OFDM | Columns per hour |
| 5 | Channels | Table of all channels with 🟢🟡🔴 markers and sparklines of the last 30 scrapes; click a value for that channel's history | same |
| 6 | Channel details | Every channel value as a clickable row (click = history popup): downstream power, SNR, corrected, uncorrectable; upstream power | same |

**Optional extra cards** — install from the Marketplace / HACS (*Dashboard*
category), then press *Create Dashboard* again:

- **Plotly Graph Card** (`plotly-graph`) — channel spectrum charts
- **ApexCharts Card** (`apexcharts-card`) — time series with coloured ranges

**As an action** (scripts, automations, *Developer tools → Actions*):

```yaml
# default settings from the app options
action: button.press
target:
  entity_id: button.tc4400_create_dashboard
```

```yaml
# custom settings (payload must be one line of JSON)
action: mqtt.publish
data:
  topic: tc4400_scraper/dashboard/create
  payload: >-
    {"title": "Cable modem", "url_path": "cable-modem", "cards": "auto",
    "hours": 24, "latency_hours": 3, "include": {"internet": true,
    "spectrum": true, "channel_table": true,
    "upstream": true, "downstream": true, "errors": true, "latency": false}}
```

All keys are optional. `url_path` overrides the *Dashboard Target* (`lovelace` = the
*Overview* dashboard). `include` accepts `status`, `power`, `gauges`, `internet`,
`spectrum`, `channel_table`, `upstream`, `downstream`, `errors`, `latency`
(modem latency). `latency_hours` (default 3) is the time range of the internet
latency charts. With several modems
add `"serial": "<serial number>"`.

The colour thresholds are rough DOCSIS rules of thumb (256QAM): downstream power
±7 dBmV good / ±15 tolerable, SNR ≥ 36 dB good / ≥ 33 dB marginal, upstream power
≤ 47 dBmV good / ≤ 51 dBmV high. Providers use their own limits.

A static example (built-in cards only) is in the repository under
`examples/dashboard.yaml`.

## Troubleshooting

| Log message | Meaning |
|---|---|
| `Scrape failed (exit code 22)` | Modem not reachable — see the router section below |
| `Scrape failed (exit code 21)` | Wrong modem username/password, or the page looks different |
| `Scrape failed (exit code 23)` | Page reached but could not be parsed (unsupported firmware?) |
| `Scrape failed (exit code 20)` | MQTT broker rejected the message (credentials, ACL) |
| `No MQTT broker found` | Install the Mosquitto broker app or set `mqtt_host` |
| `[dashboard] ERROR: no TC4400 sensors found` | Wait for the first scrape, then press *Create Dashboard* again |
| `[dashboard] ERROR: authentication failed` | Restart the app (it needs Home Assistant API access, granted on install/update) |

---

## Reaching the modem through your router

Home Assistant must be able to open the modem's web interface
(`http://192.168.100.1`). Test it with a browser on a PC in the same network, or
in a Home Assistant terminal:

```sh
curl -s -o /dev/null -w '%{http_code}\n' --connect-timeout 5 http://192.168.100.1/
```

`200` or `401` = reachable. `000` = not reachable: behind pfSense, OPNsense or
OpenWrt the router needs a virtual IP in the modem's subnet plus an outbound NAT
rule (and optionally a static ARP entry). Step by step, with tests and
troubleshooting:
**[Reaching the modem's web interface](https://github.com/betterbitsbites/tc4400-ha-addon/blob/main/MODEM_ACCESS.md)**
