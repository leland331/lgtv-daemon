"""User-session daemon: power events, user idle mode, topology, streaming host and the scripting API."""
import json
import os
import shlex
import signal
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait as wait_futures

from gi.repository import GLib

from . import commands, config, idle, persist, startup, topology, wake, webos
from .keepawake import KeepAwake
from .tray import Tray

SOCKET_PATH = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "lgtv-daemon.sock")


class Daemon:
    def __init__(self):
        self.cfg = config.load()
        self.pool = ThreadPoolExecutor(8)
        self.locks = {}
        self.session_off = set()      # -autodisable (until restart)
        self.input_timer = None       # manual blank/off: delay before watching for input
        self.input_watch = None       # manual blank/off: short idle watcher, wakes the TV on input
        self.session_on = set()       # -autoenable  (until restart)
        self.user_idle = False
        self.network_woken = False    # woken by Wake-on-LAN: TV stays off until keyboard/mouse input
        self.sleep_snapshot = None    # network-card wakeup counters taken before sleep
        self.streaming = False
        self.persist = {}             # device name -> persist.Persistent
        self.sleep_lock = threading.Lock()
        self.sleep_off_at = 0.0       # when the TVs were last powered off for sleep/shutdown
        self.user_off = set()         # TVs turned off at the TV/remote: stay off until fresh input
        self.own_off_at = {}          # device name -> when the daemon itself last turned it off/blank
        self.on_busy = set()          # devices with a power on queued or running (debounce)
        self.resumed_at = 0.0
        self.topo = {}
        self.dpms = {}
        self.idle_backend = None
        self.clients = []
        self.clients_lock = threading.Lock()
        self.keep = KeepAwake(self.log)
        self.tray = Tray(self)
        self.tray_quit = False        # 'Quit Tray' used: stay hidden until the next start

    # ------------------------------------------------------------ helpers
    def log(self, msg):
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
        print(line, flush=True)
        if self.cfg["log"]:
            try:
                os.makedirs(config.STATE_DIR, exist_ok=True)
                with open(config.LOG_PATH, "a") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def managed(self):
        out = []
        for d in self.cfg["devices"]:
            if d["name"] in self.session_off:
                continue
            if d["enabled"] or d["name"] in self.session_on:
                out.append(d)
        return out

    def event(self, name):
        """Broadcast a power/system event to API clients (Scripting.md semantics)."""
        self.log("event " + name)
        if not self.cfg["api"]["enabled"]:
            return
        with self.clients_lock:
            for c in list(self.clients):
                try:
                    c.sendall(("EVENT %s\n" % name).encode())
                except OSError:
                    self.clients.remove(c)

    def _locked(self, dev, fn, *a):
        lock = self.locks.setdefault(dev["name"], threading.Lock())
        with lock:
            try:
                return fn(*a)
            except Exception as e:   # never let one TV break the daemon
                self.log("%s: %s" % (dev["name"], e))

    def each(self, action, devices=None, wait=False):
        devices = self.managed() if devices is None else devices
        futs = []
        for d in devices:
            if action in ("on", "unblank"):
                if d["name"] in self.user_off:
                    self.log("%s: stays off (turned off at the TV) until keyboard or mouse input" % d["name"])
                    continue
                if action == "on" and d["name"] in self.on_busy:
                    continue                  # a power on is already queued or running
            if action in ("off", "off_forced", "blank"):
                self.own_off_at[d["name"]] = time.time()
            if action == "on":
                self.on_busy.add(d["name"])
                f = (self._power_on, d)
            elif action == "off":
                f = (webos.power_off, d, self.cfg["blank_instead_of_poweroff"], self.log)
            elif action == "off_forced":   # user's own command: skip the HDMI input check
                f = (lambda d: webos.power_off(d, self.cfg["blank_instead_of_poweroff"], self.log, forced=True), d)
            elif action == "blank":
                f = (webos.power_off, d, True, self.log)
            elif action == "unblank":
                f = (webos.screen_on, d, self.log)
            elif action == "mute":
                f = (commands.run_steps, d, commands.plan("mute", []), self.cfg, self.log)
            elif action == "unmute":
                f = (commands.run_steps, d, commands.plan("unmute", []), self.cfg, self.log)
            futs.append(self.pool.submit(self._locked, d, *f))
        if wait:
            for f in futs:
                f.result()

    def _power_on(self, d):
        try:
            ok = webos.power_on(d, self.cfg["power_on_timeout"], self.log,
                                cancel=lambda: d["name"] in self.user_off)
        finally:
            self.on_busy.discard(d["name"])
        p = self.persist.get(d["name"])
        if ok and p and not p.alive():
            p.kick()
        return ok

    # ------------------------------------------------------------ persistent connections
    def sync_persistent(self):
        """Keep one persistent connection per managed, paired TV (config: persistent_connection)."""
        want = {d["name"]: d for d in self.managed() if d["key"]} if self.cfg["persistent_connection"] else {}
        for name in list(self.persist):
            p = self.persist[name]
            if name not in want or not p.same_device(want[name]):
                p.stop()
                del self.persist[name]
        for name, d in want.items():
            if name not in self.persist:
                self.persist[name] = persist.Persistent(d, self.log, on_change=self.tv_state_changed).start()

    # ------------------------------------------------------------ turned off at the TV
    def is_external_off(self, name, old, new, now=None):
        """True if a state change means someone turned the TV off at the TV or remote, not us."""
        now = time.time() if now is None else now
        if old not in ("Active", "Screen Off") or new not in ("Active Standby", "Suspend", "Power Off", ""):
            return False
        if new == "" and old != "Active":
            return False                  # lost connection while blanked: not a clear signal
        recent = max(self.own_off_at.get(name, 0), self.sleep_off_at, self.resumed_at)
        return now - recent > 15

    def tv_state_changed(self, name, old, new):
        """From a persistent connection's thread."""
        if self.is_external_off(name, old, new):
            GLib.idle_add(self.mark_user_off, name)
        elif new == "Active" and name in self.user_off:
            GLib.idle_add(self.clear_user_off, name)

    def mark_user_off(self, name):
        if name not in self.user_off:
            self.user_off.add(name)
            self.log("%s: turned off at the TV; stays off until keyboard or mouse input" % name)
            self.watch_input(self.on_input_after_user_off)
        return False

    def clear_user_off(self, name):
        if name in self.user_off:
            self.user_off.discard(name)
            self.log("%s: turned on at the TV" % name)
        return False

    def on_input_after_user_off(self):
        self.stop_input_watch()
        self.end_network_wake()
        if self.user_off:
            self.log("input after the TV was turned off: powering the TV on")
            self.user_off.clear()
            self.each("on", [d for d in self.managed() if self.topo_ok(d)])

    def manual_on(self):
        """Tray Turn TV On: an explicit request beats 'turned off at the TV'."""
        self.user_off.clear()
        self.end_network_wake()
        self.each("on")

    def power_off_before(self, deadline, why):
        """Power the managed TVs off before the inhibitor window closes (sleep, shutdown).
        Uses the persistent connection when it is up (sent within milliseconds, before the network
        goes down), else quick retries. Runs once per sleep even if both a pre-sleep hook (prepare_sleep) and
        logind ask. Returns when done or at the deadline."""
        with self.sleep_lock:
            if time.time() - self.sleep_off_at < 30:
                return "already done"
            t0 = time.time()
            blank = self.cfg["blank_instead_of_poweroff"]
            futs = []
            for d in self.managed():
                p = self.persist.get(d["name"])
                if p is not None and p.power_off_now(blank, log=self.log) is not None:
                    continue
                futs.append(self.pool.submit(self._locked_until, d, deadline, blank))
            if futs:
                wait_futures(futs, timeout=max(0, deadline - time.time()))
            self.sleep_off_at = time.time()
            self.log("power off before %s took %.2f s" % (why, time.time() - t0))
            return "ok"

    def _locked_until(self, d, deadline, blank):
        # don't wait behind a long command (e.g. a 40 s power on) holding the device lock
        lock = self.locks.setdefault(d["name"], threading.Lock())
        got = lock.acquire(timeout=max(0, deadline - time.time() - 1.5))
        try:
            return webos.power_off_until(d, deadline, blank, self.log)
        except Exception as e:
            self.log("%s: %s" % (d["name"], e))
        finally:
            if got:
                lock.release()

    def topo_ok(self, dev):
        """False if topology support says this TV's output is disconnected/disabled."""
        if not self.cfg["topology"]["enabled"]:
            return True
        c = topology.match(dev, topology.connectors())
        return c is None or topology.active(c)

    # ------------------------------------------------------------ tray / keep screen on
    def open_gui(self):
        try:
            GLib.spawn_async([sys.executable, "-m", "lgtvc", "--gui"], flags=GLib.SpawnFlags.SEARCH_PATH)
        except GLib.Error as e:
            self.log("could not open the settings app: %s" % e.message)

    def toggle_keep_awake(self):
        if self.keep.active:
            self.keep.disable()
        elif self.keep.enable():
            if self.user_idle:
                self.on_active()
        self.log("Keep Screen On: %s" % ("on" if self.keep.active else "off"))
        self.tray.refresh(recheck=True)

    def quit_tray(self):
        self.tray_quit = True
        self.tray.stop()

    def apply_tray(self):
        if self.cfg["tray"] and not self.tray_quit:
            self.tray.start()
        else:
            self.tray.stop()
            if not self.cfg["tray"]:
                self.tray_quit = False

    # ------------------------------------------------------------ power
    def on_sleep(self, window=5.0):
        start = time.time()
        self.power_off_before(start + window - 0.5, "sleep")
        self.event("SYSTEM_SUSPEND")
        self.end_network_wake()
        self.stop_input_watch()
        try:
            self.sleep_snapshot = wake.snapshot()
        except Exception as e:
            self.sleep_snapshot = None
            self.log("wake source: could not read network card counters: %s" % e)

    def on_resume(self):
        self.event("SYSTEM_RESUME")
        self.user_idle = False
        self.sleep_off_at = 0.0
        self.resumed_at = time.time()
        self.user_off.clear()             # a new wake starts fresh
        for p in self.persist.values():
            p.kick()                      # the old sockets died with the network during sleep
        if self.cfg["stay_off_on_network_wake"]:
            try:
                source, why = wake.detect(self.sleep_snapshot)
            except Exception as e:
                source, why = "other", "could not tell: %s" % e
            self.log("wake source: %s (%s)" % (source, why))
            if source == "network":
                self.network_woken = True
                self.log("network wake: TV stays off until keyboard or mouse input")
                self.watch_input(self.on_input_after_network_wake, self.on_no_input_watch)
                return
        self.each("on", [d for d in self.managed() if self.topo_ok(d)])

    def on_input_after_network_wake(self):
        self.stop_input_watch()
        if self.end_network_wake():
            self.log("input after network wake: powering the TV on")
            self.each("on", [d for d in self.managed() if self.topo_ok(d)])

    def on_no_input_watch(self):
        if self.end_network_wake():
            self.log("network wake: no input detection in this session, powering the TV on")
            self.each("on", [d for d in self.managed() if self.topo_ok(d)])

    def end_network_wake(self):
        """Clear the network-woken mark; True if it was set."""
        was, self.network_woken = self.network_woken, False
        return was

    def on_shutdown(self, reboot, window=5.0):
        start = time.time()
        if not reboot:
            self.power_off_before(start + window - 0.5, "shutdown")
        self.event("SYSTEM_REBOOT" if reboot else "SYSTEM_SHUTDOWN")

    # ------------------------------------------------------------ idle
    def idle_excluded(self):
        ic = self.cfg["idle"]
        if self.keep.active:
            return "Keep Screen On"
        if ic["respect_mpris"]:
            p = idle.mpris_playing()
            if p:
                return "media playing (%s)" % p
        p = idle.excluded_process(ic["processes"])
        if p:
            return "process running (%s)" % p
        if ic["respect_inhibit"] and self.idle_backend is not None and self.idle_backend.name != "wayland" \
                and idle.session_inhibited():
            return "session idle inhibitor"
        return ""

    def on_idle(self, forced=False):
        if self.user_idle or self.streaming:
            return
        why = "" if forced else self.idle_excluded()
        if why:
            self.log("user idle mode suppressed: " + why)
            return
        self.user_idle = True
        self.event("SYSTEM_USER_IDLE")
        self.each("blank")
        if forced:
            self.watch_input()
        if self.cfg["idle"]["mute"]:
            self.each("mute")

    def watch_input(self, on_input=None, on_unavailable=None):
        """After a manual blank (tray, -idle), unblank on the next input like an idle blank does.
        A short idle timer arms once input settles; its resume is the next mouse/key event."""
        self.stop_input_watch()
        cb = lambda f: (lambda: GLib.idle_add(f))

        def arm():
            self.input_timer = None
            # Arms 1.5 s after the click; then any input counts (a 0.3 s pause is enough to start
            # watching, so moving the mouse right after the click still wakes the TV promptly).
            self.input_watch = idle.create(0.3, lambda: None, cb(on_input or self.on_active), False)
            if self.input_watch:
                self.input_watch.start()
            elif on_unavailable:
                on_unavailable()
            return False
        self.input_timer = GLib.timeout_add(1500, arm)

    def manual_off(self):
        """Tray Turn TV Off: power off now, power back on at the next mouse/key input."""
        self.each("off_forced")
        self.watch_input(self.on_input_after_off)

    def on_input_after_off(self):
        self.stop_input_watch()
        self.end_network_wake()
        self.user_off.clear()
        self.each("on")

    def stop_input_watch(self):
        t, self.input_timer = self.input_timer, None
        if t:
            GLib.source_remove(t)
        w, self.input_watch = self.input_watch, None
        if w:
            w.stop()

    def on_active(self):
        self.stop_input_watch()
        if self.user_off:                 # first input after the TV was turned off at the TV
            if self.user_idle:
                self.user_idle = False
                self.event("SYSTEM_USER_BUSY")
            self.on_input_after_user_off()
            return
        if self.network_woken:            # first input after a Wake-on-LAN wake: power on, not unblank
            if self.user_idle:
                self.user_idle = False
                self.event("SYSTEM_USER_BUSY")
            self.on_input_after_network_wake()
            return
        if not self.user_idle:
            return
        self.user_idle = False
        self.event("SYSTEM_USER_BUSY")
        self.each("unblank", [d for d in self.managed() if self.topo_ok(d)])
        if self.cfg["idle"]["mute"]:
            self.each("unmute")

    def start_idle(self):
        if self.idle_backend:
            self.idle_backend.stop()
            self.idle_backend = None
        ic = self.cfg["idle"]
        if not ic["enabled"]:
            return
        cb = lambda f: (lambda: GLib.idle_add(f))
        self.idle_backend = idle.create(ic["minutes"] * 60, cb(self.on_idle), cb(self.on_active), ic["respect_inhibit"])
        if self.idle_backend:
            self.idle_backend.start()
            self.log("user idle mode: %s backend, %d min" % (self.idle_backend.name, ic["minutes"]))
        else:
            self.log("user idle mode: no idle backend available in this session")

    # ------------------------------------------------------------ topology
    def poll_topology(self):
        conns = topology.connectors()
        for d in self.managed():
            c = topology.match(d, conns)
            if c is None:
                continue
            self.poll_dpms(d, c)
            if not self.cfg["topology"]["enabled"]:
                continue
            now = topology.active(c)
            was = self.topo.get(d["name"])
            self.topo[d["name"]] = now
            if was is None or was == now:
                continue
            self.log("%s: output %s %s" % (d["name"], c["connector"], "active" if now else "inactive"))
            if now and not self.user_idle and not self.streaming and not self.network_woken:
                self.each("on", [d])
            elif not now:
                self.each("off", [d])
        return True

    def poll_dpms(self, d, c):
        """The desktop turned the TV's output off/on (its own screen-off timer): follow it, like the
        original's display off/on events. Unforced, so the HDMI-input check applies."""
        if not topology.active(c):
            self.dpms.pop(d["name"], None)
            return
        now = topology.display_on(c)
        was = self.dpms.get(d["name"])
        self.dpms[d["name"]] = now
        if was is None or was == now or self.streaming:
            return
        if not now:
            self.event("SYSTEM_DISPLAYS_OFF")
            self.each("off", [d])
        elif self.network_woken:
            self.log("%s: display on after a network wake, TV stays off until input" % d["name"])
        else:
            self.event("SYSTEM_DISPLAYS_ON")
            self.user_idle = False
            self.each("on", [d])

    # ------------------------------------------------------------ streaming host
    def streaming_change(self, connected):
        if not self.cfg["streaming"]["enabled"]:
            return "streaming support is disabled"
        self.streaming = connected
        if connected:
            self.event("SYSTEM_DISPLAYS_OFF")
            self.each("off" if self.cfg["streaming"]["prefer_power_off"] else "blank")
        else:
            self.event("SYSTEM_DISPLAYS_ON")
            if self.cfg["streaming"]["end_mode"] == "power_on":
                self.each("on", [d for d in self.managed() if self.topo_ok(d)])
        return "ok"

    # ------------------------------------------------------------ commands / API
    def execute(self, argv):
        """Run a command line; returns list of result dicts."""
        out = []
        try:
            parsed = commands.tokenize(argv)
        except commands.CommandError as e:
            return [{"error": str(e)}]
        for cmd, args, devsel in parsed:
            if cmd in commands.OUTPUT_COMMANDS:
                continue
            devs = config.find_devices(self.cfg, devsel)
            if cmd in commands.DAEMON_COMMANDS:
                out.append({"command": cmd, "result": GLib_call(self._daemon_cmd, cmd, devs)})
                continue
            try:
                steps = commands.plan(cmd, args)
            except commands.CommandError as e:
                out.append({"command": cmd, "error": str(e)})
                continue
            for d in devs:
                r = self._locked(d, commands.run_steps, d, steps, self.cfg, self.log)
                out.append({"command": cmd, "device": d["name"], "result": r})
        return out

    def _daemon_cmd(self, cmd, devs):
        names = [d["name"] for d in devs]
        if cmd == "autoenable":
            self.session_off.difference_update(names)
            self.session_on.update(names)
            self.sync_persistent()
        elif cmd == "autodisable":
            self.session_on.difference_update(names)
            self.session_off.update(names)
            self.sync_persistent()
        elif cmd == "idle":
            if not self.cfg["idle"]["enabled"]:
                return "user idle mode is not enabled"
            self.on_idle(forced=True)
        elif cmd == "unidle":
            if not self.cfg["idle"]["enabled"]:
                return "user idle mode is not enabled"
            self.on_active()
        elif cmd == "streaming_connect":
            return self.streaming_change(True)
        elif cmd == "streaming_disconnect":
            return self.streaming_change(False)
        elif cmd == "clearlog":
            try:
                os.remove(config.LOG_PATH)
            except OSError:
                pass
        return "ok"

    def reload(self):
        self.cfg = config.load()
        self.topo = {}
        self.start_idle()
        self.apply_tray()
        self.sync_persistent()
        self.log("configuration reloaded")
        return False

    def serve(self):
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o600)
        srv.listen(8)
        threading.Thread(target=self._accept, args=(srv,), daemon=True).start()

    def _accept(self, srv):
        while True:
            c, _ = srv.accept()
            threading.Thread(target=self._client, args=(c,), daemon=True).start()

    def _client(self, c):
        with self.clients_lock:
            self.clients.append(c)
        f = c.makefile("r")
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if line == "prepare_sleep":    # for system hooks: the network is about to go down
                    res = {"result": self.power_off_before(time.time() + 4, "sleep (network going down)")}
                elif line == "reload":
                    GLib.idle_add(self.reload)
                    res = {"result": "ok"}
                elif line == "status":
                    res = {"user_idle": self.user_idle, "streaming": self.streaming, "tray": self.tray.registered,
                           "keep_screen_on": self.keep.active, "network_woken": self.network_woken,
                           "idle_backend": self.idle_backend.name if self.idle_backend else None,
                           "managed": [d["name"] for d in self.managed()], "topology": self.topo}
                else:
                    try:
                        res = self.execute(shlex.split(line))
                    except ValueError as e:
                        res = [{"error": str(e)}]
                c.sendall((json.dumps(res) + "\n").encode())
        except OSError:
            pass
        finally:
            with self.clients_lock:
                if c in self.clients:
                    self.clients.remove(c)
            c.close()

    def power_on_at_start(self, check=startup.check):
        """Boot/login (or recent input): power on, unless topology says the TV's output is off.
        A plain service restart leaves the TV as it is."""
        try:
            on, why = check()
        except Exception as e:
            on, why = True, "could not tell (%s)" % e
        self.log("start: %s" % why)
        if on:
            self.each("on", [d for d in self.managed() if self.cfg["topology"]["keep_on_boot"] or self.topo_ok(d)])

    # ------------------------------------------------------------ main
    def run(self):
        loop = GLib.MainLoop()
        self.log("LGTV Daemon daemon starting, %d device(s)" % len(self.cfg["devices"]))
        try:
            from .power import Power
            self.power = Power(lambda w: self.on_sleep(w), lambda: self.on_resume(),
                               lambda r, w: self.on_shutdown(r, w), self.log)
        except Exception as e:
            self.log("logind unavailable: %s" % e)
        self.serve()
        self.start_idle()
        self.apply_tray()
        self.power_on_at_start()
        self.sync_persistent()
        GLib.timeout_add_seconds(2, self.poll_topology)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGHUP, self.reload)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: loop.quit() or False)
        loop.run()
        self.keep.disable()


def GLib_call(fn, *a):
    """Run fn on the GLib main thread and wait for its result."""
    if threading.current_thread() is threading.main_thread():
        return fn(*a)
    ev, box = threading.Event(), []

    def run():
        box.append(fn(*a))
        ev.set()
        return False
    GLib.idle_add(run)
    ev.wait(30)
    return box[0] if box else "timeout"


def main():
    Daemon().run()
