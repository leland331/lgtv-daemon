"""Session power events from logind (PrepareForSleep / PrepareForShutdown) with delay inhibitors."""
import os

from gi.repository import Gio, GLib

LOGIN1 = ("org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager")


def is_reboot(metadata=None, jobs=None):
    """Tell reboot from shutdown without word lists.

    metadata: a{sv} from PrepareForShutdownWithMetadata (systemd >= 255), key 'type'.
    jobs: [(unit, jobtype)] from systemd ListJobs; a queued reboot/kexec target means reboot."""
    if metadata and metadata.get("type"):
        return metadata["type"] in ("reboot", "soft-reboot", "kexec")
    for unit, _ in jobs or []:
        if unit in ("reboot.target", "kexec.target", "soft-reboot.target", "systemd-reboot.service",
                    "systemd-kexec.service", "systemd-soft-reboot.service"):
            return True
    return False


def system_jobs(bus):
    try:
        r = bus.call_sync("org.freedesktop.systemd1", "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
                          "ListJobs", None, None, Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
        return [(j[1], j[2]) for j in r]
    except GLib.Error:
        return []


class Power:
    """Calls on_sleep(), on_resume(), on_shutdown(reboot: bool) from the GLib main loop."""

    def __init__(self, on_sleep, on_resume, on_shutdown, log=print):
        self.on_sleep, self.on_resume, self.on_shutdown, self.log = on_sleep, on_resume, on_shutdown, log
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
        self.fds = {}
        self.has_metadata = False
        for what in ("sleep", "shutdown"):
            self._inhibit(what)
        self.bus.signal_subscribe(LOGIN1[0], LOGIN1[2], None, LOGIN1[1], None, Gio.DBusSignalFlags.NONE, self._signal)

    def _inhibit(self, what):
        if what in self.fds:
            return
        try:
            r, fdl = self.bus.call_with_unix_fd_list_sync(
                LOGIN1[0], LOGIN1[1], LOGIN1[2], "Inhibit",
                GLib.Variant("(ssss)", (what, "LGTV Daemon", "Turn the TV off first", "delay")),
                GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, 2000, None, None)
            self.fds[what] = fdl.get(r.unpack()[0])
        except GLib.Error as e:
            self.log("inhibit %s failed: %s" % (what, e.message))

    def _release(self, what):
        fd = self.fds.pop(what, None)
        if fd is not None:
            os.close(fd)

    def _signal(self, conn, sender, path, iface, name, params):
        if name == "PrepareForSleep":
            if params.unpack()[0]:
                self.on_sleep()
                self._release("sleep")
            else:
                self._inhibit("sleep")
                self.on_resume()
        elif name in ("PrepareForShutdown", "PrepareForShutdownWithMetadata"):
            args = params.unpack()
            if not args[0]:
                self._inhibit("shutdown")   # shutdown was cancelled
                return
            if name == "PrepareForShutdownWithMetadata":
                self.has_metadata = True
                meta = args[1]
            elif self.has_metadata:
                return                      # the metadata variant is handled instead
            else:
                meta = None
            # both signals may arrive; wait briefly for the metadata one to avoid double handling
            if meta is None:
                GLib.timeout_add(300, self._shutdown_plain)
                return
            self._do_shutdown(is_reboot(meta, system_jobs(self.bus)))

    def _shutdown_plain(self):
        if not self.has_metadata:
            self._do_shutdown(is_reboot(None, system_jobs(self.bus)))
        return False

    def _do_shutdown(self, reboot):
        if "shutdown" not in self.fds:
            return
        self.on_shutdown(reboot)
        self._release("shutdown")
