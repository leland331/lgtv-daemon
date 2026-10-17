"""Wake-on-LAN magic packets: broadcast, subnet-directed broadcast or unicast to the TV's IP."""
import ipaddress
import socket


def magic_packet(mac):
    h = mac.replace(":", "").replace("-", "")
    return b"\xff" * 6 + bytes.fromhex(h) * 16


def targets(method, ip, subnet="255.255.255.0", ifaces=None):
    """[(source_ip or "", dest)] - limited broadcast goes out of every interface (bound to its address,
    sent to its broadcast address) so isolated TV links work, not only the default route."""
    if method == "ip":
        return [("", ip)]
    if method == "subnet":
        return [("", str(ipaddress.IPv4Network("%s/%s" % (ip, subnet), strict=False).broadcast_address))]
    out = [("", "255.255.255.255")]
    for i in ifaces or []:
        out.append((i["ip"], i["broadcast"]))
    return out


def wake(dev, ifaces=None):
    from . import netif
    if ifaces is None:
        ifaces = netif.interfaces()
        if dev.get("interface"):
            ifaces = [i for i in ifaces if i["name"] == dev["interface"]]
    names = {i["ip"]: i["name"] for i in ifaces}
    sent = []
    for src, addr in targets(dev["wol"], dev["ip"], dev.get("subnet", "255.255.255.0"), ifaces):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                if src:
                    try:   # pin the egress device (e.g. Wi-Fi) where allowed; bind() alone is a hint
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, names[src].encode())
                    except (OSError, KeyError):
                        pass
                    s.bind((src, 0))
                for mac in dev["macs"]:
                    for port in (9, 7):
                        s.sendto(magic_packet(mac), (addr, port))
                sent.append((src, addr))
            except OSError:
                pass
    return sent
