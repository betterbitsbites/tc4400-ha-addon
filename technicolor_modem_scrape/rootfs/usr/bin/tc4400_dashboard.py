#!/usr/bin/env python3
"""Create or update a Home Assistant dashboard for the TC4400 modem scraper.

Part of the Technicolor TC4400 Modem Scraper app. Scraper by Philipp Reichmuth
(phrxmd/Technicolor_modem_scrape), based on scripts by mmiller7. CC-BY-NC-SA 4.0.

Usage: tc4400_dashboard.py [PAYLOAD]

PAYLOAD is what was published to the dashboard command topic:
  * empty or a modem serial number (the "Create Dashboard" button), or
  * a JSON object with any of these keys (missing keys use the app options):
      {"title": "Cable modem", "url_path": "cable-modem", "cards": "auto",
       "hours": 48, "serial": "CP1234ABC",
       "include": {"status": true, "gauges": true, "spectrum": true,
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
import sys
from urllib.parse import urlparse

OPTIONS_FILE = "/data/options.json"
SECTIONS = ("status", "gauges", "spectrum", "channel_table", "upstream",
            "downstream", "errors", "latency")

# Rough DOCSIS rules of thumb (256QAM downstream). Used for colours and bands.
DS_POWER_OK, DS_POWER_WARN = 7, 15          # |dBmV|
SNR_WARN, SNR_OK = 33, 36                   # dB
US_POWER_OK, US_POWER_WARN = 47, 51         # dBmV
GREEN, YELLOW, RED = "#2e7d32", "#f9a825", "#c62828"


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
                raise RuntimeError("%s failed: %s" % (msg_type, err.get("message", err)))
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
        "url_path": opts.get("dashboard_url", "cable-modem"),
        "cards": opts.get("dashboard_cards", "auto"),
        "hours": int(opts.get("dashboard_hours", 48)),
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
        for key in ("title", "url_path", "cards", "hours", "serial"):
            if key in req and req[key] not in (None, ""):
                settings[key] = req[key]
        for key, value in (req.get("include") or {}).items():
            if key in settings["include"]:
                settings["include"][key] = bool(value)
    elif payload and payload.lower() not in ("press", "create", "{}"):
        settings["serial"] = payload
    settings["hours"] = max(1, min(int(settings["hours"]), 720))
    if settings["cards"] not in ("auto", "builtin", "hacs"):
        raise ValueError("cards must be auto, builtin or hacs")
    url = str(settings["url_path"]).strip().lower()
    if not re.fullmatch(r"[a-z0-9_]+(-[a-z0-9_]+)+", url):
        raise ValueError("url_path must contain a hyphen and only a-z, 0-9, _ (e.g. cable-modem)")
    settings["url_path"] = url
    return settings


# --------------------------------------------------------------------------
# Entity lookup by unique_id
# --------------------------------------------------------------------------
def find_entities(registry, uid_prefix, serial=None):
    pattern = re.compile(
        r"^" + re.escape(uid_prefix) +
        r"(?P<serial>[A-Za-z0-9]+)_(?P<infix>status|summary|channels_down|channels_up|downstream\d+|upstream\d+)_(?P<key>\w+)$"
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


def plotly_spectrum(title, entity, value, unit, color_js):
    """Fritz!Box-style chart: one bar per channel at its frequency."""
    chans = "(hass.states['%s']?.attributes?.channels || [])" % entity
    return full({
        "type": "custom:plotly-graph",
        "title": title,
        "raw_plotly_config": True,
        "entities": [{
            "entity": entity,
            "name": "%s (%s)" % (value.upper() if value == "snr" else value.capitalize(), unit),
            "type": "bar",
            "x": "$ex %s.map(c => c.frequency / 1e6)" % chans,
            "y": "$ex %s.map(c => c.%s)" % (chans, value),
            "marker": {"color": "$ex %s.map(c => %s)" % (chans, color_js)},
            "hovertemplate": "%%{x:.1f} MHz<br>%%{y:.1f} %s<extra></extra>" % unit,
        }],
        "layout": {"height": 260, "showlegend": False, "bargap": 0.2,
                   "xaxis": {"type": "linear", "title": {"text": "MHz"}},
                   "yaxis": {"title": {"text": unit}}},
    })


def channel_table(title, entity, upstream=False):
    """Markdown table from the channel list attributes (built-in card)."""
    mark_ok = "🟢"
    mark_warn = "🟡"
    mark_bad = "🔴"
    if upstream:
        head = "| Ch | ID | Type | MHz | Power dBmV | Modulation | Lock |\n|---:|---:|:--|---:|---:|:--|:--|\n"
        row = ("| {{ c.channel }} | {{ c.id }} | {{ c.type }} | {{ fmt(c.frequency) }} | "
               "{{ v(c.power) }} {{ us(c.power) }} | {{ c.profile }} | {{ c.lockstatus }} |\n")
    else:
        head = ("| ID | Type | MHz | Power dBmV | SNR dB | Modulation | Corrected | Uncorr. |\n"
                "|---:|:--|---:|---:|---:|:--|---:|---:|\n")
        row = ("| {{ c.id }} | {{ c.type }} | {{ fmt(c.frequency) }} | {{ v(c.power) }} {{ dp(c.power) }} | "
               "{{ v(c.snr) }} {{ sn(c.snr) }} | {{ c.profile }} | {{ c.corr }} | {{ c.uncorr }} |\n")
    content = (
        "{%- macro fmt(f) -%}{{ (f / 1000000) | round(1) if f is number else '–' }}{%- endmacro -%}\n"
        "{%- macro v(x) -%}{{ x if x is number else '–' }}{%- endmacro -%}\n"
        "{%- macro dp(x) -%}{% if x is number %}{{ '" + mark_bad + "' if x | abs > " + str(DS_POWER_WARN) +
        " else ('" + mark_warn + "' if x | abs > " + str(DS_POWER_OK) + " else '" + mark_ok + "') }}{% endif %}{%- endmacro -%}\n"
        "{%- macro sn(x) -%}{% if x is number %}{{ '" + mark_bad + "' if x < " + str(SNR_WARN) +
        " else ('" + mark_warn + "' if x < " + str(SNR_OK) + " else '" + mark_ok + "') }}{% endif %}{%- endmacro -%}\n"
        "{%- macro us(x) -%}{% if x is number %}{{ '" + mark_bad + "' if x > " + str(US_POWER_WARN) +
        " else ('" + mark_warn + "' if x > " + str(US_POWER_OK) + " else '" + mark_ok + "') }}{% endif %}{%- endmacro -%}\n"
        "{%- set chans = state_attr('" + entity + "', 'channels') or [] -%}\n"
        + head +
        "{%- for c in chans | sort(attribute='frequency') %}\n" + row.rstrip("\n") + "\n{%- endfor %}\n"
    )
    return full({"type": "markdown", "title": title, "content": content})


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
def build_dashboard(ent, settings, have_plotly, have_apex):
    inc, hours = settings["include"], settings["hours"]
    sections = []

    def section(title, cards):
        cards = [c for c in cards if c]
        if cards:
            sections.append({"type": "grid", "cards": [heading(title)] + cards})

    E = ent.get
    ds_list, us_list = E("channels_down", "count"), E("channels_up", "count")

    if inc["status"]:
        rows = [
            (E("status", "connectivity"), "Connectivity"), (E("status", "boot"), "Boot state"),
            (E("status", "docsis"), "DOCSIS"), (E("status", "sw_version"), "Software"),
            (E("status", "uptime"), "Uptime"), (E("status", "last_boot"), "Last boot"),
            (E("status", "latency"), "TCP latency"),
            (E("summary", "down_locked"), "Downstream locked"), (E("summary", "down_channels"), "Downstream channels"),
            (E("summary", "up_locked"), "Upstream locked"), (E("summary", "up_channels"), "Upstream channels"),
            (E("summary", "corr"), "Corrected total"), (E("summary", "uncorr"), "Uncorrectable total"),
            (E("summary", "corr_rate"), "Corrected / min"), (E("summary", "uncorr_rate"), "Uncorrectable / min"),
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
            cards.append(gauge(E("summary", "snr_min"), "SNR (worst)", 25, 45,
                               severity={"red": 0, "yellow": SNR_WARN, "green": SNR_OK}))
        if E("summary", "up_power_max"):
            cards.append(gauge(E("summary", "up_power_max"), "US power (highest)", 30, 55,
                               severity={"green": 0, "yellow": US_POWER_OK, "red": US_POWER_WARN}))
        if E("summary", "down_power_min"):
            cards.append(gauge(E("summary", "down_power_min"), "DS power min", -20, 20, segments=ds_segments))
        if E("summary", "down_power_max"):
            cards.append(gauge(E("summary", "down_power_max"), "DS power max", -20, 20, segments=ds_segments))
        section("Signal now", cards)

    if inc["spectrum"] and have_plotly and ds_list:
        ds_power_color = ("Math.abs(c.power) > %d ? '%s' : (Math.abs(c.power) > %d ? '%s' : '%s')"
                          % (DS_POWER_WARN, RED, DS_POWER_OK, YELLOW, GREEN))
        snr_color = "c.snr < %d ? '%s' : (c.snr < %d ? '%s' : '%s')" % (SNR_WARN, RED, SNR_OK, YELLOW, GREEN)
        us_color = "c.power > %d ? '%s' : (c.power > %d ? '%s' : '%s')" % (US_POWER_WARN, RED, US_POWER_OK, YELLOW, GREEN)
        cards = [plotly_spectrum("Downstream power per channel", ds_list, "power", "dBmV", ds_power_color),
                 plotly_spectrum("Downstream SNR per channel", ds_list, "snr", "dB", snr_color)]
        if us_list:
            cards.append(plotly_spectrum("Upstream power per channel", us_list, "power", "dBmV", us_color))
        section("Channels (spectrum)", cards)

    if inc["channel_table"] and (ds_list or us_list):
        cards = []
        if ds_list:
            cards.append(channel_table("Downstream channels", ds_list))
        if us_list:
            cards.append(channel_table("Upstream channels", us_list, upstream=True))
        section("Channel table", cards)

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
        snr = [(e, n) for e, n in ((E("summary", "snr_min"), "Worst"), (E("summary", "snr_avg"), "Average")) if e]
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
        if E("summary", "uncorr"):
            cards.append(apex_columns("Uncorrectable errors per hour", E("summary", "uncorr"), "Uncorrectable",
                                      max(hours, 72), RED)
                         if have_apex else stat_bars("Uncorrectable errors per hour", E("summary", "uncorr"),
                                                     "Uncorrectable", "hour", 3))
        if E("summary", "corr"):
            cards.append(apex_columns("Corrected errors per hour", E("summary", "corr"), "Corrected",
                                      max(hours, 72), YELLOW)
                         if have_apex else stat_bars("Corrected errors per day", E("summary", "corr"),
                                                     "Corrected", "day", 30))
        section("Errors", cards)

    if inc["latency"] and E("status", "latency"):
        s = [(E("status", "latency"), "TCP")]
        section("Latency", [apex_line("TCP latency to the modem", s, hours, ymin=0)
                            if have_apex else history("TCP latency to the modem", s, hours)])

    return {
        "title": settings["title"],
        "views": [{"title": "TC4400", "path": "tc4400", "icon": "mdi:router-network",
                   "type": "sections", "max_columns": 3, "sections": sections}],
    }


def installed_cards(ha):
    try:
        resources = ha.call("lovelace/resources") or []
    except RuntimeError:
        resources = []
    urls = " ".join((r.get("url") or "").lower() for r in resources)
    return "plotly-graph" in urls, "apexcharts-card" in urls


def main(argv):
    settings = load_settings(argv[1] if len(argv) > 1 else "")
    url = os.environ.get("HA_WS_URL", "ws://supervisor/core/websocket")
    token = os.environ.get("HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise PermissionError("no token: SUPERVISOR_TOKEN missing (homeassistant_api not enabled?)")

    ha = HomeAssistant(url, token)
    try:
        serial, mapping = find_entities(ha.call("config/entity_registry/list"),
                                        settings["uid_prefix"], settings["serial"])
        ent = Entities(mapping)
        have_plotly, have_apex = installed_cards(ha)
        if settings["cards"] == "builtin":
            have_plotly = have_apex = False
        elif settings["cards"] == "hacs":
            have_plotly = have_apex = True
        log("Modem %s: %d entities; plotly-graph %s, apexcharts-card %s" % (
            serial, len(mapping), "yes" if have_plotly else "no", "yes" if have_apex else "no"))

        config = build_dashboard(ent, settings, have_plotly, have_apex)

        url_path = settings["url_path"]
        dashboards = ha.call("lovelace/dashboards/list") or []
        if not any(d.get("url_path") == url_path for d in dashboards):
            ha.call("lovelace/dashboards/create", url_path=url_path, title=settings["title"],
                    icon="mdi:router-network", show_in_sidebar=True, require_admin=False)
            log("Created dashboard /%s" % url_path)
        ha.call("lovelace/config/save", url_path=url_path, config=config)
        n_cards = sum(len(s["cards"]) - 1 for s in config["views"][0]["sections"])
        log("Saved dashboard /%s with %d cards" % (url_path, n_cards))

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
                              "message": "Dashboard [%s](/%s) was created/updated for modem %s.%s"
                                         % (settings["title"], url_path, serial, hint),
                              "notification_id": "tc4400_dashboard"})
    finally:
        ha.ws.close()


if __name__ == "__main__":
    try:
        main(sys.argv)
    except Exception as e:  # report every failure in the app log
        log("ERROR: %s" % e)
        sys.exit(1)
