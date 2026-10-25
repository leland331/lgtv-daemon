"""Should the daemon power the TV on when it starts?

Yes at a fresh boot or login, and when someone used the keyboard or mouse in the last couple of
minutes. No for a plain restart of the service (an update, a crash, `systemctl --user restart`):
the TV may be off on purpose, and a restart must not turn it on."""
import os
import tempfile
import time

FRESH_SESSION_S = 180      # a login session younger than this counts as "just logged in"
RECENT_INPUT_S = 120       # keyboard/mouse input this recent counts as "someone is here"
MARKER = "lgtv-daemon.started"
GRAPHICAL = ("x11", "wayland", "mir")


def boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return ""


def marker_path(env=os.environ):
    return os.path.join(env.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), MARKER)


def first_start(path, bid):
    """True if the daemon hasn't started before in this boot and login; records this start.
    XDG_RUNTIME_DIR is emptied at logout and at boot, so a marker there means "restarted"."""
    try:
        with open(path) as f:
            seen = f.read().strip() == bid
    except OSError:
        seen = False
    try:
        with open(path, "w") as f:
            f.write(bid + "\n")
    except OSError:
        pass
    return not seen


def session_age(now=None):
    """Seconds since this user's newest desktop (x11/wayland) logind session started, or None if
    unknown. SSH and other text sessions don't count: an SSH login isn't someone at the TV."""
    try:
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)

        def get(path, iface, prop):
            return bus.call_sync("org.freedesktop.login1", path, "org.freedesktop.DBus.Properties", "Get",
                                 GLib.Variant("(ss)", (iface, prop)), None, Gio.DBusCallFlags.NONE,
                                 1000, None).unpack()[0]
        sessions = get("/org/freedesktop/login1/user/self", "org.freedesktop.login1.User", "Sessions")
        s = "org.freedesktop.login1.Session"
        started = [get(p, s, "Timestamp") for _, p in sessions if get(p, s, "Type") in GRAPHICAL]
        started = [t for t in started if t]
        if not started:
            return None
        return max(0.0, (now if now is not None else time.time()) - max(started) / 1e6)
    except Exception:
        return None


def input_age():
    """Seconds since the last keyboard/mouse input, from the desktop if it can tell, else None."""
    from . import idle
    for f in (idle._mutter_idletime, idle._screensaver_idletime, _x11_idletime):
        try:
            v = f()
            if v is not None:
                return float(v)
        except Exception:
            pass
    return None


def _x11_idletime(env=os.environ):
    if env.get("WAYLAND_DISPLAY"):
        return None                       # XWayland only sees input to X11 windows
    from . import idle
    return idle.X11Idle(0, None, None).idle_seconds()


def decide(first, session_s, input_s):
    """(power on?, reason). Pure, for tests."""
    if first:
        return True, "first start since boot or login"
    if session_s is not None and session_s < FRESH_SESSION_S:
        return True, "login session started %d s ago" % session_s
    if input_s is not None and input_s < RECENT_INPUT_S:
        return True, "keyboard/mouse input %d s ago" % input_s
    return False, "service restarted with no recent input: leaving the TV as it is"


def check(env=os.environ):
    return decide(first_start(marker_path(env), boot_id()), session_age(), input_age())
