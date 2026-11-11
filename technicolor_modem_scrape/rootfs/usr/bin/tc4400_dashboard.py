#!/usr/bin/env python3
"""Create or update a Home Assistant dashboard for the TC4400 modem scraper.

Part of the Technicolor TC4400 Modem Scraper app. Scraper by Philipp Reichmuth
(phrxmd/Technicolor_modem_scrape), based on scripts by mmiller7. CC-BY-NC-SA 4.0.

Usage:
  tc4400_dashboard.py create [PAYLOAD]   build the dashboard view
  tc4400_dashboard.py power-switch LABEL choose the modem power switch (select)
  tc4400_dashboard.py power-sensor LABEL choose the power draw sensor (select)
  tc4400_dashboard.py restart            power-cycle the modem via the switch
  tc4400_dashboard.py watch              follow the switch, record every power cycle
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
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

OPTIONS_FILE = "/data/options.json"
DATA_DIR = os.environ.get("DATA_DIR", "/data")
TARGET_FILE = os.path.join(DATA_DIR, "dashboard_target.json")
TOPIC_BASE = "tc4400_scraper/dashboard"
VIEW_PATH = "tc4400"
DEFAULT_DASHBOARD = "lovelace"   # the built-in "Overview" dashboard
POWER_FILE = os.path.join(DATA_DIR, "modem_power.json")
POWER_TOPIC = "tc4400_scraper/power"
RESTART_TOPIC = "tc4400_scraper/modem/restart"
NONE_LABEL = "— none —"
RESTART_COOLDOWN = 180           # seconds between two power cycles
# integrations that switch through a cloud: after "off" the internet is gone and
# "on" may never arrive, so the user gets a warning when choosing such a switch
CLOUD_PLATFORMS = {"tuya", "smartlife", "sonoff", "ewelink", "meross_cloud", "smartthings",
                   "switchbot_cloud", "alexa", "google_assistant", "ifttt", "magichome_cloud"}
SECTIONS = ("status", "power", "gauges", "internet", "spectrum", "channel_table", "upstream",
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
    # message via stdin (-s): a select with hundreds of options can exceed the
    # 128 KB limit of a single command line argument
    cmd = ["mosquitto_pub", "-h", os.environ.get("MQTT_HOST", "127.0.0.1"),
           "-p", str(os.environ.get("MQTT_PORT") or 1883), "-t", topic]
    cmd += ["-s"] if message else ["-n"]   # -n: empty message (removes a retained config)
    if retain:
        cmd.append("-r")
    if os.environ.get("MQTT_USERNAME"):
        cmd += ["-u", os.environ["MQTT_USERNAME"]]
    if os.environ.get("MQTT_PASSWORD"):
        cmd += ["-P", os.environ["MQTT_PASSWORD"]]
    r = subprocess.run(cmd, input=message.encode(), timeout=15, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("mosquitto_pub to %s failed: %s" % (topic, r.stderr.decode(errors="replace").strip()))


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
# --------------------------------------------------------------------------
# Modem power: switch for power cycling, sensor for the power draw
# --------------------------------------------------------------------------
def power_options():
    """Entity IDs from the app options (power_switch / power_sensor); they win
    over the selects on the device page."""
    try:
        with open(OPTIONS_FILE) as f:
            opts = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for key, opt in (("switch", "power_switch"), ("sensor", "power_sensor")):
        value = str(opts.get(opt) or "").strip()
        if re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", value):
            out[key] = value
        elif value:
            log("option %s ignored: '%s' is not an entity ID (e.g. switch.modem_plug)" % (opt, value))
    return out


def load_power():
    try:
        with open(POWER_FILE) as f:
            power = json.load(f)
    except (OSError, ValueError):
        power = {}
    power.update(power_options())
    return power


def store_power(data):
    """Save; values that only come from the app options are not written as the
    device selection, so clearing the option falls back to the select again."""
    try:
        with open(POWER_FILE) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    data = dict(data)
    for key in power_options():
        if key in raw:
            data[key] = raw[key]
        else:
            data.pop(key, None)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(POWER_FILE, "w") as f:
        json.dump(data, f)


def power_choices(ha):
    """(switches, power sensors) as [(label, entity_id)], sorted by name."""
    switches, sensors = [], []
    for s in ha.call("get_states") or []:
        eid, attrs = s.get("entity_id", ""), s.get("attributes") or {}
        label = "%s (%s)" % (attrs.get("friendly_name") or eid, eid)
        if eid.startswith("switch."):
            switches.append((label, eid))
        elif eid.startswith("sensor.") and (attrs.get("device_class") == "power"
                                            or attrs.get("unit_of_measurement") in ("W", "kW")):
            sensors.append((label, eid))
    key = lambda x: x[0].lower()
    return sorted(switches, key=key), sorted(sensors, key=key)


def label_to_entity(label):
    if not label or label.strip() == NONE_LABEL:
        return ""
    m = re.search(r"\(([a-z_]+\.[^)\s]+)\)\s*$", label)
    return m.group(1) if m else None


def get_state(ha, entity_id):
    for s in ha.call("get_states") or []:
        if s.get("entity_id") == entity_id:
            return s
    return None


def notify(ha, title, message, nid="tc4400_modem_power"):
    try:
        ha.call("call_service", domain="persistent_notification", service="create",
                service_data={"title": title, "message": message, "notification_id": nid})
    except Exception as e:
        log("could not create notification: %s" % e)


def publish_power(ha, modem_id):
    """Selects for switch and sensor, and the Restart Modem button (only with a switch)."""
    power = load_power()
    switches, sensors = power_choices(ha)
    disc = os.environ.get("MQTT_TOPIC_PREFIX", "homeassistant/sensor").rsplit("/", 1)[0]
    device = {"identifiers": [modem_id]}
    for kind, choices, name, icon in (("switch", switches, "Modem Power Switch", "mdi:power-socket-eu"),
                                      ("sensor", sensors, "Modem Power Sensor", "mdi:flash")):
        current = power.get(kind) or ""
        if current and current not in [c[1] for c in choices]:
            st = next((s for s in ha.call("get_states") or [] if s.get("entity_id") == current), None)
            if st:  # e.g. an input_boolean or light set in the app options
                choices = choices + [("%s (%s)" % ((st.get("attributes") or {}).get("friendly_name") or current,
                                                    current), current)]
        labels = [NONE_LABEL] + [c[0] for c in choices]
        state = next((c[0] for c in choices if c[1] == current), None)
        if current and state is None:  # chosen entity is gone: keep it visible
            state = "missing (%s)" % current
            labels.append(state)
        log("%s: %d choice(s)" % (name, len(labels) - 1))
        mqtt_publish("%s/select/%s_power_%s/config" % (disc, modem_id, kind), {
            "name": name, "unique_id": "%s_power_%s" % (modem_id, kind),
            "command_topic": "%s/%s/set" % (POWER_TOPIC, kind), "state_topic": "%s/%s" % (POWER_TOPIC, kind),
            "options": labels, "icon": icon, "entity_category": "config", "device": device})
        mqtt_publish("%s/%s" % (POWER_TOPIC, kind), state or NONE_LABEL)
    cycle_topic = "%s/sensor/%s_last_power_cycle/config" % (disc, modem_id)
    if power.get("switch"):
        mqtt_publish(cycle_topic, {
            "name": "Last Power Cycle", "unique_id": "%s_last_power_cycle" % modem_id,
            "state_topic": POWER_TOPIC + "/last_cycle", "value_template": "{{ value_json.on_at }}",
            "json_attributes_topic": POWER_TOPIC + "/last_cycle", "device_class": "timestamp",
            "icon": "mdi:power-cycle", "device": device})
        if power.get("last_cycle"):
            publish_last_cycle(power["last_cycle"])
    else:
        mqtt_publish(cycle_topic, "")
    button_topic = "%s/button/%s_restart_modem/config" % (disc, modem_id)
    if power.get("switch"):
        mqtt_publish(button_topic, {
            "name": "Restart Modem", "unique_id": "%s_restart_modem" % modem_id,
            "command_topic": RESTART_TOPIC, "payload_press": "restart",
            "icon": "mdi:restart-alert", "device": device})
    else:
        mqtt_publish(button_topic, "")  # removes the button
    return power


def cmd_power_select(kind, label):
    entity = label_to_entity(label)
    if entity is None:
        raise ValueError("unknown %s choice: %s" % (kind, label))
    try:
        with open(POWER_FILE) as f:
            power = json.load(f)
    except (OSError, ValueError):
        power = {}
    power[kind] = entity
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(POWER_FILE, "w") as f:
        json.dump(power, f)
    log("Modem power %s set to %s" % (kind, entity or "none"))
    if kind in power_options():
        log("note: the app option power_%s is set and takes precedence over this choice" % kind)
    settings = load_settings("")
    ha = connect()
    try:
        serial, _ = find_entities(ha.call("config/entity_registry/list"), settings["uid_prefix"])
        publish_power(ha, settings["uid_prefix"] + serial)
        if kind == "switch" and entity:
            reg = {e["entity_id"]: e for e in ha.call("config/entity_registry/list")}
            platform = (reg.get(entity) or {}).get("platform", "")
            if platform in CLOUD_PLATFORMS:
                notify(ha, "TC4400: check the modem power switch",
                       "**%s** is controlled through the *%s* integration, which usually works via "
                       "a cloud. When the modem is switched off, the internet is gone and the switch "
                       "may not be switched on again. Use a switch that works locally (Zigbee, "
                       "Z-Wave, Shelly, Tasmota, ESPHome, …)." % (entity, platform))
    finally:
        ha.ws.close()


def cmd_restart():
    """Power-cycle the modem: switch off, wait, switch on, verify; retries the
    switch-on, because the modem must not stay off."""
    power = load_power()
    switch = power.get("switch")
    try:
        with open(OPTIONS_FILE) as f:
            off_seconds = int(json.load(f).get("restart_off_seconds", 15))
    except (OSError, ValueError):
        off_seconds = int(os.environ.get("RESTART_OFF_SECONDS", 15))
    ha = connect()
    try:
        if not switch:
            notify(ha, "TC4400: modem not restarted",
                   "No power switch selected. Choose one under *Modem Power Switch* on the TC4400 device.")
            log("restart requested, but no power switch selected")
            return
        since = time.time() - float(power.get("last_restart", 0))
        if since < RESTART_COOLDOWN:
            notify(ha, "TC4400: modem not restarted",
                   "The modem was power-cycled %d s ago. Wait at least %d s between restarts."
                   % (since, RESTART_COOLDOWN))
            log("restart refused: last restart %d s ago" % since)
            return
        state = get_state(ha, switch)
        if state is None:
            notify(ha, "TC4400: modem not restarted", "Power switch **%s** does not exist." % switch)
            log("restart refused: %s does not exist" % switch)
            return
        if state.get("state") == "unavailable":
            notify(ha, "TC4400: modem not restarted", "Power switch **%s** is unavailable." % switch)
            log("restart refused: %s is unavailable" % switch)
            return
        power["last_restart"] = time.time()
        store_power(power)
        log("Restarting modem: %s off for %d s" % (switch, off_seconds))
        notify(ha, "TC4400: restarting modem",
               "Switching **%s** off for %d s. The internet will be down for a few minutes." % (switch, off_seconds))
        ha.call("call_service", domain=switch.split(".")[0], service="turn_off", target={"entity_id": switch})
        for _ in range(10):
            time.sleep(1)
            s = get_state(ha, switch)
            if s and s.get("state") == "off":
                break
        else:
            log("%s did not report 'off' - switching it on again anyway" % switch)
        time.sleep(max(0, off_seconds - 1))
    finally:
        ha.ws.close()

    # switch on, with retries and a fresh connection each time
    ok, last_error = False, ""
    for attempt in range(1, 7):
        try:
            ha = connect()
            try:
                ha.call("call_service", domain=switch.split(".")[0], service="turn_on", target={"entity_id": switch})
                for _ in range(15):
                    time.sleep(1)
                    s = get_state(ha, switch)
                    if s and s.get("state") == "on":
                        ok = True
                        break
                if ok:
                    log("%s is on again - the modem needs 2-5 minutes to resync" % switch)
                    notify(ha, "TC4400: modem restarted",
                           "**%s** is on again. The modem needs about 2-5 minutes to resync." % switch)
                    try:
                        ha.call("call_service", domain="logbook", service="log",
                                service_data={"name": "TC4400", "entity_id": switch,
                                              "message": "power-cycled by the TC4400 Modem Scraper app"})
                    except Exception:
                        pass
                    return
                last_error = "state is still '%s'" % (s or {}).get("state")
            finally:
                ha.ws.close()
        except Exception as e:
            last_error = str(e)
        log("switching %s on failed (attempt %d/6): %s" % (switch, attempt, last_error))
        time.sleep(10)
    ha = connect()
    try:
        notify(ha, "TC4400: MODEM IS STILL OFF",
               "Could not switch **%s** on again (%s). Switch it on manually!" % (switch, last_error))
    finally:
        ha.ws.close()
    raise RuntimeError("could not switch %s on again: %s" % (switch, last_error))


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def parse_time(value):
    """Epoch seconds from an ISO string or a number (compressed history format)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def publish_last_cycle(cycle):
    """State of the "Last Power Cycle" sensor (retained)."""
    mqtt_publish(POWER_TOPIC + "/last_cycle", {
        "on_at": iso(cycle.get("on")), "off_at": iso(cycle.get("off")),
        "off_seconds": round(cycle["on"] - cycle["off"]) if cycle.get("on") and cycle.get("off") else None,
        "source": cycle.get("source") or "unknown", "switch": cycle.get("switch")})


