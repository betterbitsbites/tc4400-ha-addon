#!/usr/bin/env python3
"""Create or update a Home Assistant dashboard for the TC4400 modem scraper.

Part of the Technicolor TC4400 Modem Scraper app. Scraper by Philipp Reichmuth
(phrxmd/Technicolor_modem_scrape), based on scripts by mmiller7. CC-BY-NC-SA 4.0.

Usage:
  tc4400_dashboard.py create [PAYLOAD]   build the dashboard view
  tc4400_dashboard.py refresh            publish the "Dashboard Target" select
                                         with the current list of dashboards
  tc4400_dashboard.py select LABEL       store the dashboard chosen in the select

The view "TC4400" (path tc4400) is added to the target dashboard, or replaced if
it already exists; all other views of that dashboard are kept. The target is,
in this order: url_path in the payload, the "Dashboard Target" select, the
dashboard_url option (created if it doesn't exist).

PAYLOAD is what was published to the dashboard command topic:
  * empty or a modem serial number (the "Create Dashboard" button), or
  * a JSON object with any of these keys (missing keys use the app options):
      {"title": "Cable modem", "url_path": "cable-modem", "cards": "auto",
       "hours": 48, "latency_hours": 3, "serial": "CP1234ABC",
       "include": {"status": true, "gauges": true, "internet": true, "spectrum": true,
                   "channel_table": true, "upstream": true, "downstream": true,
                   "errors": true, "latency": true}}

The script talks to Home Assistant through the Supervisor websocket proxy
(ws://supervisor/core/websocket, token in SUPERVISOR_TOKEN). For testing outside
the app, set HA_WS_URL (e.g. ws://homeassistant.local:8123/api/websocket) and
HA_TOKEN (a long-lived access token of an admin user).

Only the Python standard library is used.
"""
import base64
import json
import os
import re
import socket
import struct
import subprocess
import sys
from urllib.parse import urlparse

OPTIONS_FILE = "/data/options.json"
DATA_DIR = os.environ.get("DATA_DIR", "/data")
TARGET_FILE = os.path.join(DATA_DIR, "dashboard_target.json")
TOPIC_BASE = "tc4400_scraper/dashboard"
VIEW_PATH = "tc4400"
DEFAULT_DASHBOARD = "lovelace"   # the built-in "Overview" dashboard
SECTIONS = ("status", "gauges", "internet", "spectrum", "channel_table", "upstream",
            "downstream", "errors", "latency")

# Rough DOCSIS rules of thumb (256QAM downstream). Used for colours and bands.
DS_POWER_OK, DS_POWER_WARN = 7, 15          # |dBmV|
SNR_WARN, SNR_OK = 33, 36                   # dB
US_POWER_OK, US_POWER_WARN = 47, 51         # dBmV
GREEN, YELLOW, RED = "#2e7d32", "#f9a825", "#c62828"
OFDM_BLUE = "#42a5f5"   # OFDM SNR: the TC4400 measures it differently, no SC-QAM thresholds


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------
# Minimal websocket client (RFC 6455, client side, text frames only)
# --------------------------------------------------------------------------
class WebSocket:
    def __init__(self, url, timeout=30):
        u = urlparse(url)
        if u.scheme != "ws":
            raise ValueError("only ws:// URLs are supported: %s" % url)
        host, port = u.hostname, u.port or 80
        path = u.path or "/"
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            "GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key)
        )
        self.sock.sendall(request.encode())
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self.sock.recv(1)
            if not chunk:
                raise ConnectionError("connection closed during handshake")
            header += chunk
        status = header.split(b"\r\n", 1)[0]
        if b" 101 " not in status + b" ":
            raise ConnectionError("websocket handshake failed: %s" % status.decode(errors="replace"))

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("connection closed")
            buf += chunk
        return buf

    def _send_frame(self, opcode, data):
        header = bytes([0x80 | opcode])
        n = len(data)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(header + mask + masked)

    def send(self, obj):
        self._send_frame(0x1, json.dumps(obj).encode())

    def recv(self):
        message = b""
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise ConnectionError("websocket closed by server")
            if opcode == 0x9:
                self._send_frame(0xA, data)
                continue
            if opcode in (0x1, 0x0):
                message += data
                if fin:
                    return json.loads(message.decode())

    def close(self):
        try:
            self._send_frame(0x8, b"")
        finally:
            self.sock.close()


