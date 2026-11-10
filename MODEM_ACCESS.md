# Reaching the modem's web interface

The scraper reads the status pages of the modem's web interface, normally
`http://192.168.100.1`. **Home Assistant must be able to open that page.** With the
modem behind your own router (pfSense, OPNsense, OpenWrt, …) this usually does not
work out of the box and needs one extra step on the router — see below.

## 1. Test whether the modem is reachable

**From a PC in the same network as Home Assistant**, open
[`http://192.168.100.1`](http://192.168.100.1) in a browser. The TC4400 shows a login
prompt (user `admin`). If the page loads, Home Assistant can most likely reach it
too.

**From Home Assistant itself** (Terminal & SSH app, or *Advanced SSH & Web Terminal*):

```sh
curl -s -o /dev/null -w '%{http_code}\n' --connect-timeout 5 http://192.168.100.1/
```

| Result | Meaning |
|---|---|
| `200` or `401` | Reachable — the scraper will work |
| `000` after 5 s | Not reachable — set up the router (below) |

**From Windows** (PowerShell): `Test-NetConnection 192.168.100.1 -Port 80` —
`TcpTestSucceeded : True` means reachable.

**From the scraper app**: the log shows `Scrape failed (exit code 22)` while the
modem can't be reached.

## 2. Why it often doesn't work

A cable modem in bridge mode keeps its own management address (TC4400:
`192.168.100.1`). Your router's WAN interface, however, usually has a public IP from
the provider, so it has no address in `192.168.100.0/24`. Requests from Home
Assistant then go out the default route to the internet and never arrive.

## 3. The fix

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

## 4. Still not reachable?

- **Test from the router first.** pfSense: *Diagnostics → Ping*, host `192.168.100.1`,
  source address = the virtual IP. If even this fails, the virtual IP is on the wrong
  interface or the modem is not in bridge mode.
- **Check the ARP table** (pfSense: *Diagnostics → ARP Table*, OPNsense:
  *Interfaces → Diagnostics → ARP Table*). No entry for `192.168.100.1`: the router
  can't see the modem on that interface. An entry with the MAC address of a
  different device: something else uses `192.168.100.0/24`.
- **Router reachable, Home Assistant not:** the outbound NAT rule is missing or its
  source doesn't match Home Assistant's address, or a policy-routing rule sends the
  traffic to a VPN/other gateway (see *Policy routing* above).
- **Works for a while, then stops** (often after a modem reboot or resync): add a
  static ARP entry (see above).
- **Firewall rules between VLANs:** Home Assistant's VLAN must be allowed to reach
  `192.168.100.1` on port 80.
