#!/usr/bin/env python3
"""Internet latency probe for the Technicolor TC4400 Modem Scraper app.

Pings one or more targets continuously (ICMP echo, or TCP connect as fallback)
and publishes rolling statistics to MQTT with Home Assistant discovery:

  average, P95, min, max, jitter and packet loss over the last window,
  base latency (10th percentile of the last 15 minutes), congestion threshold
  (base + threshold_ms) and a status: Line clear / Unstable / Congested /
  Packet loss / Down.

Settings come from the app options (/data/options.json) or environment:
  LATENCY_TARGETS   comma separated, default 1.1.1.1
  LATENCY_INTERVAL  seconds between probes per target, default 1
  LATENCY_WINDOW    seconds per published sample, default 10
  LATENCY_THRESHOLD ms above base latency that counts as congestion, default 30
  LATENCY_METHOD    auto | icmp | tcp, default auto
  LATENCY_TCP_PORT  default 443
MQTT: MQTT_HOST, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD, MQTT_TOPIC_PREFIX
(published with mosquitto_pub). DRY_RUN=1 prints instead of publishing.

Only the Python standard library is used.
"""
import json
import math
import os
import random
import select
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import deque

OPTIONS_FILE = "/data/options.json"
BASE_SPAN = 15 * 60          # seconds of history for the base latency
DISCOVERY_REFRESH = 3600     # re-publish discovery configs every hour
RESET_AFTER = 30             # seconds of total loss before the probe is recreated


def log(msg):
    print("[latency] %s" % msg, flush=True)


def load_settings():
    opts = {}
    try:
        with open(OPTIONS_FILE) as f:
            opts = json.load(f)
    except (OSError, ValueError):
        pass

    def pick(opt, env, default, cast):
        value = os.environ.get(env)
        if value in (None, ""):
            value = opts.get(opt, default)
        return cast(value)

    targets = os.environ.get("LATENCY_TARGETS")
    targets = [t.strip() for t in targets.split(",")] if targets else opts.get("latency_targets", ["1.1.1.1"])
    targets = [t for t in targets if t] or ["1.1.1.1"]
    s = {
        "targets": targets,
        "interval": pick("latency_interval", "LATENCY_INTERVAL", 1, float),
        "window": pick("latency_window", "LATENCY_WINDOW", 10, int),
        "threshold": pick("latency_threshold", "LATENCY_THRESHOLD", 30, float),
        "method": pick("latency_method", "LATENCY_METHOD", "auto", str),
        "tcp_port": pick("latency_tcp_port", "LATENCY_TCP_PORT", 443, int),
    }
    s["interval"] = max(0.2, s["interval"])
    s["window"] = max(int(math.ceil(s["interval"] * 3)), s["window"])
    return s


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------
def checksum(data):
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


class IcmpProbe:
    """ICMP echo over a raw socket (needs CAP_NET_RAW, which the app has as root).

    Every echo request gets a new random identifier, like ping tools do. With a
    fixed identifier, routers and NAT (pf, Docker) keep one ICMP state alive
    forever because a new request arrives every second; after a WAN/VPN
    reconnect that stale state sends the replies the wrong way and the probe
    never recovers. Replies are matched by a random token in the payload, so
    NAT that rewrites the identifier doesn't matter either.
    """
    name = "icmp"

    def __init__(self, ip):
        self.ip, self.seq = ip, 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
        self.unmatched = 0   # echo replies from the target that didn't match (late or rewritten)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def probe(self, timeout):
        self.seq = (self.seq + 1) & 0xFFFF
        ident = random.getrandbits(16)
        token = os.urandom(8)
        payload = token + b"tc4400-latency"
        header = struct.pack("!BBHHH", 8, 0, 0, ident, self.seq)
        packet = struct.pack("!BBHHH", 8, 0, checksum(header + payload), ident, self.seq) + payload
        start = time.perf_counter()
        self.sock.sendto(packet, (self.ip, 0))
        deadline = start + timeout
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([self.sock], [], [], remaining)
            if not ready:
                return None
            data, addr = self.sock.recvfrom(2048)
            now = time.perf_counter()
            ihl = (data[0] & 0x0F) * 4
            if len(data) < ihl + 8 or addr[0] != self.ip or data[ihl] != 0:
                continue  # not an echo reply from our target
            if data[ihl + 8:ihl + 16] == token:
                return (now - start) * 1000.0
            self.unmatched += 1