def record_cycle(cycle):
    """Store and publish a power cycle if it is newer than the known one."""
    power = load_power()
    known = (power.get("last_cycle") or {}).get("on") or 0
    if cycle.get("on") and cycle["on"] > known + 1:
        power["last_cycle"] = cycle
        store_power(power)
        publish_last_cycle(cycle)
        log("Power cycle of %s: off %s, on %s (%s)" % (cycle.get("switch"), iso(cycle.get("off")),
                                                     iso(cycle.get("on")), cycle.get("source")))
        return True
    return False


def backfill_cycle(ha, switch, days=30):
    """Last off -> on of the switch from the recorder history (app wasn't running)."""
    start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        hist = ha.call("history/history_during_period", start_time=start, entity_ids=[switch],
                       minimal_response=True, no_attributes=True, significant_changes_only=False)
    except HAError as e:
        log("history not available: %s" % e)
        return
    prev_state, off_at, last = None, None, None
    for item in (hist or {}).get(switch, []):
        state = item.get("s", item.get("state"))
        ts = parse_time(item.get("lu", item.get("lc", item.get("last_changed", item.get("last_updated")))))
        if state == "off" and prev_state != "off":
            off_at = ts
        elif state == "on" and prev_state == "off":
            last = {"switch": switch, "off": off_at, "on": ts, "source": "unknown (from history)"}
        prev_state = state
    if last:
        record_cycle(last)


