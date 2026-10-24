"""GTK4 + libadwaita settings UI. Plain by design: device list + Settings page."""
import gi
import threading
import time
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402
from . import config, idle, netif, ssdp, topology, webos  # noqa: E402


gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")



def button_row(title, css=()):
    """Adw.ButtonRow is libadwaita 1.6+; older (Debian/Ubuntu LTS) gets an activatable ActionRow."""
    if hasattr(Adw, "ButtonRow"):
        return Adw.ButtonRow(title=title, css_classes=list(css))
    r = Adw.ActionRow(title=title, activatable=True, css_classes=list(css))
    return r

APP_ID = "io.github.lgtv_daemon"


def bg(fn, done):
    """Run fn in a thread; done(result) on the main loop."""
    def run():
        try:
            r = fn()
        except Exception as e:  # shown to the user
            r = e
        GLib.idle_add(done, r)
    threading.Thread(target=run, daemon=True).start()


def reload_daemon():
    try:
        from .cli import daemon_send
        daemon_send("reload", timeout=3)
    except OSError:
        pass


def tray_shown():
    try:
        from .cli import daemon_send
        return bool(daemon_send("status", timeout=1).get("tray"))
    except (OSError, ValueError, AttributeError):
        return False


def switch_row(title, subtitle, active, on_change):
    row = Adw.SwitchRow(title=title, subtitle=subtitle, active=active)
    row.connect("notify::active", lambda r, _: on_change(r.get_active()))
    return row


def spin_row(title, subtitle, value, lo, hi, on_change):
    row = Adw.SpinRow.new_with_range(lo, hi, 1)
    row.set_title(title)
    row.set_subtitle(subtitle)
    row.set_value(value)
    row.connect("notify::value", lambda r, _: on_change(int(r.get_value())))
    return row


def combo_row(title, subtitle, labels, index, on_change):
    row = Adw.ComboRow(title=title, subtitle=subtitle, model=Gtk.StringList.new(labels))
    row.set_selected(max(0, index))
    row.connect("notify::selected", lambda r, _: on_change(r.get_selected()))
    return row