class HAError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class HomeAssistant:
    def __init__(self, url, token):
        self.ws = WebSocket(url)
        self.next_id = 1
        first = self.ws.recv()
        if first.get("type") != "auth_required":
            raise ConnectionError("unexpected first message: %s" % first)
        self.ws.send({"type": "auth", "access_token": token})
        answer = self.ws.recv()
        if answer.get("type") != "auth_ok":
            raise PermissionError("authentication failed: %s" % answer.get("message", answer))

    def call(self, msg_type, **data):
        msg_id = self.next_id
        self.next_id += 1
        self.ws.send(dict(data, id=msg_id, type=msg_type))
        while True:
            answer = self.ws.recv()
            if answer.get("id") != msg_id:
                continue  # events or late answers
            if not answer.get("success"):
                err = answer.get("error", {})
                raise HAError(err.get("code"), "%s failed: %s" % (msg_type, err.get("message", err)))
            return answer.get("result")


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
def load_settings(payload):
    opts = {}
    try:
        with open(OPTIONS_FILE) as f:
            opts = json.load(f)
    except (OSError, ValueError):
        pass
    settings = {
        "title": opts.get("dashboard_title", "Cable modem"),
        "new_url": str(opts.get("dashboard_url", "cable-modem")).strip().lower(),
        "url_path": stored_target() or opts.get("dashboard_url", "cable-modem"),
        "cards": opts.get("dashboard_cards", "auto"),
        "hours": int(opts.get("dashboard_hours", 48)),
        "latency_hours": 3,
        "uid_prefix": os.environ.get("MQTT_UID_PREFIX") or opts.get("mqtt_uid_prefix", "cablemodem"),
        "serial": None,
        "include": {s: True for s in SECTIONS},
    }
    payload = (payload or "").strip()
    if payload.startswith("{"):
        try:
            req = json.loads(payload)
        except ValueError as e:
            raise ValueError("payload is not valid JSON: %s" % e)
        for key in ("title", "url_path", "cards", "hours", "latency_hours", "serial"):
            if key in req and req[key] not in (None, ""):
                settings[key] = req[key]
        for key, value in (req.get("include") or {}).items():
            if key in settings["include"]:
                settings["include"][key] = bool(value)
    elif payload and payload.lower() not in ("press", "create", "{}"):
        settings["serial"] = payload
    settings["hours"] = max(1, min(int(settings["hours"]), 720))
    settings["latency_hours"] = max(1, min(int(settings["latency_hours"]), 48))
    if settings["cards"] not in ("auto", "builtin", "hacs"):
        raise ValueError("cards must be auto, builtin or hacs")
    settings["url_path"] = str(settings["url_path"]).strip().lower().lstrip("/") or DEFAULT_DASHBOARD
    return settings


def valid_new_url(url):
    return re.fullmatch(r"[a-z0-9_]+(-[a-z0-9_]+)+", url or "") is not None


def stored_target():
    try:
        with open(TARGET_FILE) as f:
            return json.load(f).get("url_path")
    except (OSError, ValueError):
        return None


def store_target(url_path):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(TARGET_FILE, "w") as f:
            json.dump({"url_path": url_path}, f)
    except OSError as e:
        log("could not store dashboard target: %s" % e)


# --------------------------------------------------------------------------
# MQTT (via mosquitto_pub, like the scraper)
# --------------------------------------------------------------------------
def mqtt_publish(topic, payload, retain=True):
    message = payload if isinstance(payload, str) else json.dumps(payload)
    if os.environ.get("DRY_RUN") == "1":
        print("[dry-run] %s%s: %s" % ("-r " if retain else "", topic, message), flush=True)
        return
    cmd = ["mosquitto_pub", "-h", os.environ.get("MQTT_HOST", "127.0.0.1"),
           "-p", str(os.environ.get("MQTT_PORT") or 1883), "-t", topic, "-m", message]
    if retain:
        cmd.append("-r")
    if os.environ.get("MQTT_USERNAME"):
        cmd += ["-u", os.environ["MQTT_USERNAME"]]
    if os.environ.get("MQTT_PASSWORD"):
        cmd += ["-P", os.environ["MQTT_PASSWORD"]]
    subprocess.run(cmd, check=True, timeout=15, capture_output=True)


# --------------------------------------------------------------------------
# Dashboard targets ("Dashboard Target" select)
# --------------------------------------------------------------------------
def dashboard_targets(ha, settings):
    """[(label, url_path, exists)] of dashboards the view can be written to."""
    targets = [("Overview (/%s)" % DEFAULT_DASHBOARD, DEFAULT_DASHBOARD, True)]
    for d in sorted(ha.call("lovelace/dashboards/list") or [], key=lambda d: (d.get("title") or "").lower()):
        if d.get("mode", "storage") != "storage" or not d.get("url_path"):
            continue  # YAML dashboards can't be written
        targets.append(("%s (/%s)" % (d.get("title") or d["url_path"], d["url_path"]), d["url_path"], True))
    if settings["new_url"] not in [t[1] for t in targets] and valid_new_url(settings["new_url"]):
        targets.append(("New: %s (/%s)" % (settings["title"], settings["new_url"]), settings["new_url"], False))
    return targets


def label_to_url(label):
    m = re.search(r"\(/([^)]+)\)\s*$", label or "")
    return m.group(1) if m else None


def publish_target_select(ha, settings, modem_id, targets):
    current = settings["url_path"]
    labels = [t[0] for t in targets]
    state = next((t[0] for t in targets if t[1] == current), None)
    if state is None:  # stored target was deleted: fall back to the option
        state = next((t[0] for t in targets if t[1] == settings["new_url"]), labels[0])
    disc = os.environ.get("MQTT_TOPIC_PREFIX", "homeassistant/sensor").rsplit("/", 1)[0]
    mqtt_publish("%s/select/%s_dashboard_target/config" % (disc, modem_id), {
        "name": "Dashboard Target", "unique_id": "%s_dashboard_target" % modem_id,
        "command_topic": TOPIC_BASE + "/target/set", "state_topic": TOPIC_BASE + "/target",
        "options": labels, "icon": "mdi:view-dashboard-variant", "entity_category": "config",
        "device": {"identifiers": [modem_id]}})
    mqtt_publish(TOPIC_BASE + "/target", state)
    return state


