"""User idle detection.

Backends (picked at runtime from the session type and what the compositor offers):
  wayland  - ext-idle-notify-v1 (KDE, wlroots, Hyprland, niri, COSMIC...), tiny pure-Python wire client
  gnome    - org.gnome.Mutter.IdleMonitor.GetIdletime (GNOME Wayland has no ext-idle-notify)
  x11      - XScreenSaverQueryInfo via libXss
Each backend calls on_idle() / on_active()."""
import ctypes
import ctypes.util
import os
import socket
import struct
import threading
import time


ORDER = ("wayland", "kwin", "gnome", "x11", "screensaver", "logind")


def select_backend(env, probes):
    """Pure selection logic (unit tested). probes: {backend: callable -> bool}; each is only called
    when reached. Order: ext-idle-notify-v1, org_kde_kwin_idle (older Plasma), Mutter IdleMonitor,
    X11 XScreenSaver, org.freedesktop.ScreenSaver, logind IdleHint."""
    wayland = bool(env.get("WAYLAND_DISPLAY")) and env.get("XDG_SESSION_TYPE", "") != "x11"
    for kind in ORDER:
        if kind in ("wayland", "kwin") and not wayland:
            continue
        if kind == "x11" and not env.get("DISPLAY"):
            continue
        p = probes.get(kind)
        if p:
            try:
                if p():
                    return kind
            except Exception:
                pass
    return None


# ---------------------------------------------------------------- Wayland wire client
def _wl_str(s):
    b = s.encode() + b"\0"
    return struct.pack("<I", len(b)) + b + b"\0" * (-len(b) % 4)