class TcpProbe:
    """TCP connect time; a refused connection (RST) is a valid round trip too."""
    name = "tcp"
    unmatched = 0

    def __init__(self, ip, port):
        self.ip, self.port = ip, port

    def close(self):
        pass

    def probe(self, timeout):
        start = time.perf_counter()
        try:
            with socket.create_connection((self.ip, self.port), timeout=timeout):
                pass
        except ConnectionRefusedError:
            pass
        except OSError:
            return None
        return (time.perf_counter() - start) * 1000.0


def make_probe(ip, method, port):
    if method in ("auto", "icmp"):
        try:
            p = IcmpProbe(ip)
            if method == "icmp" or any(p.probe(2.0) is not None for _ in range(3)):
                return p
            p.close()
            log("%s: no ICMP reply, falling back to TCP port %d" % (ip, port))
        except PermissionError:
            if method == "icmp":
                raise
            log("%s: ICMP not permitted, using TCP port %d" % (ip, port))
    return TcpProbe(ip, port)


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------
def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * p / 100.0
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def window_stats(samples, history, threshold_ms):
    """samples: [rtt or None] of the last window; history: [rtt] of the last 15 min."""
    ok = [s for s in samples if s is not None]
    total = len(samples)
    loss = 100.0 * (total - len(ok)) / total if total else None
    jitter = None
    if len(ok) >= 2:
        jitter = sum(abs(a - b) for a, b in zip(ok, ok[1:])) / (len(ok) - 1)
    base = percentile(history, 10) if history else None
    threshold = base + threshold_ms if base is not None else None
    avg = sum(ok) / len(ok) if ok else None
    p95 = percentile(ok, 95)
    if not ok:
        status = "Down"
    elif loss > 10:
        status = "Packet loss"
    elif threshold is not None and avg > threshold:
        status = "Congested"
    elif loss > 0 or (threshold is not None and p95 > threshold):
        status = "Unstable"
    else:
        status = "Line clear"

    def r(x):
        return None if x is None else round(x, 1)
    return {"avg": r(avg), "p95": r(p95), "min": r(min(ok) if ok else None), "max": r(max(ok) if ok else None),
            "jitter": r(jitter), "loss": r(loss), "base": r(base), "threshold": r(threshold),
            "status": status, "samples": total}


# --------------------------------------------------------------------------
# MQTT
# --------------------------------------------------------------------------
class Mqtt:
    def __init__(self):
        self.dry = os.environ.get("DRY_RUN") == "1"
        self.args = ["mosquitto_pub", "-h", os.environ.get("MQTT_HOST", "127.0.0.1"),
                     "-p", str(os.environ.get("MQTT_PORT") or 1883)]
        if os.environ.get("MQTT_USERNAME"):
            self.args += ["-u", os.environ["MQTT_USERNAME"]]
        if os.environ.get("MQTT_PASSWORD"):
            self.args += ["-P", os.environ["MQTT_PASSWORD"]]
        self.lock = threading.Lock()

    def publish(self, topic, payload, retain=False):
        message = json.dumps(payload) if not isinstance(payload, str) else payload
        if self.dry:
            print("[dry-run] %s%s: %s" % ("-r " if retain else "", topic, message), flush=True)
            return True
        cmd = self.args + (["-r"] if retain else []) + ["-t", topic, "-m", message]
        with self.lock:
            try:
                subprocess.run(cmd, check=True, timeout=15, capture_output=True)
                return True
            except (subprocess.SubprocessError, OSError) as e:
                log("MQTT publish to %s failed: %s" % (topic, e))
                return False


def slug(target):
    return "".join(c if c.isalnum() else "_" for c in target.lower())


DEVICE = {"identifiers": ["tc4400_scraper_internet_latency"], "name": "Internet Latency",
          "manufacturer": "TC4400 Modem Scraper", "model": "Latency probe"}

SENSORS = [  # key, name, unit, device_class, state_class, icon
    ("status", "Status", None, None, None, "mdi:speedometer"),
    ("avg", "Average", "ms", "duration", "measurement", "mdi:timer-outline"),
    ("p95", "P95", "ms", "duration", "measurement", "mdi:timer-alert-outline"),
    ("min", "Min", "ms", "duration", "measurement", "mdi:arrow-collapse-down"),
    ("max", "Max", "ms", "duration", "measurement", "mdi:arrow-collapse-up"),
    ("jitter", "Jitter", "ms", "duration", "measurement", "mdi:sine-wave"),
    ("loss", "Loss", "%", None, "measurement", "mdi:lan-disconnect"),
    ("base", "Base Latency", "ms", "duration", "measurement", "mdi:ray-start-arrow"),
    ("threshold", "Congestion Threshold", "ms", "duration", "measurement", "mdi:ray-vertex"),
]


