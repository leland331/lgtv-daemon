"""Monitor topology: which DRM connectors are connected+enabled, and which one belongs to each TV.

Probed at runtime from /sys/class/drm/card*-*/{status,enabled,edid}; nothing is hardcoded."""
import ctypes
import ctypes.util
import glob
import os
import re
import struct
import subprocess


def parse_edid(data):
    """-> {'mfr': 'GSM', 'name': 'LG TV SSCR2', 'serial': ...} or {} for empty/invalid EDID."""
    if len(data) < 128 or data[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return {}
    v = (data[8] << 8) | data[9]
    mfr = "".join(chr(((v >> s) & 0x1F) + 64) for s in (10, 5, 0))
    out = {"mfr": mfr, "name": "", "serial": ""}
    for off in (54, 72, 90, 108):
        d = data[off:off + 18]
        if d[0:3] == b"\0\0\0" and d[3] in (0xFC, 0xFF):
            txt = d[5:18].split(b"\n")[0].decode("ascii", "replace").strip()
            out["name" if d[3] == 0xFC else "serial"] = txt
    return out


def connectors(root="/sys/class/drm", env=None, overlay=True):
    """[{connector, connected, enabled, dpms, mfr, name}]: DRM sysfs, corrected from the session
    (sysfs dpms is stale on NVIDIA proprietary and some others), or xrandr when sysfs has nothing."""
    res = _sysfs(root)
    if overlay:
        res = apply_session(res, *session_state(os.environ if env is None else env))
    return res


def _sysfs(root):
    res = []
    for path in sorted(glob.glob(os.path.join(root, "card*-*"))):
        base = os.path.basename(path)
        def rd(f, mode="r"):
            try:
                with open(os.path.join(path, f), mode) as fh:
                    return fh.read()
            except OSError:
                return b"" if "b" in mode else ""
        status = rd("status").strip()
        if not status:
            continue
        edid = parse_edid(rd("edid", "rb"))
        res.append({"connector": base.split("-", 1)[1], "card": base.split("-", 1)[0],
                    "connected": status == "connected",
                    "enabled": rd("enabled").strip() == "enabled",
                    "dpms": rd("dpms").strip() or "On",
                    "mfr": edid.get("mfr", ""), "name": edid.get("name", "")})
    return res


def is_lg(c):
    return c["mfr"] == "GSM"


def match(dev, conns):
    """Connector dict for this device, or None.

    1. user's explicit choice (dev['connector']), 2. LG EDID whose model name matches the device's
    model/name, 3. the only LG connector, if exactly one exists."""
    if dev.get("connector"):
        for c in conns:
            if c["connector"] == dev["connector"]:
                return c
        return None
    lg = [c for c in conns if is_lg(c) and c["connected"]]
    model = (dev.get("model") or "").upper()
    for c in lg:
        n = c["name"].upper()
        if n and model and (n in model or model in n or n.split()[-1] in model):
            return c
    if len(lg) == 1:
        return lg[0]
    return None


def display_on(c):
    """DPMS state the desktop chose for this output (passively read, never changed)."""
    return bool(c) and c.get("dpms", "On") == "On"


def active(c):
    """A TV's output counts as 'on' when connected and enabled (has a CRTC)."""
    return bool(c and c["connected"] and c["enabled"])


# ---------------------------------------------------------------- session fallbacks
def apply_session(conns, wl, glob_on, xr):
    """Pure merge (unit tested). wl: {connector: True/False/None power} for enabled Wayland outputs, or
    None; glob_on: whole-desktop DPMS (Mutter PowerSaveMode / X11 DPMS) or None; xr: xrandr list or None."""
    if not conns and xr:
        conns = xr                      # no DRM sysfs (e.g. nvidia without modeset): use X11's view
    for c in conns:
        if wl:
            c["enabled"] = c["connector"] in wl
            if wl.get(c["connector"]) is not None:
                c["dpms"] = "On" if wl[c["connector"]] else "Off"
                continue
        if glob_on is not None:
            c["dpms"] = "On" if glob_on else "Off"
    return conns


def session_state(env):
    wl = glob_on = xr = None
    try:
        if env.get("WAYLAND_DISPLAY"):
            wl = wayland_outputs(env)
        glob_on = mutter_power()
        if glob_on is None and env.get("DISPLAY") and not wl:
            glob_on = x11_dpms()
        if env.get("DISPLAY") and not wl:
            xr = xrandr()
    except Exception:
        pass
    return wl, glob_on, xr


def wayland_outputs(env):
    """{wl_output name (= DRM connector name): power on?} via wlr-output-power-management or KDE dpms;
    power is None when neither protocol exists. Only enabled outputs are advertised."""
    from .idle import _Wayland
    try:
        w = _Wayland(env)
        reg, g = w.globals()
        outs = {}
        # registry gives one entry per interface name; re-list to get every wl_output global
        reg2, cb = w.new_id(), w.new_id()
        w.send(1, 1, struct.pack("<I", reg2))
        w.send(1, 0, struct.pack("<I", cb))
        out_globals = []
        while True:
            obj, op, msg = w.read()
            if obj == cb:
                break
            if obj == reg2 and op == 0:
                gname, n = struct.unpack_from("<II", msg)
                if msg[8:8 + n - 1] == b"wl_output":
                    out_globals.append((gname, struct.unpack_from("<I", msg, 8 + n + (-n % 4))[0]))
        for gname, ver in out_globals:
            if ver < 4:
                return None             # no output names: can't map to connectors
            outs[w.bind(reg2, gname, "wl_output", 4)] = None
        mgr = kind = None
        if "zwlr_output_power_manager_v1" in g:
            kind, mgr = "wlr", w.bind(reg2, g["zwlr_output_power_manager_v1"][0], "zwlr_output_power_manager_v1", 1)
        elif "org_kde_kwin_dpms_manager" in g:
            kind, mgr = "kde", w.bind(reg2, g["org_kde_kwin_dpms_manager"][0], "org_kde_kwin_dpms_manager", 1)
        power = {}
        if mgr:
            for oid in outs:
                pid = w.new_id()
                w.send(mgr, 0, struct.pack("<II", pid, oid))
                power[pid] = oid
        names, state = {}, {}
        cb = w.new_id()
        w.send(1, 0, struct.pack("<I", cb))
        while True:
            obj, op, msg = w.read()
            if obj == cb:
                break
            if obj in outs and op == 4:
                n = struct.unpack_from("<I", msg)[0]
                names[obj] = msg[4:4 + n - 1].decode()
            elif obj in power and op in ((0,) if kind == "wlr" else (1,)):
                state[power[obj]] = struct.unpack_from("<I", msg)[0] == 0 if kind == "kde" \
                    else struct.unpack_from("<I", msg)[0] == 1
        w.sock.close()
        return {names[o]: state.get(o) for o in outs if o in names} or None
    except (OSError, ValueError, KeyError, struct.error):
        return None


def mutter_power():
    """GNOME: org.gnome.Mutter.DisplayConfig PowerSaveMode (0 on, 1-3 off), or None."""
    try:
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        v = bus.call_sync("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
                          "org.freedesktop.DBus.Properties", "Get",
                          GLib.Variant("(ss)", ("org.gnome.Mutter.DisplayConfig", "PowerSaveMode")),
                          None, Gio.DBusCallFlags.NONE, 500, None).unpack()[0]
        return None if v < 0 else v == 0
    except Exception:
        return None


_xdpy = []


def x11_dpms():
    """X11 DPMSInfo: True on, False standby/suspend/off, None unknown/disabled."""
    try:
        if not _xdpy:
            x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
            xext = ctypes.CDLL(ctypes.util.find_library("Xext") or "libXext.so.6")
            x11.XOpenDisplay.restype = ctypes.c_void_p
            xext.DPMSInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ushort), ctypes.POINTER(ctypes.c_ubyte)]
            dpy = x11.XOpenDisplay(None)
            if not dpy:
                return None
            _xdpy.extend([xext, dpy])
        xext, dpy = _xdpy
        lvl, on = ctypes.c_ushort(), ctypes.c_ubyte()
        if not xext.DPMSInfo(dpy, ctypes.byref(lvl), ctypes.byref(on)) or not on.value:
            return None
        return lvl.value == 0
    except (OSError, AttributeError):
        return None


