"""What woke the PC: the network card (Wake-on-LAN) or something else (keyboard, mouse, power button).

Uses only what the kernel exposes to ordinary users, compared before and after the sleep:

  - each network card's wakeup counter   /sys/class/net/<if>/device/power/wakeup_count
  - the kernel's wakeup sources          /sys/class/wakeup/*/  (entries that belong to a network card)
  - the IRQ that woke the system         /sys/power/pm_wakeup_irq (when the platform records it)

Some machines record none of these for a Wake-on-LAN wake (the firmware handles the wake and the
kernel never sees it). For those, a system integrator can install a small root hook that finds out
in a machine-specific way and writes WAKE_SOURCE_FILE after each resume:

    {"time": <unix time, written after resume>, "source": "network" | "other", "detail": "<free text>"}

The daemon trusts that file when it was written after the sleep began. Anything unclear counts as an
ordinary wake, so the TV behaves as it always did."""
import json
import os
import time

WAKE_SOURCE_FILE = "/run/lgtv-daemon-wake.json"
NET_CLASS = "/sys/class/net"
WAKEUP_CLASS = "/sys/class/wakeup"
WAKEUP_IRQ = "/sys/power/pm_wakeup_irq"


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _int(path):
    try:
        return int(_read(path) or "")
    except ValueError:
        return None


def network_cards(net=NET_CLASS):
    """{interface name: real path of its hardware device} (virtual interfaces have no device)."""
    out = {}
    try:
        names = os.listdir(net)
    except OSError:
        return out
    for name in names:
        link = os.path.join(net, name, "device")
        if os.path.exists(link):
            out[name] = os.path.realpath(link)
    return out


def wakeup_counts(net=NET_CLASS, wakeup=WAKEUP_CLASS):
    """{key: count} for every wakeup counter that belongs to a network card."""
    cards = network_cards(net)
    out = {}
    for name, dev in cards.items():
        n = _int(os.path.join(dev, "power", "wakeup_count"))
        if n is not None:
            out[name] = n
    devs = {dev: name for name, dev in cards.items()}
    try:
        sources = os.listdir(wakeup)
    except OSError:
        sources = []
    for ws in sources:
        base = os.path.join(wakeup, ws)
        name = devs.get(os.path.realpath(os.path.join(base, "device")))   # the device that owns the source
        if name:
            n = _int(os.path.join(base, "wakeup_count"))
            if n is not None:
                out["%s/%s" % (name, ws)] = n
    return out


def card_irqs(net=NET_CLASS):
    """{irq number: interface name} for the network cards' interrupts (legacy and MSI)."""
    out = {}
    for name, dev in network_cards(net).items():
        irq = _int(os.path.join(dev, "irq"))
        if irq:
            out[irq] = name
        try:
            for m in os.listdir(os.path.join(dev, "msi_irqs")):
                if m.isdigit():
                    out[int(m)] = name
        except OSError:
            pass
    return out


def snapshot(net=NET_CLASS, wakeup=WAKEUP_CLASS):
    """Taken just before sleep; compared against after resume."""
    return {"time": time.time(), "counts": wakeup_counts(net, wakeup)}


def read_wake_source(path=WAKE_SOURCE_FILE):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def decide(before, after_counts, wake_irq, irqs, hook):
    """('network' | 'other', reason). Pure: all inputs are plain values so it can be tested.

    before: snapshot() from before the sleep (or None); after_counts: wakeup_counts() now;
    wake_irq: /sys/power/pm_wakeup_irq (or None); irqs: card_irqs(); hook: WAKE_SOURCE_FILE contents."""
    since = (before or {}).get("time", 0)
    if hook and hook.get("source") in ("network", "other") and hook.get("time", 0) >= since > 0:
        return hook["source"], "wake-source file: %s" % (hook.get("detail") or hook["source"])
    for key, n in sorted(after_counts.items()):
        was = (before or {}).get("counts", {}).get(key)
        if was is not None and n > was:
            return "network", "%s wakeup count went from %d to %d" % (key, was, n)
    if wake_irq and wake_irq in irqs:
        return "network", "woken by IRQ %d (%s)" % (wake_irq, irqs[wake_irq])
    return "other", "no network wake seen"


def detect(before, net=NET_CLASS, wakeup=WAKEUP_CLASS, irq_file=WAKEUP_IRQ, path=WAKE_SOURCE_FILE):
    return decide(before, wakeup_counts(net, wakeup), _int(irq_file), card_irqs(net), read_wake_source(path))
