"""IPv4 interface enumeration (so SSDP/WOL reach TVs on any link, not only the default route)."""
import fcntl
import ipaddress
import json
import socket
import struct
import subprocess


def interfaces():
    """[{name, ip, broadcast}] for every up, non-loopback IPv4 interface."""
    try:
        out = subprocess.run(["ip", "-j", "-4", "addr", "show", "up"], capture_output=True, text=True, timeout=3).stdout
        data = json.loads(out or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return _ioctl_interfaces()
    res = []
    for link in data:
        # Wi-Fi, USB NICs, bridges, VLANs alike (kernel view, whatever manages them: NM, networkd,
        # ifupdown); point-to-point tunnels (VPN, wg) have no broadcast domain.
        if link.get("ifname") == "lo" or {"LOOPBACK", "POINTOPOINT"} & set(link.get("flags", [])):
            continue
        for a in link.get("addr_info", []):
            if a.get("family") != "inet":
                continue
            net = ipaddress.IPv4Network("%s/%s" % (a["local"], a["prefixlen"]), strict=False)
            res.append({"name": link["ifname"], "ip": a["local"],
                        "broadcast": a.get("broadcast") or str(net.broadcast_address), "network": str(net)})
    return res


def for_ip(ip, ifaces=None):
    """Interface whose subnet contains ip, or None."""
    for i in ifaces if ifaces is not None else interfaces():
        if ipaddress.IPv4Address(ip) in ipaddress.IPv4Network(i["network"]):
            return i
    return None


def _ioctl_interfaces():
    """Fallback without iproute2: SIOCGIFFLAGS/ADDR/NETMASK per interface."""
    res = []
    try:
        names = [n for _, n in socket.if_nameindex()]
    except OSError:
        return res
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for n in names:
            req = struct.pack("256s", n.encode()[:15])
            try:
                flags = struct.unpack_from("H", fcntl.ioctl(s, 0x8913, req), 16)[0]
                if not flags & 0x1 or flags & 0x18:          # !UP, LOOPBACK|POINTOPOINT
                    continue
                ip = socket.inet_ntoa(fcntl.ioctl(s, 0x8915, req)[20:24])
                mask = socket.inet_ntoa(fcntl.ioctl(s, 0x891B, req)[20:24])
            except OSError:
                continue
            net = ipaddress.IPv4Network("%s/%s" % (ip, mask), strict=False)
            res.append({"name": n, "ip": ip, "broadcast": str(net.broadcast_address), "network": str(net)})
    return res
