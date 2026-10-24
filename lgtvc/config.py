"""JSON configuration in ~/.config/lgtv-daemon/config.json."""
import json
import os

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "lgtv-daemon")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "lgtv-daemon")
LOG_PATH = os.path.join(STATE_DIR, "log.txt")

WOL_METHODS = ("broadcast", "subnet", "ip")

DEVICE_DEFAULTS = {
    "name": "",
    "ip": "",
    "macs": [],
    "key": "",                 # pairing key from the TV
    "enabled": True,           # "automatically manage this device"
    "wol": "broadcast",        # broadcast | subnet | ip
    "subnet": "255.255.255.0",
    "interface": "",           # "" = send WOL out of every interface, else e.g. eth1
    "ssl": True,               # wss on 3001 (newer firmware) or ws on 3000
    # HDMI input handling, same names/semantics as LGTV Companion (Common/device.h)
    "source_hdmi_input": 1,                    # the PC's HDMI input, 1-4
    "check_hdmi_input_when_power_off": False,  # leave TV alone if another input/app is in front
    "set_hdmi_input_on_power_on": False,
    "set_hdmi_input_on_power_on_delay": 1,     # seconds, 0-30
    "connector": "",           # "" = match by EDID automatically, else DRM connector name e.g. HDMI-A-1
    "model": "",               # model name from SSDP / EDID, used for matching
}

DEFAULTS = {
    "version": 1,
    "log": False,
    "tray": True,                 # show the tray icon
    "power_on_timeout": 40,
    "blank_instead_of_poweroff": False,
    "stay_off_on_network_wake": True,
    "persistent_connection": True,     # keep a connection open to each TV (fast power off at sleep)  # Wake-on-LAN wake: TV stays off until keyboard/mouse input
    "idle": {
        "enabled": False,
        "minutes": 10,
        "mute": False,
        "respect_inhibit": True,      # session idle-inhibit (video players, presentations)
        "respect_mpris": True,        # MPRIS players in Playing state
        "processes": [],              # process names that prevent idle mode while running
    },
    "topology": {"enabled": False, "keep_on_boot": False},
    "streaming": {"enabled": False, "prefer_power_off": True, "end_mode": "power_on"},  # power_on | nothing
    "api": {"enabled": False},
    "devices": [],
}


def _merge(defaults, data):
    out = {}
    for k, v in defaults.items():
        if isinstance(v, dict):
            out[k] = _merge(v, data.get(k, {}) if isinstance(data.get(k), dict) else {})
        else:
            out[k] = data.get(k, v)
    return out


def normalize(data):
    cfg = _merge(DEFAULTS, data or {})
    devs = []
    for i, d in enumerate(cfg["devices"]):
        dev = _merge(DEVICE_DEFAULTS, d)
        if isinstance(dev["macs"], str):
            dev["macs"] = [m for m in dev["macs"].replace(",", " ").split() if m]
        dev["macs"] = [normalize_mac(m) for m in dev["macs"]]
        if dev["wol"] not in WOL_METHODS:
            dev["wol"] = "broadcast"
        dev["source_hdmi_input"] = min(4, max(1, int(dev["source_hdmi_input"])))
        dev["set_hdmi_input_on_power_on_delay"] = min(30, max(0, int(dev["set_hdmi_input_on_power_on_delay"])))
        if not dev["name"]:
            dev["name"] = "Device%d" % (i + 1)
        devs.append(dev)
    cfg["devices"] = devs
    return cfg


def normalize_mac(mac):
    h = "".join(c for c in mac.lower() if c in "0123456789abcdef")
    if len(h) != 12:
        raise ValueError("invalid MAC address: %s" % mac)
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def load(path=CONFIG_PATH):
    try:
        with open(path) as f:
            return normalize(json.load(f))
    except FileNotFoundError:
        return normalize({})


def save(cfg, path=CONFIG_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(normalize(cfg), f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def find_devices(cfg, selectors):
    """Resolve 'Device1' style ids or friendly names; empty selector list = all devices."""
    devs = cfg["devices"]
    if not selectors:
        return list(devs)
    out = []
    for s in selectors:
        for i, d in enumerate(devs):
            if s.lower() == ("device%d" % (i + 1)) or s == d["name"]:
                if d not in out:
                    out.append(d)
    return out