# --------------------------------------------------------------------------
# Entity lookup by unique_id
# --------------------------------------------------------------------------
def find_entities(registry, uid_prefix, serial=None):
    pattern = re.compile(
        r"^" + re.escape(uid_prefix) +
        r"(?P<serial>[A-Za-z0-9]+)_(?P<infix>status|summary|channels_down|channels_up|channel_history|downstream\d+|upstream\d+)_(?P<key>\w+)$"
    )
    modems = {}
    for e in registry:
        if e.get("platform") != "mqtt" or e.get("disabled_by"):
            continue
        m = pattern.match(e.get("unique_id") or "")
        if m:
            modems.setdefault(m.group("serial"), {})[(m.group("infix"), m.group("key"))] = e["entity_id"]
    if not modems:
        raise LookupError("no TC4400 sensors found - is the scraper running and MQTT discovery enabled?")
    if serial:
        if serial not in modems:
            raise LookupError("modem %s not found (known: %s)" % (serial, ", ".join(sorted(modems))))
        return serial, modems[serial]
    serial = max(modems, key=lambda s: len(modems[s]))
    return serial, modems[serial]


def find_latency(registry):
    """{target slug: {key: entity_id}} for the internet latency probe."""
    pattern = re.compile(r"^inetlat_(?P<slug>\w+?)_(?P<key>status|avg|p95|min|max|jitter|loss|base|threshold)$")
    targets = {}
    for e in registry:
        if e.get("platform") != "mqtt" or e.get("disabled_by"):
            continue
        m = pattern.match(e.get("unique_id") or "")
        if m:
            targets.setdefault(m.group("slug"), {})[m.group("key")] = e["entity_id"]
    def natural(s):  # 1_1_1_1 before 193_99_144_80
        return [(0, int(x), "") if x.isdigit() else (1, 0, x) for x in s.split("_")]
    return {s: targets[s] for s in sorted(targets, key=natural) if "avg" in targets[s]}


class Entities:
    def __init__(self, mapping):
        self.map = mapping

    def get(self, infix, key):
        return self.map.get((infix, key))

    def channels(self, direction, key):
        """[(channel number, entity_id)] for downstreamN / upstreamN, sorted by N."""
        out = []
        for (infix, k), entity in self.map.items():
            m = re.fullmatch(direction + r"(\d+)", infix)
            if m and k == key:
                out.append((int(m.group(1)), entity))
        return sorted(out)


# --------------------------------------------------------------------------
# Card builders
# --------------------------------------------------------------------------
def heading(text):
    return {"type": "heading", "heading": text, "heading_style": "title"}


def full(card):
    card["grid_options"] = {"columns": "full"}
    return card


def bands_ds_power():
    return [(-30, -DS_POWER_WARN, RED), (-DS_POWER_WARN, -DS_POWER_OK, YELLOW),
            (-DS_POWER_OK, DS_POWER_OK, GREEN), (DS_POWER_OK, DS_POWER_WARN, YELLOW),
            (DS_POWER_WARN, 30, RED)]


def bands_snr():
    return [(0, SNR_WARN, RED), (SNR_WARN, SNR_OK, YELLOW), (SNR_OK, 60, GREEN)]


def bands_us_power():
    return [(0, US_POWER_OK, GREEN), (US_POWER_OK, US_POWER_WARN, YELLOW), (US_POWER_WARN, 70, RED)]


def apex_line(title, series, hours, bands=None, ymin=None, ymax=None, decimals=1):
    card = {
        "type": "custom:apexcharts-card",
        "graph_span": "%dh" % hours,
        "header": {"show": True, "title": title, "show_states": True, "colorize_states": True},
        "series": [{"entity": e, "name": n, "stroke_width": 2, "curve": "stepline"} for e, n in series],
    }
    yaxis = {"decimals": decimals}
    if ymin is not None:
        yaxis["min"] = ymin
    if ymax is not None:
        yaxis["max"] = ymax
    card["yaxis"] = [yaxis]
    if bands:
        card["apex_config"] = {"annotations": {"yaxis": [
            {"y": lo, "y2": hi, "fillColor": color, "opacity": 0.08, "borderColor": "transparent"}
            for lo, hi, color in bands]}}
    return full(card)


def apex_columns(title, entity, name, hours, color):
    return full({
        "type": "custom:apexcharts-card",
        "graph_span": "%dh" % hours,
        "header": {"show": True, "title": title},
        "series": [{"entity": entity, "name": name, "type": "column", "color": color,
                    "group_by": {"func": "diff", "duration": "1h"}}],
    })


def history(title, series, hours):
    return full({"type": "history-graph", "title": title, "hours_to_show": hours,
                 "entities": [{"entity": e, "name": n} for e, n in series]})


def stat_bars(title, entity, name, period, days):
    return full({"type": "statistics-graph", "title": title, "chart_type": "bar",
                 "period": period, "days_to_show": days, "stat_types": ["change"],
                 "entities": [{"entity": entity, "name": name}]})


def gauge(entity, name, gmin, gmax, severity=None, segments=None):
    card = {"type": "gauge", "entity": entity, "name": name, "min": gmin, "max": gmax, "needle": True}
    if severity:
        card["severity"] = severity
    if segments:
        card["segments"] = segments
    return card