def parse_xrandr(text):
    """xrandr --prop output -> connector dicts (names are X11's, e.g. HDMI-0 / DisplayPort-1 / DP-1-1)."""
    res, cur, hexbuf = [], None, None
    for line in text.splitlines():
        m = re.match(r"^(\S+) (connected|disconnected)( primary)?( \d+x\d+\+\d+\+\d+)?", line)
        if m:
            cur = {"connector": m.group(1), "card": "x11", "connected": m.group(2) == "connected",
                   "enabled": bool(m.group(4)), "dpms": "On", "mfr": "", "name": ""}
            res.append(cur)
            hexbuf = None
        elif cur is not None and line.strip() == "EDID:":
            hexbuf = ""
        elif hexbuf is not None:
            t = line.strip()
            if re.fullmatch(r"[0-9a-fA-F]{32}", t):
                hexbuf += t
            else:
                cur.update(parse_edid(bytes.fromhex(hexbuf)))
                cur.pop("serial", None)
                hexbuf = None
    if hexbuf and cur is not None:
        cur.update(parse_edid(bytes.fromhex(hexbuf)))
    return res


def xrandr():
    try:
        out = subprocess.run(["xrandr", "--prop"], capture_output=True, text=True, timeout=3).stdout
        return parse_xrandr(out) or None
    except (OSError, subprocess.TimeoutExpired):
        return None