class _Wayland:
    def __init__(self, env=os.environ):
        path = env.get("WAYLAND_DISPLAY", "wayland-0")
        if not path.startswith("/"):
            path = os.path.join(env.get("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid()), path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.buf = b""
        self.next_id = 2

    def new_id(self):
        i = self.next_id
        self.next_id += 1
        return i

    def send(self, obj, opcode, payload=b""):
        self.sock.sendall(struct.pack("<II", obj, ((8 + len(payload)) << 16) | opcode) + payload)

    def read(self):
        while len(self.buf) < 8 or len(self.buf) < (struct.unpack_from("<I", self.buf, 4)[0] >> 16):
            d = self.sock.recv(4096)
            if not d:
                raise ConnectionError("wayland connection closed")
            self.buf += d
        obj, so = struct.unpack_from("<II", self.buf)
        size = so >> 16
        msg, self.buf = self.buf[8:size], self.buf[size:]
        return obj, so & 0xFFFF, msg

    def globals(self):
        reg, cb = self.new_id(), self.new_id()
        self.send(1, 1, struct.pack("<I", reg))   # wl_display.get_registry
        self.send(1, 0, struct.pack("<I", cb))    # wl_display.sync
        found = {}
        while True:
            obj, op, msg = self.read()
            if obj == cb:
                return reg, found
            if obj == reg and op == 0:
                name, n = struct.unpack_from("<II", msg)
                iface = msg[8:8 + n - 1].decode()
                ver = struct.unpack_from("<I", msg, 8 + n + (-n % 4))[0]
                found[iface] = (name, ver)
            if obj == 1 and op == 0:
                raise ConnectionError("wayland protocol error")

    def bind(self, reg, glob, iface, version):
        oid = self.new_id()
        self.send(reg, 0, struct.pack("<I", glob) + _wl_str(iface) + struct.pack("<II", version, oid))
        return oid


def wayland_globals(env=os.environ):
    try:
        w = _Wayland(env)
        _, g = w.globals()
        w.sock.close()
        return g
    except (OSError, ValueError, struct.error):
        return {}


class WaylandIdle:
    """ext-idle-notify-v1 (name 'wayland') or, on Plasma without it, org_kde_kwin_idle ('kwin')."""
    name = "wayland"

    def __init__(self, timeout_s, on_idle, on_active, respect_inhibit=True):
        self.timeout_ms = int(timeout_s * 1000)
        self.on_idle, self.on_active, self.respect_inhibit = on_idle, on_active, respect_inhibit
        self.w = None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                self._loop()
            except OSError:
                time.sleep(5)

    def _loop(self):
        self.w = w = _Wayland()
        reg, g = w.globals()
        seat = w.bind(reg, g["wl_seat"][0], "wl_seat", 1)
        if self.name == "kwin":
            mgr = w.bind(reg, g["org_kde_kwin_idle"][0], "org_kde_kwin_idle", 1)
            note = w.new_id()
            w.send(mgr, 0, struct.pack("<III", note, seat, self.timeout_ms))   # get_idle_timeout
            while True:
                obj, op, _ = w.read()
                if obj == note:
                    (self.on_idle if op == 0 else self.on_active)()   # idle / resumed
        nver = min(g["ext_idle_notifier_v1"][1], 2)
        notifier = w.bind(reg, g["ext_idle_notifier_v1"][0], "ext_idle_notifier_v1", nver)
        note = w.new_id()
        # opcode 1 = get_idle_notification (honours idle inhibitors, e.g. video players);
        # opcode 2 = get_input_idle_notification (v2, input only)
        op = 1 if self.respect_inhibit or nver < 2 else 2
        w.send(notifier, op, struct.pack("<III", note, self.timeout_ms, seat))
        while True:
            obj, op, _ = w.read()
            if obj == note:
                (self.on_idle if op == 0 else self.on_active)()

    def stop(self):
        if self.w:
            self.w.sock.close()


# ---------------------------------------------------------------- polling backends
class _Poller:
    def __init__(self, timeout_s, on_idle, on_active, respect_inhibit=True):
        self.timeout_s, self.on_idle, self.on_active = timeout_s, on_idle, on_active
        self.idle = False
        self.running = True

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.running = False

    def _run(self):
        while self.running:
            t = self.idle_seconds()
            if t is not None:
                if not self.idle and t >= self.timeout_s:
                    self.idle = True
                    self.on_idle()
                elif self.idle and t < self.timeout_s:
                    self.idle = False
                    self.on_active()
            time.sleep(0.5 if self.idle else 2)


def _mutter_idletime():
    from gi.repository import Gio, GLib
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    r = bus.call_sync("org.gnome.Mutter.IdleMonitor", "/org/gnome/Mutter/IdleMonitor/Core",
                      "org.gnome.Mutter.IdleMonitor", "GetIdletime", None, GLib.VariantType("(t)"),
                      Gio.DBusCallFlags.NONE, 1000, None)
    return r.unpack()[0] / 1000.0


def probe_mutter():
    try:
        _mutter_idletime()
        return True
    except Exception:
        return False


class GnomeIdle(_Poller):
    name = "gnome"

    def idle_seconds(self):
        try:
            return _mutter_idletime()
        except Exception:
            return None


class _XSSInfo(ctypes.Structure):
    _fields_ = [("window", ctypes.c_ulong), ("state", ctypes.c_int), ("kind", ctypes.c_int),
                ("til_or_since", ctypes.c_ulong), ("idle", ctypes.c_ulong), ("eventMask", ctypes.c_ulong)]


def _xss():
    x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
    xss = ctypes.CDLL(ctypes.util.find_library("Xss") or "libXss.so.1")
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x11.XDefaultRootWindow.restype = ctypes.c_ulong
    xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(_XSSInfo)
    xss.XScreenSaverQueryInfo.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_XSSInfo)]
    dpy = x11.XOpenDisplay(None)
    if not dpy:
        raise OSError("cannot open X display")
    return x11, xss, dpy


def probe_xss():
    try:
        _xss()
        return True
    except OSError:
        return False


class X11Idle(_Poller):
    name = "x11"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.x11, self.xss, self.dpy = _xss()
        self.root = self.x11.XDefaultRootWindow(self.dpy)
        self.info = self.xss.XScreenSaverAllocInfo()

    def idle_seconds(self):
        if not self.xss.XScreenSaverQueryInfo(self.dpy, self.root, self.info):
            return None
        return self.info.contents.idle / 1000.0