def plotly_spectrum(title, entity, value, unit, color_js, yrange=None, lines=()):
    """Fritz!Box-style spectrum: one block per channel at its frequency, as wide as
    the channel, coloured by thresholds; dashed lines mark the thresholds."""
    chans = "(hass.states['%s']?.attributes?.channels || [])" % entity
    shapes = [{"type": "line", "xref": "paper", "x0": 0, "x1": 1, "y0": y, "y1": y,
               "line": {"color": color, "width": 1, "dash": "dash"}} for y, color in lines]
    yaxis = {"title": {"text": unit}, "zeroline": True}
    if yrange:
        yaxis["range"] = list(yrange)
    return full({
        "type": "custom:plotly-graph",
        "title": title,
        "raw_plotly_config": True,
        "refresh_interval": 60,
        "config": {"displayModeBar": False},
        "entities": [{
            "entity": entity,
            "name": "%s (%s)" % (value.upper() if value == "snr" else value.capitalize(), unit),
            "type": "bar",
            "x": "$ex %s.map(c => c.frequency / 1e6)" % chans,
            "y": "$ex %s.map(c => c.%s)" % (chans, value),
            # block width = channel width (at least 1 MHz so narrow channels stay visible)
            "width": "$ex %s.map(c => Math.max((c.width || 0) / 1e6, 1) * 0.95)" % chans,
            "marker": {"color": "$ex %s.map(c => typeof c.%s !== 'number' ? '#9e9e9e' : (%s))"
                                % (chans, value, color_js),
                       "opacity": "$ex %s.map(c => /OFDM/.test(c.type || '') ? 0.55 : 0.95)" % chans},
            "customdata": "$ex %s.map(c => [c.id, c.type, c.profile, (c.width || 0) / 1e6])" % chans,
            "hovertemplate": ("Channel %%{customdata[0]} · %%{customdata[1]} · %%{customdata[2]}<br>"
                              "%%{x:.1f} MHz (%%{customdata[3]:.1f} MHz wide)<br>%%{y:.1f} %s<extra></extra>") % unit,
        }],
        "layout": {"height": 240, "showlegend": False, "bargap": 0,
                   "margin": {"t": 10, "b": 45, "l": 55, "r": 10},
                   "xaxis": {"type": "linear", "title": {"text": "MHz"}},
                   "yaxis": yaxis, "shapes": shapes},
    })


SPARK_CHARS = "▁▂▃▄▅▆▇█"