def publish_discovery(mqtt, prefix, target, state_topic, window):
    disc = prefix.rsplit("/", 1)[0]  # homeassistant/sensor -> homeassistant
    for key, name, unit, dclass, sclass, icon in SENSORS:
        cfg = {"name": "%s %s" % (target, name), "state_topic": state_topic,
               "value_template": "{{ value_json.%s }}" % key,
               "unique_id": "inetlat_%s_%s" % (slug(target), key), "icon": icon,
               "expire_after": window * 6, "device": DEVICE}
        if key == "status":
            cfg["json_attributes_topic"] = state_topic
        if unit:
            cfg["unit_of_measurement"] = unit
        if dclass:
            cfg["device_class"] = dclass
        if sclass:
            cfg["state_class"] = sclass
        mqtt.publish("%s/sensor/inetlat_%s_%s/config" % (disc, slug(target), key), cfg, retain=True)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
class Target(threading.Thread):
    def __init__(self, name, index, settings, mqtt, prefix):
        super().__init__(daemon=True)
        self.name_, self.index, self.s, self.mqtt, self.prefix = name, index, settings, mqtt, prefix
        self.topic = "%s/internet_latency/%s" % (prefix, slug(name))

    def setup(self):
        """Resolve the target and pick the probe; retries until it works."""
        while True:
            try:
                ip = socket.gethostbyname(self.name_)
                return ip, make_probe(ip, self.s["method"], self.s["tcp_port"])
            except OSError as e:
                log("%s: %s - retrying in 60 s" % (self.name_, e))
                time.sleep(60)

    def run(self):
        s = self.s
        ip, probe = self.setup()
        log("%s (%s): probing via %s every %.1f s, publishing every %d s" % (
            self.name_, ip, probe.name, s["interval"], s["window"]))
        timeout = min(max(s["interval"], 1.0), 2.0)
        history = deque()           # (time, rtt) of successful probes, last 15 min
        samples = []
        next_publish = time.monotonic() + s["window"]
        next_discovery = 0
        last_ok = time.monotonic()
        down_since = None
        while True:
            t0 = time.monotonic()
            if t0 >= next_discovery:
                publish_discovery(self.mqtt, self.prefix, self.name_, self.topic, s["window"])
                next_discovery = t0 + DISCOVERY_REFRESH
            try:
                rtt = probe.probe(timeout)
            except OSError as e:
                if down_since is None:
                    log("%s: probe error: %s" % (self.name_, e))
                rtt = None
            samples.append(rtt)
            if rtt is not None:
                history.append((t0, rtt))
                last_ok = t0
                if down_since is not None:
                    log("%s: replies again after %d s (via %s)" % (self.name_, t0 - down_since, probe.name))
                    down_since = None
            elif t0 - last_ok >= RESET_AFTER:
                # No reply for a while: start over with a fresh socket and re-check the
                # method (and the address, for host names). Fixes stale NAT/socket state.
                if down_since is None:
                    down_since = last_ok
                    hint = (" (%d unmatched echo replies - NAT rewriting the packets?)" % probe.unmatched
                            if probe.unmatched else "")
                    log("%s: no reply for %d s via %s%s - recreating the probe" % (
                        self.name_, t0 - last_ok, probe.name, hint))
                probe.close()
                ip, probe = self.setup()
                last_ok = t0  # next reset at the earliest after another RESET_AFTER seconds
            while history and history[0][0] < t0 - BASE_SPAN:
                history.popleft()
            if time.monotonic() >= next_publish:
                stats = window_stats(samples, [h[1] for h in history], s["threshold"])
                stats.update(target=self.name_, ip=ip, method=probe.name, window_s=s["window"])
                self.mqtt.publish(self.topic, stats, retain=True)
                samples = []
                next_publish += s["window"]
            time.sleep(max(0.0, s["interval"] - (time.monotonic() - t0)))


def main():
    s = load_settings()
    prefix = os.environ.get("MQTT_TOPIC_PREFIX", "homeassistant/sensor")
    mqtt = Mqtt()
    log("targets: %s, threshold +%g ms, method %s" % (", ".join(s["targets"]), s["threshold"], s["method"]))
    threads = [Target(t, i, s, mqtt, prefix) for i, t in enumerate(s["targets"])]
    for t in threads:
        t.start()
    run_for = float(os.environ.get("LATENCY_RUN_SECONDS", "0"))  # for tests
    start = time.monotonic()
    while not run_for or time.monotonic() - start < run_for:
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