def _screensaver_idletime():
    """org.freedesktop.ScreenSaver.GetSessionIdleTime (KDE, some others; GNOME doesn't implement it)."""
    from gi.repository import Gio, GLib
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    r = bus.call_sync("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver", "org.freedesktop.ScreenSaver",
                      "GetSessionIdleTime", None, GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 1000, None)
    return r.unpack()[0] / 1000.0


def _logind_idle(now=None):
    """Seconds idle from the session's IdleHint/IdleSinceHint (set by the desktop after its own timeout)."""
    from gi.repository import Gio, GLib
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
    def get(prop):
        return bus.call_sync("org.freedesktop.login1", "/org/freedesktop/login1/session/auto",
                             "org.freedesktop.DBus.Properties", "Get",
                             GLib.Variant("(ss)", ("org.freedesktop.login1.Session", prop)),
                             None, Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
    return logind_seconds(get("IdleHint"), get("IdleSinceHint"), now)


def logind_seconds(hint, since_us, now=None):
    if not hint:
        return 0.0
    if not since_us:
        return float("inf")
    return max(0.0, (now if now is not None else time.time()) - since_us / 1e6)


class ScreenSaverIdle(_Poller):
    name = "screensaver"

    def idle_seconds(self):
        try:
            return _screensaver_idletime()
        except Exception:
            return None


class LogindIdle(_Poller):
    name = "logind"

    def idle_seconds(self):
        try:
            return _logind_idle()
        except Exception:
            return None


def _probe(f):
    try:
        f()
        return True
    except Exception:
        return False


def _probes(env):
    g = {}
    def wl(iface):
        if not g:
            g.update(wayland_globals(env) or {"": 0})
        return iface in g and "wl_seat" in g
    # logind: only useful when the desktop maintains IdleHint (a hint already set proves it)
    return {"wayland": lambda: wl("ext_idle_notifier_v1"), "kwin": lambda: wl("org_kde_kwin_idle"),
            "gnome": probe_mutter, "x11": probe_xss, "screensaver": lambda: _probe(_screensaver_idletime),
            "logind": lambda: _probe(_logind_idle)}


def detect(env=os.environ):
    """Name of the backend this session would use, or None (shown in the GUI)."""
    return select_backend(env, _probes(env))


CLASSES = {"wayland": WaylandIdle, "kwin": WaylandIdle, "gnome": GnomeIdle, "x11": X11Idle,
           "screensaver": ScreenSaverIdle, "logind": LogindIdle}


def create(timeout_s, on_idle, on_active, respect_inhibit=True, env=os.environ):
    kind = detect(env)
    if not kind:
        return None
    try:
        b = CLASSES[kind](timeout_s, on_idle, on_active, respect_inhibit)
    except Exception:
        return None
    b.name = kind
    return b


# ---------------------------------------------------------------- exclusions
def running_processes():
    names = set()
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                with open("/proc/%s/comm" % pid) as f:
                    names.add(f.read().strip())
            except OSError:
                pass
    return names


def excluded_process(listed):
    if not listed:
        return ""
    procs = running_processes()
    for p in listed:
        if p in procs or p[:15] in procs:   # comm is truncated to 15 chars
            return p
    return ""


def mpris_playing():
    """Name of an MPRIS player in Playing state, or ''."""
    try:
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        names = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                              "ListNames", None, None, Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
        for n in names:
            if n.startswith("org.mpris.MediaPlayer2."):
                try:
                    v = bus.call_sync(n, "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties", "Get",
                                      GLib.Variant("(ss)", ("org.mpris.MediaPlayer2.Player", "PlaybackStatus")),
                                      None, Gio.DBusCallFlags.NONE, 500, None).unpack()[0]
                    if v == "Playing":
                        return n[23:]
                except Exception:
                    pass
    except Exception:
        pass
    return ""


def session_inhibited():
    """Idle inhibitors visible to logind (used by the polling backends; Wayland honours them natively)."""
    try:
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
        v = bus.call_sync("org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.DBus.Properties",
                          "Get", GLib.Variant("(ss)", ("org.freedesktop.login1.Manager", "BlockInhibited")),
                          None, Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
        return "idle" in v.split(":")
    except Exception:
        return False