def channel_table(title, entity, upstream=False, history=None):
    """Markdown table from the channel list attributes (built-in card).

    With a history entity, every row gets text sparklines of the last scrapes:
    downstream SNR and uncorrectable errors per scrape, upstream power. They are
    coloured by the latest value (OFDM SNR in blue: no SC-QAM thresholds)."""
    mark_ok, mark_warn, mark_bad = "🟢", "🟡", "🔴"
    c_ok, c_warn, c_bad, c_ofdm, c_dim = "#4caf50", "#fbc02d", "#e53935", OFDM_BLUE, "#757575"
    macros = (
        "{%- macro fmt(f) -%}{{ (f / 1000000) | round(1) if f is number else '–' }}{%- endmacro -%}\n"
        "{%- macro v(x) -%}{{ x if x is number else '–' }}{%- endmacro -%}\n"
        "{%- macro dp(x) -%}{% if x is number %}{{ '" + mark_bad + "' if x | abs > " + str(DS_POWER_WARN) +
        " else ('" + mark_warn + "' if x | abs > " + str(DS_POWER_OK) + " else '" + mark_ok + "') }}{% endif %}{%- endmacro -%}\n"
        "{%- macro sn(x, t) -%}{% if x is number and 'OFDM' in (t or '') %}🔵{% elif x is number %}{{ '" + mark_bad +
        "' if x < " + str(SNR_WARN) +
        " else ('" + mark_warn + "' if x < " + str(SNR_OK) + " else '" + mark_ok + "') }}{% endif %}{%- endmacro -%}\n"
        "{%- macro us(x) -%}{% if x is number %}{{ '" + mark_bad + "' if x > " + str(US_POWER_WARN) +
        " else ('" + mark_warn + "' if x > " + str(US_POWER_OK) + " else '" + mark_ok + "') }}{% endif %}{%- endmacro -%}\n"
        "{%- macro typ(t) -%}{{ ' <sub>' ~ t ~ '</sub>' if t and t != 'SC-QAM' else '' }}{%- endmacro -%}\n"
        # sparkline: values scaled to the row's own range, but at least `span` wide so
        # noise doesn't look like a cliff; missing values are a thin space
        "{%- macro spark(vals, span, color) -%}"
        "{%- set nums = (vals or []) | select('number') | list -%}"
        "{%- if nums | count < 2 -%}<font color=\"" + c_dim + "\">collecting…</font>{%- else -%}"
        "{%- set lo = nums | min -%}{%- set hi = [nums | max, lo + span] | max -%}"
        "<font color=\"{{ color }}\">{%- for x in vals -%}"
        "{%- if x is number -%}{{ '" + SPARK_CHARS + "'[(((x - lo) / (hi - lo)) * 7) | round(0) | int] }}"
        "{%- else -%}&#8201;{%- endif -%}{%- endfor -%}</font>{%- endif -%}{%- endmacro -%}\n"
        # error sparkline: height ~ sqrt(errors per scrape), grey when there were none
        "{%- macro espark(vals) -%}"
        "{%- set nums = (vals or []) | select('number') | list -%}"
        "{%- if nums | count < 1 -%}<font color=\"" + c_dim + "\">collecting…</font>{%- else -%}"
        "{%- set hi = [nums | max, 1] | max -%}{%- set total = nums | sum -%}"
        "<font color=\"{{ '" + c_bad + "' if total > 0 else '" + c_dim + "' }}\">{%- for x in vals -%}"
        "{%- if x is number -%}{{ '" + SPARK_CHARS + "'[((x / hi) ** 0.5 * 7) | round(0) | int] }}"
        "{%- else -%}&#8201;{%- endif -%}{%- endfor -%}</font> <sub>{{ total | int }}</sub>{%- endif -%}{%- endmacro -%}\n"
        "{%- macro snrcol(x, t) -%}{{ '" + c_ofdm + "' if 'OFDM' in (t or '') else ('" + c_bad + "' if x is number and x < " +
        str(SNR_WARN) + " else ('" + c_warn + "' if x is number and x < " + str(SNR_OK) + " else '" + c_ok + "')) }}{%- endmacro -%}\n"
        "{%- macro uscol(x) -%}{{ '" + c_bad + "' if x is number and x > " + str(US_POWER_WARN) + " else ('" + c_warn +
        "' if x is number and x > " + str(US_POWER_OK) + " else '" + c_ok + "') }}{%- endmacro -%}\n"
        "{%- set chans = state_attr('" + entity + "', 'channels') or [] -%}\n"
    )
    if history:
        macros += ("{%- set hist = state_attr('" + history + "', 'up' if " + ("true" if upstream else "false") +
                   " else 'down') or {} -%}\n"
                   "{%- set n = state_attr('" + history + "', 'length') or 30 -%}\n")
    if upstream:
        head = "| Ch | ID | MHz | Power dBmV | Modulation | Lock |"
        sep = "|---:|---:|---:|---:|:--|:--|"
        row = ("| {{ c.channel }} | {{ c.id }}{{ typ(c.type) }} | {{ fmt(c.frequency) }} | "
               "{{ v(c.power) }} {{ us(c.power) }} | {{ c.profile }} | {{ c.lockstatus }} |")
        if history:
            head += " Power · last {{ n }} |"
            sep += ":--|"
            row += " {% set h = hist.get(c.id | string, {}) %}{{ spark(h.power, 3, uscol(c.power)) }} |"
        foot = ""
    else:
        head = "| ID | MHz | Power dBmV | SNR dB | Modulation | Corrected | Uncorr. |"
        sep = "|---:|---:|---:|---:|:--|---:|---:|"
        row = ("| {{ c.id }}{{ typ(c.type) }} | {{ fmt(c.frequency) }} | {{ v(c.power) }} {{ dp(c.power) }} | "
               "{{ v(c.snr) }} {{ sn(c.snr, c.type) }} | {{ c.profile }} | {{ c.corr }} | {{ c.uncorr }} |")
        if history:
            head += " SNR · last {{ n }} | Uncorr. per scrape |"
            sep += ":--|:--|"
            row += (" {% set h = hist.get(c.id | string, {}) %}{{ spark(h.snr, 3, snrcol(c.snr, c.type)) }} | "
                    "{{ espark(h.uncorr) }} |")
        foot = ("\n<small>🔵 OFDM: SNR measured differently, SC-QAM thresholds don't apply. "
                "Corrected/uncorrectable count codewords, which OFDM sends far more of."
                + (" Trends: last scrapes, newest on the right; the number is the sum of the "
                   "uncorrectable errors shown." if history else "") + "</small>\n")
    content = (
        macros + head + "\n" + sep + "\n"
        "{%- for c in chans | sort(attribute='frequency') %}\n" + row + "\n{%- endfor %}\n" + foot
    )
    return full({"type": "markdown", "title": title, "content": content})


def latency_summary(title, keys):
    """One line like the pfSense latency header, from the status sensor's attributes."""
    st = keys.get("status")
    if not st:
        return None
    content = (
        "{%- set e = '" + st + "' -%}\n"
        "{%- set s = states(e) -%}\n"
        "{%- macro v(k, u) -%}{% set x = state_attr(e, k) %}{{ x if x is number else '–' }}{{ u }}{%- endmacro -%}\n"
        "{{ {'Line clear': '🟢', 'Unstable': '🟡', 'Congested': '🟠', 'Packet loss': '🔴', 'Down': '⛔'}.get(s, '⚪') }} "
        "**{{ s }}** · last {{ state_attr(e, 'window_s') or '–' }} s: average **{{ v('avg', ' ms') }}** · "
        "P95 **{{ v('p95', ' ms') }}** · Min/Max **{{ v('min', '') }} / {{ v('max', ' ms') }}** · "
        "Jitter **{{ v('jitter', ' ms') }}** · Loss **{{ v('loss', ' %') }}**  \n"
        "<small>Base latency {{ v('base', ' ms') }} · congestion threshold {{ v('threshold', ' ms') }} · "
        "via {{ state_attr(e, 'method') or '–' }}</small>"
    )
    return full({"type": "markdown", "title": title, "content": content})


