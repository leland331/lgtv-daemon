#!/usr/bin/env python3
"""What woke the PC: the network card (Wake-on-LAN) or something else (keyboard, mouse, power button).

Standalone, standard library only: install.sh also copies this file as a systemd system-sleep hook
(/usr/lib/systemd/system-sleep/lgtv-daemon-wake), which runs as root around every suspend.

Why a root hook: on many desktops the kernel records nothing user-visible for a Wake-on-LAN wake.
The network card's own wakeup counter (/sys/class/net/<if>/device/power/wakeup_count) only moves
when the kernel's PCIe PME driver handles the wake, and on boards where the firmware owns PME
(common on AMD desktops) it stays at 0. What does survive is the PCIe root port's Root Status
register: it latches "PME Status" plus the requester ID (bus:device.function) of the device that
sent the wake message. That register needs root to read, so the hook clears it before suspend
('pre') and records it after resume ('post') in STATE_FILE, which the user daemon reads.

The daemon decides "network" if the recorded requester is a network card, or (on machines where the
kernel does count it) if a network card's wakeup counter went up across the sleep. Anything else,
including a missing hook, counts as an ordinary wake, so the TV behaves as it always did."""
import json
import os
import struct
import sys
import time

STATE_FILE = "/run/lgtv-daemon-wake.json"
PCI_DEVICES = "/sys/bus/pci/devices"
NET_CLASS = "/sys/class/net"

PCI_CAP_EXP = 0x10          # PCI Express capability ID
ROOT_STATUS = 0x20          # Root Status, offset inside the PCIe capability
PME_STATUS = 1 << 16        # RW1C: a PME message was received; bits 15:0 hold its requester ID
PORT_ROOT, PORT_RCEC = 0x4, 0xA


# ------------------------------------------------------------ config-space parsing (pure)
def pcie_cap(cfg):
    """Offset of the PCI Express capability in config space bytes, or None."""
    if len(cfg) < 0x40 or not (struct.unpack_from("<H", cfg, 0x06)[0] & 0x10):  # no capability list
        return None
    off, seen = cfg[0x34] & 0xFC, set()
    while off and off not in seen and off + 2 <= len(cfg):
        seen.add(off)
        if cfg[off] == PCI_CAP_EXP:
            return off
        off = cfg[off + 1] & 0xFC
    return None


def root_status(cfg):
    """(offset of Root Status, its value) for a PCIe root port, else None."""
    cap = pcie_cap(cfg)
    if cap is None or cap + ROOT_STATUS + 4 > len(cfg):
        return None
    port_type = (struct.unpack_from("<H", cfg, cap + 2)[0] >> 4) & 0xF
    if port_type not in (PORT_ROOT, PORT_RCEC):
        return None
    return cap + ROOT_STATUS, struct.unpack_from("<I", cfg, cap + ROOT_STATUS)[0]


def requester(port_addr, value):
    """'0000:06:00.0' style address of the device that sent the PME, from the Root Status value."""
    rid = value & 0xFFFF
    domain = port_addr.split(":")[0] if port_addr.count(":") == 2 else "0000"
    return "%s:%02x:%02x.%x" % (domain, rid >> 8, (rid >> 3) & 0x1F, rid & 7)


# ------------------------------------------------------------ root hook
def _root_ports(base):
    for addr in sorted(os.listdir(base)):
        path = os.path.join(base, addr, "config")
        try:
            with open(path, "rb") as f:
                cfg = f.read(256)
        except OSError:
            continue
        rs = root_status(cfg)
        if rs:
            yield addr, path, rs[0], rs[1]


def latched(base=PCI_DEVICES):
    """[{'port', 'requester'}] for every root port that has PME Status set."""
    return [{"port": a, "requester": requester(a, v)} for a, _, _, v in _root_ports(base) if v & PME_STATUS]


def clear(base=PCI_DEVICES):
    for _, path, off, v in _root_ports(base):
        if v & PME_STATUS:
            fd = os.open(path, os.O_WRONLY)
            try:
                os.pwrite(fd, struct.pack("<I", PME_STATUS), off)   # write 1 to clear
            finally:
                os.close(fd)


def _write_state(data, path):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def hook(phase, base=PCI_DEVICES, path=STATE_FILE, wait=0.5):
    """systemd-sleep calls this with 'pre' before suspend and 'post' after resume."""
    if phase == "pre":
        clear(base)
        _write_state({"phase": "pre", "time": time.time()}, path)
    elif phase == "post":
        pme = latched(base)
        end = time.monotonic() + wait
        while not pme and time.monotonic() < end:   # the wake message can trail the resume slightly
            time.sleep(0.1)
            pme = latched(base)
        _write_state({"phase": "post", "time": time.time(), "pme": pme}, path)


# ------------------------------------------------------------ user side
def network_cards(net=NET_CLASS):
    """{pci address: interface name} for network interfaces backed by a PCI device."""
    out = {}
    try:
        names = os.listdir(net)
    except OSError:
        return out
    for name in names:
        dev = os.path.realpath(os.path.join(net, name, "device"))
        if "/pci" in dev and os.path.exists(dev):
            out[os.path.basename(dev)] = name
    return out


def wakeup_counts(net=NET_CLASS):
    out = {}
    for name in network_cards(net).values():
        try:
            with open(os.path.join(net, name, "device", "power", "wakeup_count")) as f:
                out[name] = int(f.read().strip() or 0)
        except (OSError, ValueError):
            pass
    return out


def snapshot(net=NET_CLASS):
    """Taken just before sleep; compared against after resume."""
    return {"time": time.time(), "counts": wakeup_counts(net)}


def read_state(path=STATE_FILE):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def decide(before, after_counts, state, cards):
    """('network' | 'other', reason). Pure: all inputs are plain values so it can be tested.

    before: snapshot() from before the sleep (or None); after_counts: wakeup_counts() now;
    state: the hook's STATE_FILE contents (or None); cards: network_cards()."""
    since = (before or {}).get("time", 0)
    if state and state.get("phase") == "post" and state.get("time", 0) >= since:
        for p in state.get("pme") or []:
            if p.get("requester") in cards:
                return "network", "PCIe wake message from %s (%s)" % (p["requester"], cards[p["requester"]])
    for name, n in sorted(after_counts.items()):
        was = (before or {}).get("counts", {}).get(name)
        if was is not None and n > was:
            return "network", "%s wakeup count went from %d to %d" % (name, was, n)
    if not state or state.get("phase") != "post" or state.get("time", 0) < since:
        return "other", "no network wake seen (wake-source hook not installed or did not run)"
    if state.get("pme"):
        return "other", "woken by %s" % ", ".join(p["requester"] for p in state["pme"])
    return "other", "no network wake seen"


def detect(before, net=NET_CLASS, path=STATE_FILE):
    return decide(before, wakeup_counts(net), read_state(path), network_cards(net))


def main(argv):
    """Hook entry point: lgtv-daemon-wake pre|post [suspend|hibernate|...]. Never blocks sleep."""
    try:
        if len(argv) > 1:
            hook(argv[1])
    except Exception as e:   # noqa: BLE001 - a broken hook must never stop the PC sleeping
        print("lgtv-daemon-wake: %s" % e, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