def entry_row(title, text, on_change):
    row = Adw.EntryRow(title=title, text=text)
    row.connect("changed", lambda r: on_change(r.get_text()))
    return row


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="LGTV Daemon", default_width=640, default_height=560)
        self.cfg = config.load()
        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        self.set_content(self.toasts)
        self.nav.add(self.main_page())
        self.connect("realize", self.watch_minimize)

    # -------------------------------------------------------- minimize to tray
    def watch_minimize(self, *_):
        """Minimize hides the window to the tray (the daemon's tray icon presents it again). X button quits.
        X11 reports MINIMIZED; Wayland compositors report SUSPENDED (GTK 4.12+). Elsewhere minimize is normal."""
        surf = self.get_surface()
        if not isinstance(surf, Gdk.Toplevel):
            return
        flag = Gdk.ToplevelState.MINIMIZED
        if "Wayland" in type(surf).__name__ and hasattr(Gdk.ToplevelState, "SUSPENDED"):
            flag = Gdk.ToplevelState.SUSPENDED
        surf.connect("notify::state", lambda s, _: self.minimized(s.get_state() & flag))

    def minimized(self, hit):
        if not hit or not tray_shown():
            return
        GLib.idle_add(self.hide_to_tray)

    def hide_to_tray(self):
        self.set_visible(False)
        return False

    # -------------------------------------------------------- helpers
    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    def save(self):
        try:
            config.save(self.cfg)
            self.cfg = config.load()
        except ValueError as e:
            self.toast(str(e))
            return False
        reload_daemon()
        return True

    # -------------------------------------------------------- main page
    def main_page(self):
        tb = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        settings = Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text="Settings")
        settings.connect("clicked", lambda *_: self.nav.push(self.settings_page()))
        hb.pack_end(settings)
        tb.add_top_bar(hb)
        self.page = Adw.PreferencesPage()
        tb.set_content(self.page)
        self.group = None
        self.fill_devices()
        return Adw.NavigationPage(child=tb, title="LGTV Daemon", tag="main")

    def fill_devices(self):
        if self.group:
            self.page.remove(self.group)
        self.group = g = Adw.PreferencesGroup(
            title="Devices", description="LG WebOS TVs controlled by this PC. Turn on 'Manage' so the TV "
            "follows the PC: power, sleep, idle and monitor changes.")
        box = Gtk.Box(spacing=6)
        for label, cb in (("Scan", self.on_scan), ("Add", lambda *_: self.edit_device(None))):
            b = Gtk.Button(label=label)
            b.connect("clicked", cb)
            box.append(b)
        g.set_header_suffix(box)
        if not self.cfg["devices"]:
            g.add(Adw.ActionRow(title="No devices", subtitle="Use Scan to find TVs on the network, or Add one manually."))
        for i, d in enumerate(self.cfg["devices"]):
            row = Adw.ActionRow(title=d["name"], subtitle="%s  ·  %s" % (d["ip"] or "no IP",
                                "paired" if d["key"] else "not paired"))
            sw = Gtk.Switch(active=d["enabled"], valign=Gtk.Align.CENTER, tooltip_text="Manage automatically")
            sw.connect("notify::active", lambda s, _, d=d: (d.update(enabled=s.get_active()), self.save()))
            row.add_suffix(Gtk.Label(label="Manage", css_classes=["dim-label"]))
            row.add_suffix(sw)
            for label, cb in (("Test", self.on_test), ("Configure", lambda b, i=i: self.edit_device(i))):
                b = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
                b.connect("clicked", (lambda b, d=d, cb=cb: cb(d)) if label == "Test" else cb)
                row.add_suffix(b)
            g.add(row)
        self.page.add(g)

    def on_scan(self, btn):
        btn.set_sensitive(False)
        self.toast("Scanning the network")

        def done(found):
            btn.set_sensitive(True)
            if isinstance(found, Exception):
                self.toast("Scan failed: %s" % found)
                return
            known = {d["ip"] for d in self.cfg["devices"]}
            new = [f for f in found if f["ip"] not in known]
            for f in new:
                self.cfg["devices"].append({"name": f["name"], "ip": f["ip"], "model": f["model"],
                                            "macs": [f["mac"]] if f.get("mac") else []})
            self.save()
            self.fill_devices()
            self.toast("%d new device(s) found" % len(new) if new else "No new devices found")
        bg(ssdp.discover, done)

    def on_test(self, d):

        def run():
            # Remember the input in use so the test returns the TV to it (not the home screen).
            tv = webos.connect(d, timeout=3, pair_timeout=5)
            try:
                app = tv.request("com.webos.applicationManager/getForegroundAppInfo").get("appId", "")
            finally:
                tv.close()
            webos.power_off(d, self.cfg["blank_instead_of_poweroff"], log=lambda m: None, forced=True)
            time.sleep(5)
            ok = webos.power_on(d, self.cfg["power_on_timeout"], log=lambda m: None)
            if ok is True and app.startswith("com.webos.app.hdmi"):
                time.sleep(2)
                tv = webos.connect(d, timeout=3, pair_timeout=5)
                try:
                    tv.request("system.launcher/launch", {"id": app})
                finally:
                    tv.close()
            return ok
        bg(run, lambda ok: self.toast("Test passed" if ok is True else "Test failed: %s" % ("TV did not turn back on" if ok is False else ok)))

    # -------------------------------------------------------- device page
    def edit_device(self, index):
        new = index is None
        d = dict(config.DEVICE_DEFAULTS) if new else self.cfg["devices"][index]
        d = {k: (list(v) if isinstance(v, list) else v) for k, v in d.items()}
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(title="Network")
        g.add(entry_row("Name", d["name"], lambda t: d.update(name=t)))
        g.add(entry_row("IP address", d["ip"], lambda t: d.update(ip=t.strip())))
        g.add(entry_row("MAC address(es), comma separated", ", ".join(d["macs"]), lambda t: d.update(macs=t)))
        methods = ["Broadcast", "Subnet broadcast", "Send to IP address"]
        g.add(combo_row("Wake-on-LAN method", "How the power-on packet is sent. Try 'Send to IP address' if "
                        "broadcast doesn't wake the TV.", methods, config.WOL_METHODS.index(d["wol"]),
                        lambda i: d.update(wol=config.WOL_METHODS[i])))
        g.add(entry_row("Subnet mask (for subnet broadcast)", d["subnet"], lambda t: d.update(subnet=t.strip())))
        ifs = [""] + sorted({i["name"] for i in netif.interfaces()} | ({d["interface"]} - {""}))
        g.add(combo_row("Network interface", "Auto sends wake-up packets out of every interface.",
                        ["Auto"] + ifs[1:], ifs.index(d["interface"]), lambda i: d.update(interface=ifs[i])))
        g.add(switch_row("Secure connection", "Use wss on port 3001 (newer firmware). Turn off for old TVs "
                         "that only accept port 3000.", d["ssl"], lambda v: d.update(ssl=v)))
        page.add(g)

        g = Adw.PreferencesGroup(title="HDMI input")
        g.add(spin_row("HDMI-input for PC", "The TV input the PC is connected to.", d["source_hdmi_input"], 1, 4,
                       lambda v: d.update(source_hdmi_input=v)))
        g.add(switch_row("Only Turn Off on the PC's Input",
                         "Leaves the TV alone while it shows another input or an app, like Netflix. "
                         "Commands you send yourself always apply.",
                         d["check_hdmi_input_when_power_off"], lambda v: d.update(check_hdmi_input_when_power_off=v)))
        g.add(switch_row("Switch to PC HDMI-input when powering on", "After the TV powers on, change to the PC's "
                         "HDMI input.", d["set_hdmi_input_on_power_on"],
                         lambda v: d.update(set_hdmi_input_on_power_on=v)))
        g.add(spin_row("Input switch delay", "Seconds to wait after the TV turns on before switching input.",
                       d["set_hdmi_input_on_power_on_delay"], 0, 30,
                       lambda v: d.update(set_hdmi_input_on_power_on_delay=v)))
        page.add(g)

        g = Adw.PreferencesGroup(title="Monitor output")
        conns = topology.connectors()
        names = [""] + [c["connector"] for c in conns]
        labels = ["Auto (match by EDID)"] + ["%s%s" % (c["connector"], " - " + c["name"] if c["name"] else "")
                                             for c in conns]
        if d["connector"] not in names:
            names.append(d["connector"])
            labels.append(d["connector"])
        g.add(combo_row("Connector", "Which PC output this TV is plugged into, used by monitor topology support.",
                        labels, names.index(d["connector"]), lambda i: d.update(connector=names[i])))
        page.add(g)

        g = Adw.PreferencesGroup()
        if not new:
            pr = button_row("Pair")
            pr.connect("activated", lambda *_: self.pair(d))
            g.add(pr)
            rm = button_row("Remove device", ["destructive-action"])
            rm.connect("activated", lambda *_: (self.cfg["devices"].pop(index), self.save(), self.fill_devices(),
                                                self.nav.pop()))
            g.add(rm)
        page.add(g)

        tb = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        save = Gtk.Button(label="Save", css_classes=["suggested-action"])

        def on_save(*_):
            if new:
                self.cfg["devices"].append(d)
            else:
                self.cfg["devices"][index] = d
            if self.save():
                self.fill_devices()
                self.nav.pop()
            elif new:
                self.cfg["devices"].pop()
        save.connect("clicked", on_save)
        hb.pack_end(save)
        tb.add_top_bar(hb)
        tb.set_content(page)
        self.nav.push(Adw.NavigationPage(child=tb, title="Add Device" if new else d["name"]))

    def pair(self, d):
        self.toast("Accept the pairing prompt on the TV")
        dev = dict(d, key="")

        def done(r):
            if isinstance(r, Exception):
                self.toast("Pairing failed: %s" % r)
                return
            d["key"] = r
            for i, x in enumerate(self.cfg["devices"]):
                if x["name"] == d["name"]:
                    self.cfg["devices"][i]["key"] = r
            self.save()
            self.fill_devices()
            self.toast("Paired")

        def run():
            tv = webos.connect(dev, pair_timeout=60)
            d["model"] = tv.request("system/getSystemInfo").get("modelName", "") or d["model"]
            tv.close()
            return dev["key"]
        bg(run, done)

    # -------------------------------------------------------- settings page
    def settings_page(self):
        c = self.cfg
        set_ = lambda sec, k: (lambda v: (c[sec].__setitem__(k, v), self.save()))
        top = lambda k: (lambda v: (c.__setitem__(k, v), self.save()))
        page = Adw.PreferencesPage()

        g = Adw.PreferencesGroup(title="Power")
        g.add(switch_row("Blank screen instead of power off", "Turns the picture off but keeps the TV running, "
                         "for faster wake-up. Shutdown still powers off.", c["blank_instead_of_poweroff"],
                         top("blank_instead_of_poweroff")))
        g.add(spin_row("Power on timeout (seconds)", "How long to keep trying to wake the TV.",
                       c["power_on_timeout"], 5, 120, top("power_on_timeout")))
        g.add(switch_row("Keep TV off after a network wake", "When the PC is woken over the network "
                         "(Wake-on-LAN), leave the TV off until you use the keyboard or mouse.",
                         c["stay_off_on_network_wake"], top("stay_off_on_network_wake")))
        g.add(switch_row("Persistent connection", "Keep a connection open to each TV so it can be turned "
                         "off instantly when the PC sleeps or shuts down.",
                         c["persistent_connection"], top("persistent_connection")))
        page.add(g)

        g = Adw.PreferencesGroup(title="User idle mode", description="Blank the TV screen when you've been away "
                                 "from the PC, and turn it back on when you return.")
        backend = idle.detect()
        g.add(switch_row("Enable", "Idle detection: %s." % backend if backend else
                         "Unavailable: this session offers no idle detection.", c["idle"]["enabled"],
                         set_("idle", "enabled")))
        g.add(spin_row("Minutes before blanking", "Time without keyboard or mouse input.", c["idle"]["minutes"],
                       1, 240, set_("idle", "minutes")))
        g.add(switch_row("Mute speakers while idle", "Also mute the TV while the screen is blank.",
                         c["idle"]["mute"], set_("idle", "mute")))
        g.add(switch_row("Not while an app keeps the screen awake", "Video players and presentations that block "
                         "the screensaver prevent idle mode.", c["idle"]["respect_inhibit"],
                         set_("idle", "respect_inhibit")))
        g.add(switch_row("Not while media is playing", "Any media player reporting 'Playing' (MPRIS) prevents "
                         "idle mode.", c["idle"]["respect_mpris"], set_("idle", "respect_mpris")))
        g.add(entry_row("Not while these processes run (comma separated)", ", ".join(c["idle"]["processes"]),
                        lambda t: (c["idle"].__setitem__("processes", [p.strip() for p in t.split(",") if p.strip()]),
                                   self.save())))
        page.add(g)

        g = Adw.PreferencesGroup(title="Other")
        g.add(switch_row("Monitor topology support", "Power off a TV when its PC output is disabled or unplugged, "
                         "and back on when it returns." if topology.connectors() else
                         "Unavailable: no display outputs could be detected.", c["topology"]["enabled"], set_("topology", "enabled")))
        g.add(switch_row("Always power on at boot", "Ignore topology at boot and login.",
                         c["topology"]["keep_on_boot"], set_("topology", "keep_on_boot")))
        g.add(switch_row("Remote streaming host support", "Turn TVs off while a Sunshine or similar client is "
                         "connected (uses -streaming_connect / -streaming_disconnect, see README).",
                         c["streaming"]["enabled"], set_("streaming", "enabled")))
        g.add(switch_row("Power off while streaming", "Off: blank the screen instead.",
                         c["streaming"]["prefer_power_off"], set_("streaming", "prefer_power_off")))
        g.add(switch_row("External API", "Send power events to scripts on the local socket.",
                         c["api"]["enabled"], set_("api", "enabled")))
        g.add(switch_row("Show Tray Icon", "A tray icon with Keep Screen On and quick TV controls. "
                         "Needs a system tray (GNOME needs the AppIndicator extension).", c["tray"], top("tray")))
        g.add(switch_row("Logging", "Write a log to " + config.LOG_PATH, c["log"], top("log")))
        page.add(g)

        tb = Adw.ToolbarView()
        tb.add_top_bar(Adw.HeaderBar())
        tb.set_content(page)
        return Adw.NavigationPage(child=tb, title="Settings")


def main():
    if (Adw.get_major_version(), Adw.get_minor_version()) < (1, 4):
        print("The settings app needs libadwaita 1.4+ (found %d.%d). The daemon and CLI still work."
              % (Adw.get_major_version(), Adw.get_minor_version()))
        return 1
    app = Adw.Application(application_id=APP_ID)
    app.connect("activate", lambda a: (a.props.active_window or Window(a)).present())
    return app.run([])