def apex_latency(title, keys, hours):
    """pfSense-style chart, smoothed to 1-minute values: P95 shading, average curve,
    base latency and congestion threshold. Max/jitter/loss only in the header, so
    single spikes don't flatten the curve."""
    one_min = {"duration": "1min", "fill": "last"}
    series = []
    if keys.get("p95"):
        series.append({"entity": keys["p95"], "name": "P95", "type": "area", "color": "#e57373",
                       "opacity": 0.25, "stroke_width": 1, "curve": "smooth",
                       "group_by": dict(one_min, func="avg")})
    series.append({"entity": keys["avg"], "name": "Average", "type": "line", "color": "#ef5350",
                   "stroke_width": 2, "curve": "smooth", "group_by": dict(one_min, func="avg")})
    if keys.get("base"):
        series.append({"entity": keys["base"], "name": "Base latency", "type": "line", "color": "#26a69a",
                       "stroke_width": 2, "stroke_dash": 4, "curve": "stepline",
                       "group_by": dict(one_min, func="last")})
    if keys.get("threshold"):
        series.append({"entity": keys["threshold"], "name": "Congestion threshold", "type": "line",
                       "color": "#f9a825", "stroke_width": 2, "stroke_dash": 4, "curve": "stepline",
                       "group_by": dict(one_min, func="last")})
    for k, name in (("max", "Max"), ("jitter", "Jitter"), ("loss", "Loss")):
        if keys.get(k):
            series.append({"entity": keys[k], "name": name, "show": {"in_chart": False}})
    return full({
        "type": "custom:apexcharts-card",
        "graph_span": "%dh" % hours,
        "update_interval": "1min",
        "header": {"show": True, "title": title, "show_states": True, "colorize_states": True},
        "yaxis": [{"min": 0, "max": "~60", "decimals": 0}],
        "series": series,
    })


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
def build_dashboard(ent, settings, have_plotly, have_apex, latency=None):
    inc, hours = settings["include"], settings["hours"]
    sections = []

    def section(title, cards, span=1):
        cards = [c for c in cards if c]
        if cards:
            sec = {"type": "grid", "cards": [heading(title)] + cards}
            if span > 1:
                sec["column_span"] = span
            sections.append(sec)

    E = ent.get
    ds_list, us_list = E("channels_down", "count"), E("channels_up", "count")
    hist = E("channel_history", "scrapes")

    if inc["status"]:
        rows = [
            (E("status", "connectivity"), "Connectivity"), (E("status", "boot"), "Boot state"),
            (E("status", "docsis"), "DOCSIS"), (E("status", "sw_version"), "Software"),
            (E("status", "uptime"), "Uptime"), (E("status", "last_boot"), "Last boot"),
            (E("status", "latency"), "TCP latency"),
            (E("summary", "down_locked"), "Downstream locked"), (E("summary", "down_channels"), "Downstream channels"),
            (E("summary", "up_locked"), "Upstream locked"), (E("summary", "up_channels"), "Upstream channels"),
            (E("summary", "corr"), "Corrected total (SC-QAM)"), (E("summary", "uncorr"), "Uncorrectable total (SC-QAM)"),
            (E("summary", "corr_rate"), "Corrected / min (SC-QAM)"), (E("summary", "uncorr_rate"), "Uncorrectable / min (SC-QAM)"),
            (E("summary", "ofdm_snr_min"), "OFDM SNR (worst)"),
            (E("summary", "ofdm_uncorr_rate"), "OFDM uncorrectable / min"),
        ]
        entities = []
        for entity, name in rows:
            if entity:
                row = {"entity": entity, "name": name}
                if name == "Last boot":
                    row["format"] = "relative"
                entities.append(row)
        section("Status", [{"type": "entities", "show_header_toggle": False, "entities": entities}] if entities else [])

    if inc["gauges"]:
        ds_segments = [{"from": -20, "color": "var(--error-color)"},
                       {"from": -DS_POWER_WARN, "color": "var(--warning-color)"},
                       {"from": -DS_POWER_OK, "color": "var(--success-color)"},
                       {"from": DS_POWER_OK, "color": "var(--warning-color)"},
                       {"from": DS_POWER_WARN, "color": "var(--error-color)"}]
        cards = []
        if E("summary", "snr_min"):
            cards.append(gauge(E("summary", "snr_min"), "SNR worst (SC-QAM)", 25, 45,
                               severity={"red": 0, "yellow": SNR_WARN, "green": SNR_OK}))
        if E("summary", "up_power_max"):
            cards.append(gauge(E("summary", "up_power_max"), "US power (highest)", 30, 55,
                               severity={"green": 0, "yellow": US_POWER_OK, "red": US_POWER_WARN}))
        if E("summary", "down_power_min"):
            cards.append(gauge(E("summary", "down_power_min"), "DS power min", -20, 20, segments=ds_segments))
        if E("summary", "down_power_max"):
            cards.append(gauge(E("summary", "down_power_max"), "DS power max", -20, 20, segments=ds_segments))
        section("Signal now", cards)

    if inc["internet"] and latency:
        cards = []
        lh = settings["latency_hours"]
        for slug, keys in latency.items():
            name = slug.replace("_", ".")
            cards.append(latency_summary("Internet latency %s" % name, keys))
            if have_apex:
                cards.append(apex_latency("Latency %s" % name, keys, lh))
            else:
                s = [(keys[k], n) for k, n in (("avg", "Average"), ("max", "Max"), ("base", "Base latency"),
                                              ("threshold", "Congestion threshold")) if keys.get(k)]
                cards.append(history("Latency %s" % name, s, lh))
        section("Internet latency", cards)

    if inc["spectrum"] and have_plotly and ds_list:
        ds_power_color = ("Math.abs(c.power) > %d ? '%s' : (Math.abs(c.power) > %d ? '%s' : '%s')"
                          % (DS_POWER_WARN, RED, DS_POWER_OK, YELLOW, GREEN))
        snr_color = ("/OFDM/.test(c.type || '') ? '%s' : (c.snr < %d ? '%s' : (c.snr < %d ? '%s' : '%s'))"
                     % (OFDM_BLUE, SNR_WARN, RED, SNR_OK, YELLOW, GREEN))
        us_color = "c.power > %d ? '%s' : (c.power > %d ? '%s' : '%s')" % (US_POWER_WARN, RED, US_POWER_OK, YELLOW, GREEN)
        cards = [plotly_spectrum("Downstream power per channel", ds_list, "power", "dBmV", ds_power_color,
                                 (-DS_POWER_WARN - 3, DS_POWER_WARN + 3),
                                 [(DS_POWER_OK, YELLOW), (-DS_POWER_OK, YELLOW),
                                  (DS_POWER_WARN, RED), (-DS_POWER_WARN, RED)]),
                 plotly_spectrum("Downstream SNR per channel", ds_list, "snr", "dB", snr_color,
                                 (20, 45), [(SNR_OK, YELLOW), (SNR_WARN, RED)])]
        if us_list:
            cards.append(plotly_spectrum("Upstream power per channel", us_list, "power", "dBmV", us_color,
                                         (30, 56), [(US_POWER_OK, YELLOW), (US_POWER_WARN, RED)]))
        section("Channels (spectrum)", cards)

    if inc["channel_table"] and (ds_list or us_list):
        cards = []
        if ds_list:
            cards.append(channel_table("Downstream channels", ds_list, history=hist))
        if us_list:
            cards.append(channel_table("Upstream channels", us_list, upstream=True, history=hist))
        # twice as wide, so the table with its trend columns fits without scrolling
        section("Channel table", cards, span=2 if hist else 1)

    if inc["upstream"]:
        series = [(e, "Up %d" % n) for n, e in ent.channels("upstream", "power")]
        cards = []
        if series:
            cards.append(apex_line("Upstream power per channel", series, hours, bands_us_power(), 30, 55)
                         if have_apex else history("Upstream power per channel", series, hours))
        if E("summary", "up_locked"):
            s = [(E("summary", "up_locked"), "Locked")]
            cards.append(apex_line("Locked upstream channels", s, max(hours, 168), decimals=0, ymin=0)
                         if have_apex else history("Locked upstream channels", s, max(hours, 168)))
        section("Upstream (upload)", cards)

    if inc["downstream"]:
        cards = []
        snr = [(e, n) for e, n in ((E("summary", "snr_min"), "Worst SC-QAM"), (E("summary", "snr_avg"), "Average SC-QAM"),
                                   (E("summary", "ofdm_snr_min"), "Worst OFDM")) if e]
        pwr = [(e, n) for e, n in ((E("summary", "down_power_min"), "Min"), (E("summary", "down_power_max"), "Max")) if e]
        if snr:
            cards.append(apex_line("Downstream SNR", snr, hours, bands_snr(), 25, 45)
                         if have_apex else history("Downstream SNR", snr, hours))
        if pwr:
            cards.append(apex_line("Downstream power range", pwr, hours, bands_ds_power(), -20, 20)
                         if have_apex else history("Downstream power range", pwr, hours))
        section("Downstream (download)", cards)

    if inc["errors"]:
        cards = []
        for key, title, name, color, fallback in (
                ("uncorr", "Uncorrectable errors per hour (SC-QAM)", "Uncorrectable", RED, ("hour", 3)),
                ("corr", "Corrected errors per hour (SC-QAM)", "Corrected", YELLOW, ("day", 30)),
                ("ofdm_uncorr", "OFDM uncorrectable codewords per hour", "OFDM uncorrectable", OFDM_BLUE, ("hour", 3))):
            if E("summary", key):
                cards.append(apex_columns(title, E("summary", key), name, max(hours, 72), color)
                             if have_apex else stat_bars(title, E("summary", key), name, *fallback))
        section("Errors", cards)

    if inc["latency"] and E("status", "latency"):
        s = [(E("status", "latency"), "TCP")]
        section("Modem latency", [apex_line("TCP latency to the modem", s, hours, ymin=0)
                                  if have_apex else history("TCP latency to the modem", s, hours)])

    return {"title": "TC4400", "path": VIEW_PATH, "icon": "mdi:router-network",
            "type": "sections", "max_columns": 3, "sections": sections}


