"""Keep Screen On: an explicit, user-toggled idle inhibitor. Nothing is inhibited unless this is enabled."""
import os

from gi.repository import Gio, GLib


class KeepAwake:
    def __init__(self, log):
        self.log = log
        self.cookie = None     # org.freedesktop.ScreenSaver cookie
        self.fd = None         # logind idle inhibitor fd (fallback)

    @property
    def active(self):
        return self.cookie is not None or self.fd is not None

    def enable(self):
        if self.active:
            return True
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        try:
            r = bus.call_sync("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver",
                              "org.freedesktop.ScreenSaver", "Inhibit",
                              GLib.Variant("(ss)", ("LGTV Daemon", "Keep Screen On")),
                              GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 3000, None)
            self.cookie = r.unpack()[0]
            return True
        except GLib.Error as e:
            self.log("ScreenSaver inhibit unavailable (%s), trying logind" % e.message)
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            r, fds = bus.call_with_unix_fd_list_sync(
                "org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager", "Inhibit",
                GLib.Variant("(ssss)", ("idle", "LGTV Daemon", "Keep Screen On", "block")),
                GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, 3000, None, None)
            self.fd = fds.get(r.unpack()[0])
            return True
        except GLib.Error as e:
            self.log("Keep Screen On failed: %s" % e.message)
            return False

    def disable(self):
        if self.cookie is not None:
            try:
                Gio.bus_get_sync(Gio.BusType.SESSION, None).call_sync(
                    "org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver", "org.freedesktop.ScreenSaver",
                    "UnInhibit", GLib.Variant("(u)", (self.cookie,)), None, Gio.DBusCallFlags.NONE, 3000, None)
            except GLib.Error as e:
                self.log("ScreenSaver uninhibit failed: %s" % e.message)
            self.cookie = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def other_blockers(timeout_ms=500):
    """Who else blocks idle/screen off right now ('who: why'), or ''. Never raises. Our own inhibitor is excluded."""
    def call(bus, dest, path, iface, method, params, rtype):
        return Gio.bus_get_sync(bus, None).call_sync(dest, path, iface, method, params, GLib.VariantType(rtype),
                                                     Gio.DBusCallFlags.NONE, timeout_ms, None).unpack()
    names = []
    try:   # logind: every block inhibitor that includes idle
        for what, who, why, mode, _uid, pid in call(
                Gio.BusType.SYSTEM, "org.freedesktop.login1", "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager", "ListInhibitors", None, "(a(ssssuu))")[0]:
            if "idle" in what.split(":") and mode == "block" and pid != os.getpid():
                names.append("%s: %s" % (who, why) if why else who)
    except GLib.Error:
        pass
    if names:
        return ", ".join(names)
    try:   # KDE: also sees ScreenSaver/PowerManagement inhibits that logind does not.
        # Only count it when screen settings (dim/off/lock, policy 4) are held; the list also has sleep-only holds.
        pa = ("org.kde.Solid.PowerManagement.PolicyAgent", "/org/kde/Solid/PowerManagement/PolicyAgent",
              "org.kde.Solid.PowerManagement.PolicyAgent")
        if call(Gio.BusType.SESSION, *pa, "HasInhibition", GLib.Variant("(u)", (4,)), "(b)")[0]:
            r = call(Gio.BusType.SESSION, *pa, "ListInhibitions", None, "(aas)")[0]
            names = ["%s: %s" % (a[0], a[1]) for a in r if len(a) > 1 and "LGTV" not in a[0]] or ["an app"]
    except GLib.Error:
        pass
    if not names:
        try:   # GNOME: flag 8 = idle
            if call(Gio.BusType.SESSION, "org.gnome.SessionManager", "/org/gnome/SessionManager",
                    "org.gnome.SessionManager", "IsInhibited", GLib.Variant("(u)", (8,)), "(b)")[0]:
                names = ["an app"]
        except GLib.Error:
            pass
    return ", ".join(names)