def describe_source(ha, context, power, off_at):
    """Who switched the modem off: this app, a user, an automation/script or the device."""
    if off_at and abs(off_at - float(power.get("last_restart", 0))) < 30:
        return "Restart Modem button (app)"
    context = context or {}
    if context.get("user_id"):
        try:
            users = {u["id"]: u.get("name") for u in ha.call("config/auth/list") or []}
            return "user %s" % (users.get(context["user_id"]) or context["user_id"][:8])
        except Exception:
            return "user"
    if context.get("parent_id"):
        return "automation or script"
    return "device or integration"


def cmd_watch():
    """Long-running: follow the selected power switch and record every off -> on,
    no matter who switched it. Reconnects on errors and when the switch changes."""
    import socket as _socket
    while True:
        switch = load_power().get("switch")
        if not switch:
            time.sleep(60)
            continue
        try:
            ha = connect()
        except Exception as e:
            log("watcher: cannot connect: %s - retrying in 60 s" % e)
            time.sleep(60)
            continue
        try:
            backfill_cycle(ha, switch)
            sub_id = None
            for key in ("trigger", "platform"):  # newer / older trigger syntax
                try:
                    sub_id = ha.next_id
                    ha.call("subscribe_trigger", trigger={key: "state", "entity_id": switch})
                    break
                except HAError:
                    sub_id = None
            if sub_id is None:
                raise RuntimeError("subscribe_trigger failed")
            log("watching %s for power cycles" % switch)
            ha.ws.sock.settimeout(60)
            off_at, off_ctx = None, None
            s = get_state(ha, switch)
            if s and s.get("state") == "off":
                off_at, off_ctx = parse_time(s.get("last_changed")), s.get("context")
            while True:
                try:
                    msg = ha.ws.recv()
                except _socket.timeout:
                    if load_power().get("switch") != switch:
                        log("watcher: power switch changed")
                        break
                    ha.ws.send({"id": ha.next_id, "type": "ping"})
                    ha.next_id += 1
                    continue
                if msg.get("id") != sub_id or msg.get("type") != "event":
                    continue
                trig = ((msg.get("event") or {}).get("variables") or {}).get("trigger") or {}
                old, new = trig.get("from_state") or {}, trig.get("to_state") or {}
                if new.get("state") == "off" and old.get("state") != "off":
                    off_at, off_ctx = parse_time(new.get("last_changed")) or time.time(), new.get("context")
                elif new.get("state") == "on" and old.get("state") == "off":
                    power = load_power()
                    record_cycle({"switch": switch, "off": off_at, "on": parse_time(new.get("last_changed")) or time.time(),
                                  "source": describe_source(ha, off_ctx, power, off_at)})
                    off_at, off_ctx = None, None
        except Exception as e:
            log("watcher: %s - reconnecting in 30 s" % e)
            time.sleep(30)
        finally:
            try:
                ha.ws.close()
            except Exception:
                pass


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