def installed_cards(ha):
    try:
        resources = ha.call("lovelace/resources") or []
    except RuntimeError:
        resources = []
    urls = " ".join((r.get("url") or "").lower() for r in resources)
    return "plotly-graph" in urls, "apexcharts-card" in urls


def connect():
    url = os.environ.get("HA_WS_URL", "ws://supervisor/core/websocket")
    token = os.environ.get("HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise PermissionError("no token: SUPERVISOR_TOKEN missing (homeassistant_api not enabled?)")
    return HomeAssistant(url, token)


def write_view(ha, settings, view, targets):
    """Add or replace our view in the target dashboard. Returns the dashboard URL."""
    url_path = settings["url_path"]
    known = {t[1]: t for t in targets}
    api_url = None if url_path == DEFAULT_DASHBOARD else url_path
    if url_path not in known or not known[url_path][2]:
        yaml = [d for d in (ha.call("lovelace/dashboards/list") or []) if d.get("url_path") == url_path]
        if yaml:
            raise ValueError("dashboard /%s is a YAML dashboard and can't be changed" % url_path)
        if not valid_new_url(url_path):
            raise ValueError("new dashboard URL must contain a hyphen and only a-z, 0-9, _ (e.g. cable-modem)")
        ha.call("lovelace/dashboards/create", url_path=url_path, title=settings["title"],
                icon="mdi:router-network", show_in_sidebar=True, require_admin=False)
        log("Created dashboard /%s" % url_path)
        config = {"title": settings["title"], "views": []}
    else:
        try:
            config = ha.call("lovelace/config", url_path=api_url, force=False) or {}
        except HAError as e:
            if e.code != "config_not_found":
                raise
            config = None
        if config is None or "strategy" in config or "views" not in config:
            raise ValueError("dashboard /%s is generated automatically. Open it, choose "
                             "\u22ee \u2192 Edit dashboard \u2192 Take control, then try again "
                             "(or pick another dashboard)" % url_path)
    views = config.setdefault("views", [])
    for i, v in enumerate(views):
        if v.get("path") == VIEW_PATH:
            views[i] = view
            action = "Replaced"
            break
    else:
        views.append(view)
        action = "Added"
    ha.call("lovelace/config/save", url_path=api_url, config=config)
    n_cards = sum(len(s["cards"]) - 1 for s in view["sections"])
    log("%s view /%s/%s with %d cards (%d view(s) in this dashboard)" % (
        action, url_path, VIEW_PATH, n_cards, len(views)))
    return url_path


def cmd_create(payload):
    settings = load_settings(payload)
    ha = connect()
    try:
        registry = ha.call("config/entity_registry/list")
        serial, mapping = find_entities(registry, settings["uid_prefix"], settings["serial"])
        latency = find_latency(registry)
        ent = Entities(mapping)
        have_plotly, have_apex = installed_cards(ha)
        if settings["cards"] == "builtin":
            have_plotly = have_apex = False
        elif settings["cards"] == "hacs":
            have_plotly = have_apex = True
        log("Modem %s: %d entities, %d latency target(s); plotly-graph %s, apexcharts-card %s" % (
            serial, len(mapping), len(latency), "yes" if have_plotly else "no", "yes" if have_apex else "no"))

        view = build_dashboard(ent, settings, have_plotly, have_apex, latency)
        targets = dashboard_targets(ha, settings)
        url_path = write_view(ha, settings, view, targets)

        # keep the select in sync (a new dashboard now exists)
        try:
            settings["url_path"] = url_path
            publish_target_select(ha, settings, settings["uid_prefix"] + serial, dashboard_targets(ha, settings))
        except Exception as e:  # not critical
            log("could not update the Dashboard Target select: %s" % e)

        missing = []
        if settings["cards"] != "builtin":
            if not have_plotly and settings["include"]["spectrum"]:
                missing.append("plotly-graph (channel spectrum)")
            if not have_apex:
                missing.append("apexcharts-card (charts with coloured ranges)")
        hint = ""
        if missing:
            hint = ("\n\nInstall from the Marketplace/HACS for more charts, then press "
                    "*Create Dashboard* again: " + ", ".join(missing) + ".")
        ha.call("call_service", domain="persistent_notification", service="create",
                service_data={"title": "TC4400 dashboard ready",
                              "message": "View [TC4400](/%s/%s) was written to dashboard /%s for modem %s.%s"
                                         % (url_path, VIEW_PATH, url_path, serial, hint),
                              "notification_id": "tc4400_dashboard"})
    except ValueError as e:
        try:
            ha.call("call_service", domain="persistent_notification", service="create",
                    service_data={"title": "TC4400 dashboard not created", "message": str(e),
                                  "notification_id": "tc4400_dashboard"})
        finally:
            raise
    finally:
        ha.ws.close()


def cmd_refresh():
    settings = load_settings("")
    ha = connect()
    try:
        serial, _ = find_entities(ha.call("config/entity_registry/list"), settings["uid_prefix"])
        targets = dashboard_targets(ha, settings)
        state = publish_target_select(ha, settings, settings["uid_prefix"] + serial, targets)
        log("Dashboard Target: %d choice(s), current: %s" % (len(targets), state))
    finally:
        ha.ws.close()


def cmd_select(label):
    url_path = label_to_url(label)
    if not url_path:
        raise ValueError("unknown dashboard choice: %s" % label)
    store_target(url_path)
    mqtt_publish(TOPIC_BASE + "/target", label.strip())
    log("Dashboard Target set to /%s" % url_path)


def main(argv):
    command = argv[1] if len(argv) > 1 else "create"
    arg = argv[2] if len(argv) > 2 else ""
    if command == "refresh":
        cmd_refresh()
    elif command == "select":
        cmd_select(arg)
    elif command == "create":
        cmd_create(arg)
    else:  # backwards compatible: tc4400_dashboard.py PAYLOAD
        cmd_create(command)


if __name__ == "__main__":
    try:
        main(sys.argv)
    except Exception as e:  # report every failure in the app log
        log("ERROR: %s" % e)
        sys.exit(1)
