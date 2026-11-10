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
  `http://192.168.100.1`. Behind a router/firewall this usually needs one extra
  step — see [Reaching the modem through your router](#reaching-the-modem-through-your-router).

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

## Sensors

- **Modem**: connectivity, boot and security state, uptime, IP addresses
- **Down N** / **Up N**: per channel frequency, width, power, SNR (downstream),
  modulation, lock/bonding status, corrected/uncorrectable errors (downstream)
- **Summary**: channel and locked-channel counts, worst/average SNR, power ranges,
  error totals

An example dashboard is in the repository under `examples/dashboard.yaml`.

## Troubleshooting

| Log message | Meaning |
|---|---|
| `Scrape failed (exit code 22)` | Modem not reachable — see the router section below |
| `Scrape failed (exit code 21)` | Wrong modem username/password, or the page looks different |
| `Scrape failed (exit code 23)` | Page reached but could not be parsed (unsupported firmware?) |
| `Scrape failed (exit code 20)` | MQTT broker rejected the message (credentials, ACL) |
| `No MQTT broker found` | Install the Mosquitto broker app or set `mqtt_host` |

---

## Reaching the modem through your router

A cable modem in bridge mode keeps its own management address (TC4400:
`192.168.100.1`). Your router's WAN interface, however, usually has a public IP from
the provider, so it has no address in `192.168.100.0/24`. Requests from Home
Assistant then go out the default route to the internet and never arrive.

The fix on every router is the same idea:

1. Give the WAN interface an **additional address in the modem's subnet**
   (e.g. `192.168.100.2/24`). The router can now reach the modem directly and
   resolve its MAC address via ARP.
2. Add an **outbound NAT rule** so requests from your network to the modem leave
   with that address. The modem only answers its own subnet and has no route back
   to your LAN.
3. Make sure **no policy routing** sends this traffic to a VPN or other gateway.

Use any free address in the modem's subnet except `.1`. Don't use
`192.168.100.0/24` for one of your own LANs/VLANs — it would shadow the modem.

### pfSense

1. **Firewall → Virtual IPs → Add**
   - Type: **IP Alias**
   - Interface: **WAN** (the interface the modem is connected to)
   - Address: `192.168.100.2` / `24`
   - Description: `Modem access`
2. **Firewall → NAT → Outbound**
   - Mode: **Hybrid Outbound NAT** (keeps your automatic rules) → Save
   - **Add** a rule:
     - Interface: **WAN**
     - Source: the network or host of Home Assistant (e.g. `192.168.1.0/24`)
     - Destination: Network `192.168.100.1` / `32`
     - Translation / Address: the virtual IP `192.168.100.2`
   - Save → Apply Changes
3. **Policy routing** (only if you set a gateway on LAN/VLAN pass rules, e.g. for a
   VPN client or multi-WAN gateway group): on the interface Home Assistant lives on,
   add a pass rule **above** those rules with destination `192.168.100.1` and the
   gateway left at **Default**. A rule with a gateway forces packets to that
   gateway's MAC address, so they never reach the modem — a static ARP entry does
   not help in that case.
4. **Test**
   - **Diagnostics → Ping**: host `192.168.100.1`, source address `192.168.100.2`
   - **Diagnostics → ARP Table**: `192.168.100.1` should be listed on the WAN
     interface with the modem's MAC address.

Netgate's own guide (variant with a separate interface, needed for PPPoE WANs):
[Accessing a CPE/Modem from Inside the Firewall](https://docs.netgate.com/pfsense/en/latest/interfaces/accessing-modem-from-inside-firewall.html).

#### Static ARP entry (optional)

Normally not needed — the router learns the modem's MAC via ARP once step 1 is done.
Some modems stop answering ARP for a while after a reboot or resync; a static entry
then keeps the modem reachable. Take the MAC from **Diagnostics → ARP Table** after
a successful ping (it can differ from the "Cable Modem MAC" shown on the modem's
status page).

pfSense has no GUI for static ARP on a WAN interface. Install the **Shellcmd**
package and add a command of type *shellcmd*:

```sh
arp -S 192.168.100.1 aa:bb:cc:dd:ee:ff
```

(`-S` replaces an existing entry; `-s` fails if one exists.)

### OPNsense

1. **Interfaces → Virtual IPs → Settings → Add**
   - Mode: **IP Alias**, Interface: **WAN**, Network: `192.168.100.2/24`
2. **Firewall → NAT → Outbound**
   - Mode: **Hybrid outbound NAT rule generation** → Save
   - **Add**: Interface **WAN**, Source = Home Assistant's network,
     Destination `192.168.100.1/32`, Translation / target = `192.168.100.2`
3. **Policy routing**: if a LAN rule sets a gateway or gateway group, add a pass rule
   above it with destination `192.168.100.1` and gateway **default**.
4. **Static ARP (optional)**: newer versions have **Interfaces → Neighbors** for
   static ARP entries (IP `192.168.100.1`, MAC from **Interfaces → Diagnostics → ARP Table**).

### OpenWrt

Add a second address on the WAN device and put it into the `wan` firewall zone
(which already has masquerading enabled):

```sh
uci set network.modem=interface
uci set network.modem.proto='static'
uci set network.modem.device="$(uci get network.wan.device)"
uci set network.modem.ipaddr='192.168.100.2'
uci set network.modem.netmask='255.255.255.0'
uci add_list firewall.@zone[1].network='modem'   # @zone[1] is usually "wan" - check with: uci show firewall
uci commit && /etc/init.d/network reload && /etc/init.d/firewall reload
```

### Other routers (Fritz!Box, consumer routers)

Many consumer routers already NAT traffic to `192.168.100.1` out of the box — try
opening `http://192.168.100.1` in a browser first. If that doesn't work, look for an
option like "modem access" or an additional WAN IP in the router's WAN settings;
without such an option the modem can't be reached from behind that router.