def width(card, columns):
    """Card width in grid columns (12 per section column; HA clamps it on phones)."""
    card["grid_options"] = {"columns": columns}
    return card


CHART_HEIGHT = 230


def apex_height(card, height=CHART_HEIGHT):
    card.setdefault("apex_config", {}).setdefault("chart", {})["height"] = height
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
    return full(apex_height(card))


def apex_columns(title, rate_entity, name, hours, color):
    """Errors per hour as columns, from a per-minute rate sensor (average per hour
    x 60). The rate is already reset-safe (modem reboot, counter reset), unlike
    differences of the totals, which turn negative when the modem restarts."""
    return full(apex_height({
        "type": "custom:apexcharts-card",
        "graph_span": "%dh" % hours,
        "header": {"show": True, "title": title},
        "yaxis": [{"min": 0, "decimals": 0}],
        "series": [{"entity": rate_entity, "name": name, "type": "column", "color": color,
                    "group_by": {"func": "avg", "duration": "1h"},
                    "transform": "return x === null ? null : Math.round(Number(x) * 60);"}],
    }))


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
        "layout": {"height": CHART_HEIGHT, "showlegend": False, "bargap": 0,
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
    return full(apex_height({
        "type": "custom:apexcharts-card",
        "graph_span": "%dh" % hours,
        "update_interval": "1min",
        "header": {"show": True, "title": title, "show_states": True, "colorize_states": True},
        "yaxis": [{"min": 0, "max": "~60", "decimals": 0}],
        "series": series,
    }))


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
def qam_vs_ofdm(E):
    """SC-QAM and OFDM side by side: channels, SNR, error totals and rates.
    They are never mixed: OFDM counts codewords (billions) and measures SNR differently."""
    keys = {"sc_snr_min": E("summary", "snr_min"), "sc_snr_avg": E("summary", "snr_avg"),
            "sc_corr": E("summary", "corr"), "sc_uncorr": E("summary", "uncorr"),
            "sc_corr_rate": E("summary", "corr_rate"), "sc_uncorr_rate": E("summary", "uncorr_rate"),
            "down": E("summary", "down_channels"), "of_n": E("summary", "ofdm_channels"),
            "of_snr_min": E("summary", "ofdm_snr_min"),
            "of_corr": E("summary", "ofdm_corr"), "of_uncorr": E("summary", "ofdm_uncorr"),
            "of_corr_rate": E("summary", "ofdm_corr_rate"), "of_uncorr_rate": E("summary", "ofdm_uncorr_rate")}
    if not (keys["sc_uncorr"] or keys["of_uncorr"]):
        return None
    sets = "".join("{%%- set %s = states('%s') -%%}\n" % (k, e) for k, e in keys.items() if e)
    sets += "".join("{%%- set %s = 'unknown' -%%}\n" % k for k, e in keys.items() if not e)
    content = (
        sets +
        # number with thin-space thousands separators, '–' when unknown
        "{%- macro n(x, d=0) -%}{%- if x | is_number -%}"
        "{{ ('{:,.' ~ d ~ 'f}').format(x | float) | replace(',', '\u2009') }}"
        "{%- else -%}–{%- endif -%}{%- endmacro -%}\n"
        # rate: red when uncorrectable errors are happening right now
        "{%- macro r(x, bad) -%}{%- if bad and x | is_number and x | float > 0 -%}"
        "<font color=\"" + RED + "\">{{ n(x, 1) }}</font>{%- else -%}{{ n(x, 1) }}{%- endif -%}{%- endmacro -%}\n"
        "{%- set sc_n = (down | int(0)) - (of_n | int(0)) if down | is_number else 'unknown' -%}\n"
        "| | SC-QAM | <font color=\"" + OFDM_BLUE + "\">OFDM</font> |\n"
        "|:--|--:|--:|\n"
        "| Channels | {{ n(sc_n) }} | {{ n(of_n) }} |\n"
        "| SNR worst | {{ n(sc_snr_min, 1) }} dB | {{ n(of_snr_min, 1) }} dB |\n"
        "| SNR average | {{ n(sc_snr_avg, 1) }} dB | |\n"
        "| Uncorrectable / min | {{ r(sc_uncorr_rate, true) }} | {{ r(of_uncorr_rate, true) }} |\n"
        "| Corrected / min | {{ r(sc_corr_rate, false) }} | {{ r(of_corr_rate, false) }} |\n"
        "| Uncorrectable since boot | {{ n(sc_uncorr) }} | {{ n(of_uncorr) }} |\n"
        "| Corrected since boot | {{ n(sc_corr) }} | {{ n(of_corr) }} |\n"
        "\n<small>OFDM counts codewords and measures SNR differently, so the two are never "
        "added up. Corrected OFDM codewords are normal.</small>"
    )
    return {"type": "markdown", "title": "SC-QAM vs OFDM", "content": content}


def modem_rows(power, E, with_power=True, with_status=True):
    """Compact list for the Modem section: restart, power and device facts."""
    rows = []
    if with_power and power:
        if power.get("button"):
            rows.append({
                "type": "button", "name": "Restart modem", "icon": "mdi:restart-alert", "action_name": "Restart",
                "tap_action": {"action": "perform-action", "perform_action": "button.press",
                               "target": {"entity_id": power["button"]},
                               "confirmation": {"text": "Power-cycle the modem now? The internet will be "
                                                        "down for a few minutes."}}})
        if power.get("switch"):
            rows.append({"entity": power["switch"], "name": "Modem power", "type": "simple-entity"})
        if power.get("sensor"):
            rows.append({"entity": power["sensor"], "name": "Power draw"})
        if power.get("cycle"):
            rows.append({"entity": power["cycle"], "name": "Last power cycle", "format": "relative"})
            rows.append({"type": "attribute", "entity": power["cycle"], "attribute": "source",
                         "name": "Triggered by", "icon": "mdi:account-cog"})
    if with_status:
        for entity, name, extra in ((E("status", "last_boot"), "Last modem boot", {"format": "relative"}),
                                    (E("status", "boot"), "Boot state", {}),
                                    (E("status", "docsis"), "DOCSIS", {}),
                                    (E("status", "sw_version"), "Software", {})):
            if entity:
                rows.append(dict({"entity": entity, "name": name}, **extra))
    return rows


def build_dashboard(ent, settings, have_plotly, have_apex, latency=None, power=None):
    """Rows of sections whose widths add up to the 3 columns, so related things stay
    together and nothing drifts: overview (connection | signal | modem),
    downstream | upstream, internet latency | stability, errors, channel table.
    On narrower screens the sections stack in the same order."""
    inc, hours = settings["include"], settings["hours"]
    sections = []

    def section(title, cards, span=1):
        cards = [c for c in cards if c]
        if cards:
            sec = {"type": "grid", "cards": [heading(title)] + cards}
            if span > 1:
                sec["column_span"] = span
            sections.append(sec)
            return True
        return False

    def half(card, span):          # two cards side by side in a section of `span` columns
        return width(card, 6 * span)

    def line(title, series, h, bands=None, ymin=None, ymax=None, decimals=1):
        if have_apex:
            return apex_line(title, series, h, bands, ymin, ymax, decimals)
        return history(title, series, h)

    E = ent.get
    ds_list, us_list = E("channels_down", "count"), E("channels_up", "count")
    hist = E("channel_history", "scrapes")
    lat_status = [k["status"] for k in (latency or {}).values() if k.get("status")]

    # ---- row 1: overview -------------------------------------------------
    if inc["status"]:
        tiles = [(E("status", "connectivity"), "Connectivity", "mdi:lan-connect"),
                 (E("status", "uptime"), "Uptime", "mdi:timer-outline"),
                 (E("summary", "down_locked"), "Downstream locked", "mdi:download-network"),
                 (E("summary", "up_locked"), "Upstream locked", "mdi:upload-network"),
                 (lat_status[0] if lat_status and inc["internet"] else None, "Internet", "mdi:earth"),
                 (E("status", "latency"), "Modem response", "mdi:timer-sand")]
        section("Connection", [{"type": "tile", "entity": e, "name": n, "icon": i} for e, n, i in tiles if e]
                + [qam_vs_ofdm(E)])

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
            cards.append(gauge(E("summary", "up_power_max"), "Upstream max", 30, 55,
                               severity={"green": 0, "yellow": US_POWER_OK, "red": US_POWER_WARN}))
        if E("summary", "down_power_min"):
            cards.append(gauge(E("summary", "down_power_min"), "Downstream min", -20, 20, segments=ds_segments))
        if E("summary", "down_power_max"):
            cards.append(gauge(E("summary", "down_power_max"), "Downstream max", -20, 20, segments=ds_segments))
        section("Signal", cards)

    rows = modem_rows(power, E, with_power=inc["power"], with_status=inc["status"])
    section("Modem", [{"type": "entities", "show_header_toggle": False, "entities": rows}] if rows else [])

    # ---- row 2: downstream | upstream -----------------------------------
    spectrum = inc["spectrum"] and have_plotly
    ds_power_color = ("Math.abs(c.power) > %d ? '%s' : (Math.abs(c.power) > %d ? '%s' : '%s')"
                      % (DS_POWER_WARN, RED, DS_POWER_OK, YELLOW, GREEN))
    snr_color = ("/OFDM/.test(c.type || '') ? '%s' : (c.snr < %d ? '%s' : (c.snr < %d ? '%s' : '%s'))"
                 % (OFDM_BLUE, SNR_WARN, RED, SNR_OK, YELLOW, GREEN))
    us_color = "c.power > %d ? '%s' : (c.power > %d ? '%s' : '%s')" % (US_POWER_WARN, RED, US_POWER_OK, YELLOW, GREEN)

    cards = []
    if spectrum and ds_list:
        cards += [half(plotly_spectrum("Power per channel", ds_list, "power", "dBmV", ds_power_color,
                                       (-DS_POWER_WARN - 3, DS_POWER_WARN + 3),
                                       [(DS_POWER_OK, YELLOW), (-DS_POWER_OK, YELLOW),
                                        (DS_POWER_WARN, RED), (-DS_POWER_WARN, RED)]), 2),
                  half(plotly_spectrum("SNR per channel", ds_list, "snr", "dB", snr_color,
                                       (20, 45), [(SNR_OK, YELLOW), (SNR_WARN, RED)]), 2)]
    if inc["downstream"]:
        snr = [(e, n) for e, n in ((E("summary", "snr_min"), "Worst SC-QAM"), (E("summary", "snr_avg"), "Average SC-QAM"),
                                   (E("summary", "ofdm_snr_min"), "Worst OFDM")) if e]
        pwr = [(e, n) for e, n in ((E("summary", "down_power_min"), "Min"), (E("summary", "down_power_max"), "Max")) if e]
        if snr:
            cards.append(half(line("SNR", snr, hours, bands_snr(), 25, 45), 2))
        if pwr:
            cards.append(half(line("Power range", pwr, hours, bands_ds_power(), -20, 20), 2))
    section("Downstream", cards, span=2)

    cards = []
    if spectrum and us_list:
        cards.append(plotly_spectrum("Power per channel", us_list, "power", "dBmV", us_color,
                                     (30, 56), [(US_POWER_OK, YELLOW), (US_POWER_WARN, RED)]))
    if inc["upstream"]:
        series = [(e, "Up %d" % n) for n, e in ent.channels("upstream", "power")]
        if series:
            cards.append(line("Power history", series, hours, bands_us_power(), 30, 55))
    section("Upstream", cards)

    # ---- row 3: internet latency | stability -----------------------------
    lat_cards = []
    if inc["internet"] and latency:
        lh = settings["latency_hours"]
        for slug, keys in latency.items():
            name = slug.replace("_", ".")
            lat_cards.append(latency_summary(name, keys))
            if have_apex:
                lat_cards.append(apex_latency("Latency %s" % name, keys, lh))
            else:
                s = [(keys[k], n) for k, n in (("avg", "Average"), ("max", "Max"), ("base", "Base latency"),
                                              ("threshold", "Congestion threshold")) if keys.get(k)]
                lat_cards.append(history("Latency %s" % name, s, lh))
    has_latency = section("Internet latency", lat_cards, span=2)

    cards = []
    locked = [(e, n) for e, n in ((E("summary", "down_locked"), "Downstream"), (E("summary", "up_locked"), "Upstream")) if e]
    if inc["upstream"] and locked:
        cards.append(line("Locked channels (7 days)", locked, max(hours, 168), ymin=0, decimals=0))
    if inc["latency"] and E("status", "latency"):
        cards.append(line("Response time of the modem", [(E("status", "latency"), "TCP")], hours, ymin=0, decimals=0))
    if has_latency:
        section("Stability", cards)
    else:
        section("Stability", [half(c, 3) for c in cards], span=3)

    # ---- row 4: errors ---------------------------------------------------
    if inc["errors"]:
        cards = []
        # ApexCharts: from the reset-safe rate sensors. Built-in fallback: long-term
        # statistics of the totals (total_increasing, HA treats a drop as a reset).
        for key, rate, title, name, color, fallback in (
                ("uncorr", "uncorr_rate", "Uncorrectable per hour (SC-QAM)", "Uncorrectable", RED, ("hour", 3)),
                ("corr", "corr_rate", "Corrected per hour (SC-QAM)", "Corrected", YELLOW, ("day", 30)),
                ("ofdm_uncorr", "ofdm_uncorr_rate", "Uncorrectable codewords per hour (OFDM)", "OFDM uncorrectable",
                 OFDM_BLUE, ("hour", 3)),
                # corrected codewords are normal on OFDM (LDPC); useful to compare before/after
                ("ofdm_corr", "ofdm_corr_rate", "Corrected codewords per hour (OFDM)", "OFDM corrected",
                 "#90caf9", ("day", 30))):
            if have_apex and E("summary", rate):
                cards.append(half(apex_columns(title, E("summary", rate), name, max(hours, 72), color), 3))
            elif E("summary", key):
                cards.append(half(stat_bars(title, E("summary", key), name, *fallback), 3))
        section("Errors", cards, span=3)

    # ---- row 5: channel table ---------------------------------------------
    if inc["channel_table"] and (ds_list or us_list):
        cards = []
        if ds_list:
            cards.append(channel_table("Downstream channels", ds_list, history=hist))
        if us_list:
            cards.append(channel_table("Upstream channels", us_list, upstream=True, history=hist))
        section("Channels", cards, span=3)

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

        power = load_power()
        modem_id = settings["uid_prefix"] + serial
        by_uid = {e.get("unique_id"): e["entity_id"] for e in registry if not e.get("disabled_by")}
        if power.get("switch"):
            power["button"] = by_uid.get("%s_restart_modem" % modem_id)
            power["cycle"] = by_uid.get("%s_last_power_cycle" % modem_id)
        power = {k: v for k, v in power.items() if k in ("switch", "sensor", "button", "cycle") and v}
        if power:
            power["last_boot"] = ent.get("status", "last_boot")
        view = build_dashboard(ent, settings, have_plotly, have_apex, latency, power)
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
    """Publish the selects and the power entities. Every part on its own, so one
    failure doesn't hide the others; exit code 1 if something failed."""
    settings = load_settings("")
    ha = connect()
    failed = []
    try:
        serial, _ = find_entities(ha.call("config/entity_registry/list"), settings["uid_prefix"])
        modem_id = settings["uid_prefix"] + serial
        try:
            targets = dashboard_targets(ha, settings)
            state = publish_target_select(ha, settings, modem_id, targets)
            log("Dashboard Target: %d choice(s), current: %s" % (len(targets), state))
        except Exception as e:
            failed.append("Dashboard Target: %s" % e)
        try:
            power = publish_power(ha, modem_id)
            log("Modem power switch: %s, sensor: %s%s" % (
                power.get("switch") or "none", power.get("sensor") or "none",
                " - Restart Modem button and Last Power Cycle published" if power.get("switch") else ""))
        except Exception as e:
            failed.append("modem power entities: %s" % e)
    finally:
        ha.ws.close()
    for f in failed:
        log("ERROR: %s" % f)
    if failed:
        sys.exit(1)


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
    elif command in ("power-switch", "power-sensor"):
        cmd_power_select(command.split("-")[1], arg)
    elif command == "restart":
        cmd_restart()
    elif command == "watch":
        cmd_watch()
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
