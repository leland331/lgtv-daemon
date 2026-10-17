"""SSDP discovery of LG WebOS TVs."""
import concurrent.futures
import json
import re
import select
import socket
import subprocess
import time
import urllib.request

ST = "urn:lge-com:service:webos-second-screen:1"
# Fixed local port for M-SEARCH replies, so a firewall rule can allow exactly this (see firewall/).
REPLY_PORT = 41900


def scan(timeout=3.0):
    from . import netif
    found = {}
    socks = []
    for src in [i["ip"] for i in netif.interfaces()] or [""]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            if src:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(src))
            try:
                s.bind((src, REPLY_PORT))
            except OSError:
                s.bind((src, 0))     # port busy: still scan, the firewall may drop replies
            s.setblocking(False)
            socks.append(s)
        except OSError:
            pass
    return _scan(socks, found, timeout)


def _scan(socks, found, timeout):
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
           "MX: 2\r\nST: %s\r\n\r\n" % ST).encode()
    end = time.time() + timeout
    for s in socks:
        for _ in range(2):
            try:
                s.sendto(msg, ("239.255.255.250", 1900))
            except OSError:
                pass
    while time.time() < end and socks:
        r, _, _ = select.select(socks, [], [], 0.5)
        for s in r:
          try:
            data, (ip, _) = s.recvfrom(4096)
          except OSError:
            continue
          text = data.decode(errors="replace")
          if ip in found or "webos" not in text.lower() and "lge" not in text.lower():
            continue
          loc = re.search(r"(?im)^location:\s*(\S+)", text)
          found[ip] = {"ip": ip, "name": "", "model": "", "location": loc.group(1) if loc else ""}
    for s in socks:
        s.close()
    for d in found.values():
        if d["location"]:
            try:
                xml = urllib.request.urlopen(d["location"], timeout=2).read().decode(errors="replace")
                m = re.search(r"<friendlyName>(.*?)</friendlyName>", xml)
                d["name"] = m.group(1).strip() if m else ""
                m = re.search(r"<modelName>(.*?)</modelName>", xml)
                d["model"] = m.group(1).strip() if m else ""
            except OSError:
                pass
        d["name"] = d["name"] or "LG TV " + d["ip"]
    return list(found.values())


def mac_for_ip(ip):
    """Read the TV's MAC from the kernel ARP/neighbour table (after we've talked to it)."""
    try:
        with open("/proc/net/arp") as f:
            for line in f.readlines()[1:]:
                p = line.split()
                if p[0] == ip and p[3] != "00:00:00:00:00:00":
                    return p[3]
    except OSError:
        pass
    return ""


def neighbour_probe(timeout=0.6):
    """Fallback when SSDP replies are blocked (e.g. ufw drops unicast replies): try the WebOS
    ports on every IPv4 neighbour the kernel knows about, on every interface."""
    try:
        out = subprocess.run(["ip", "-j", "-4", "neigh"], capture_output=True, text=True, timeout=3).stdout
        neigh = [n for n in json.loads(out or "[]") if n.get("lladdr") and "FAILED" not in n.get("state", [])]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []

    def probe(n):
        for port in (3001, 3000):
            try:
                from .ws import WebSocket
                url = ("wss://%s:3001/" if port == 3001 else "ws://%s:3000/") % n["dst"]
                WebSocket(url, timeout=timeout * 2).close()   # only a WebOS TV upgrades here
                return {"ip": n["dst"], "name": "LG TV " + n["dst"], "model": "", "location": "",
                        "mac": n["lladdr"], "interface": n.get("dev", "")}
            except Exception:
                pass
        return None
    with concurrent.futures.ThreadPoolExecutor(32) as ex:
        return [r for r in ex.map(probe, neigh) if r]


def discover(timeout=3.0):
    """SSDP plus neighbour fallback, merged by IP; MACs filled from the neighbour table."""
    res = {d["ip"]: d for d in scan(timeout)}
    for d in neighbour_probe():
        res.setdefault(d["ip"], d)
    for d in res.values():
        d.setdefault("mac", mac_for_ip(d["ip"]))
    return list(res.values())
