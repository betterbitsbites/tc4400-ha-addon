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
- **Summary**: channel and locked-channel counts, worst/average SNR, power ranges,
  error totals and errors per minute
- **Downstream / Upstream Channel List**: number of channels, with all channels
  as the `channels` attribute (used by the dashboard's spectrum charts and tables)
- **Create Dashboard** (button): builds the dashboard, see below
- **Internet Latency** device: one sensor set per latency target, see below

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

| Section | Built-in cards | With extra cards |
|---|---|---|
| Status | Connectivity, DOCSIS, uptime, last boot, latency, locked channels, errors | same |
| Signal now | Gauges: worst SNR, highest upstream power, downstream power range | same |
| Internet latency | Status line per target (average, P95, min/max, jitter, loss) and history graph | Chart with min–max shading, average, base latency and congestion threshold (*apexcharts-card*) |
| Channels (spectrum) | – | Fritz!Box-style bars per channel along the frequency band: downstream power, downstream SNR, upstream power — coloured green/yellow/red (*plotly-graph*) |
| Channel table | Table of all channels with 🟢🟡🔴 markers | same |
| Upstream / Downstream | History graphs | Charts with coloured good/warning/bad ranges (*apexcharts-card*) |
| Errors | Corrected/uncorrectable per hour/day | Columns per hour (*apexcharts-card*) |
| Modem latency | History graph | Chart (*apexcharts-card*) |

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
*Overview* dashboard). `include` accepts `status`, `gauges`, `internet`,
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
**[Reaching the modem's web interface](https://github.com/coaxx77/tc4400-ha-addon/blob/main/MODEM_ACCESS.md)**
