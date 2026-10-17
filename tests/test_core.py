import json
import os
import tempfile
import unittest

from lgtvc import commands, config, idle, topology, wol
from lgtvc.power import is_reboot
from lgtvc.webos import luna_payload


def edid(mfr="GSM", name="LG TV SSCR2"):
    b = bytearray(128)
    b[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    v = sum((ord(c) - 64) << s for c, s in zip(mfr, (10, 5, 0)))
    b[8], b[9] = v >> 8, v & 0xFF
    b[54:59] = b"\0\0\0\xfc\0"
    b[59:72] = (name.encode() + b"\n").ljust(13, b" ")[:13]
    return bytes(b)


class ConfigTest(unittest.TestCase):
    def test_defaults_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            cfg = config.load(p)
            self.assertEqual(cfg["idle"]["minutes"], 10)
            cfg["devices"].append({"ip": "10.0.0.2", "macs": "AA-BB-CC-DD-EE-FF, 112233445566", "wol": "bogus"})
            config.save(cfg, p)
            again = config.load(p)
            dev = again["devices"][0]
            self.assertEqual(dev["macs"], ["aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"])
            self.assertEqual(dev["wol"], "broadcast")
            self.assertEqual(dev["name"], "Device1")
            self.assertEqual(dev["interface"], "")
            self.assertEqual(oct(os.stat(p).st_mode & 0o777), "0o600")

    def test_bad_mac(self):
        with self.assertRaises(ValueError):
            config.normalize({"devices": [{"macs": ["zz"]}]})

    def test_find_devices(self):
        cfg = config.normalize({"devices": [{"name": "A"}, {"name": "OLED 42"}]})
        self.assertEqual(len(config.find_devices(cfg, [])), 2)
        self.assertEqual([d["name"] for d in config.find_devices(cfg, ["Device2"])], ["OLED 42"])
        self.assertEqual([d["name"] for d in config.find_devices(cfg, ["OLED 42", "device1"])], ["OLED 42", "A"])


class CliParseTest(unittest.TestCase):
    def test_tokenize(self):
        t = commands.tokenize(["-poweron", "Device1", "OLED 42", "-backlight", "50", "-of"])
        self.assertEqual(t, [("poweron", [], ["Device1", "OLED 42"]), ("backlight", ["50"], []), ("of", [], [])])

    def test_negative_number_is_argument(self):
        self.assertEqual(commands.tokenize(["-colortemperature", "-10"])[0], ("colortemperature", ["-10"], []))

    def test_errors(self):
        for argv in (["-nope"], ["-volume"], ["poweron"]):
            with self.assertRaises(commands.CommandError):
                commands.tokenize(argv)

    def test_every_command_known(self):
        doc = os.path.join(os.path.dirname(__file__), "upstream-commands.txt")
        names = set(open(doc).read().split())
        missing = sorted(n for n in names if n not in commands.ARGC and n + "1" not in commands.ARGC)
        self.assertEqual(missing, [])

    def test_plans(self):
        self.assertEqual(commands.plan("backlight", ["50"]), [("settings", "picture", {"backlight": 50})])
        self.assertEqual(commands.plan("gamemode_hdmi2", ["on"]), [("settings", "other", {"gameMode": {"hdmi2": "on"}})])
        self.assertEqual(commands.plan("sethdmi", ["3"])[0][2], {"id": "com.webos.app.hdmi3"})
        self.assertEqual(commands.plan("sethdmi4", [])[0][2], {"id": "com.webos.app.hdmi4"})
        self.assertEqual(commands.plan("button", ["home"]), [("button", "HOME")])
        self.assertEqual(len(commands.plan("set_curvature", ["50"])), 4)
        self.assertEqual(commands.plan("lowleveladjustment", ["-5"])[0][2], {"lowLevelAdjustment": -5})
        with self.assertRaises(commands.CommandError):
            commands.plan("backlight", ["101"])
        with self.assertRaises(commands.CommandError):
            commands.plan("picturemode", ["nonsense"])
        with self.assertRaises(commands.CommandError):
            commands.plan("button", ["NOTAKEY"])

    def test_output_formats(self):
        obj = {"a": {"backlight": 80}}
        self.assertEqual(commands.format_output(obj, "key", "backlight"), "80")
        self.assertIn("\n", commands.format_output(obj, "friendly"))

    def test_luna_payload(self):
        p = luna_payload("luna://x/y", {"k": 1})
        self.assertEqual(p["onclose"], {"uri": "luna://x/y", "params": {"k": 1}})
        self.assertEqual(p["buttons"][0]["onClick"], "luna://x/y")


class IdleSelectTest(unittest.TestCase):
    W = {"WAYLAND_DISPLAY": "wayland-0", "XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}

    def pick(self, env, *ok, boom=()):
        def mk(k):
            def f():
                if k in boom:
                    raise RuntimeError("dbus down")
                return k in ok
            return f
        return idle.select_backend(env, {k: mk(k) for k in idle.ORDER})

    def test_order_and_fallbacks(self):
        self.assertEqual(self.pick(self.W, *idle.ORDER), "wayland")
        self.assertEqual(self.pick(self.W, "kwin", "gnome", "x11"), "kwin")         # Plasma 5 without ext-idle
        self.assertEqual(self.pick(self.W, "gnome", "x11"), "gnome")                # GNOME Wayland
        self.assertEqual(self.pick(self.W, "x11", "logind"), "x11")                 # Xwayland only
        self.assertEqual(self.pick(self.W, "screensaver", "logind", boom=("gnome",)), "screensaver")
        self.assertEqual(self.pick(self.W, "logind"), "logind")

    def test_x11_session_skips_wayland(self):
        env = dict(self.W, XDG_SESSION_TYPE="x11")
        self.assertEqual(self.pick(env, "wayland", "kwin", "x11"), "x11")
        self.assertEqual(self.pick({"DISPLAY": ":0"}, "wayland", "screensaver"), "screensaver")

    def test_none(self):
        self.assertIsNone(self.pick({}, "wayland", "x11"))
        self.assertIsNone(self.pick(self.W))

    def test_logind_seconds(self):
        self.assertEqual(idle.logind_seconds(False, 5e6, 100), 0.0)
        self.assertEqual(idle.logind_seconds(True, 40e6, 100), 60.0)

    def test_process_exclusion(self):
        me = list(idle.running_processes())[:1]
        self.assertEqual(idle.excluded_process(me), me[0])
        self.assertEqual(idle.excluded_process(["definitely-not-running-xyz"]), "")


class TopologyTest(unittest.TestCase):
    def make(self, root, name, status, enabled, data=b""):
        d = os.path.join(root, name)
        os.makedirs(d)
        open(os.path.join(d, "status"), "w").write(status + "\n")
        open(os.path.join(d, "enabled"), "w").write(enabled + "\n")
        open(os.path.join(d, "edid"), "wb").write(data)

    def test_edid(self):
        self.assertEqual(topology.parse_edid(edid()), {"mfr": "GSM", "name": "LG TV SSCR2", "serial": ""})
        self.assertEqual(topology.parse_edid(b""), {})

    def test_scan_and_match(self):
        with tempfile.TemporaryDirectory() as r:
            self.make(r, "card0-DP-1", "connected", "enabled", edid("DEL", "DELL U2720Q"))
            self.make(r, "card0-HDMI-A-1", "connected", "enabled", edid("GSM", "LG TV SSCR2"))
            self.make(r, "card0-HDMI-A-2", "disconnected", "disabled")
            conns = topology.connectors(r, overlay=False)
            self.assertEqual(len(conns), 3)
            m = topology.match({"model": ""}, conns)
            self.assertEqual(m["connector"], "HDMI-A-1")
            self.assertTrue(topology.active(m))
            self.assertEqual(topology.match({"connector": "DP-1"}, conns)["mfr"], "DEL")
            self.assertIsNone(topology.match({"connector": "HDMI-A-9"}, conns))

    def test_two_lg_needs_model(self):
        with tempfile.TemporaryDirectory() as r:
            self.make(r, "card0-HDMI-A-1", "connected", "enabled", edid("GSM", "LG TV SSCR2"))
            self.make(r, "card0-HDMI-A-2", "connected", "disabled", edid("GSM", "LG OLED65G3"))
            conns = topology.connectors(r, overlay=False)
            self.assertIsNone(topology.match({}, conns))
            m = topology.match({"model": "OLED65G34LA"}, conns)
            self.assertEqual(m["connector"], "HDMI-A-2")
            self.assertFalse(topology.active(m))


class PowerTest(unittest.TestCase):
    def test_reboot_detection(self):
        self.assertTrue(is_reboot({"type": "reboot"}))
        self.assertFalse(is_reboot({"type": "poweroff"}))
        self.assertTrue(is_reboot(None, [("reboot.target", "start")]))
        self.assertFalse(is_reboot(None, [("poweroff.target", "start")]))
        self.assertFalse(is_reboot(None, []))


class WolTest(unittest.TestCase):
    def test_packet(self):
        p = wol.magic_packet("aa:bb:cc:dd:ee:ff")
        self.assertEqual(len(p), 102)
        self.assertEqual(p[6:12], bytes.fromhex("aabbccddeeff"))

    def test_targets(self):
        ifs = [{"ip": "10.1.2.3", "broadcast": "10.1.2.255"}]
        self.assertEqual(wol.targets("ip", "10.1.2.9"), [("", "10.1.2.9")])
        self.assertEqual(wol.targets("subnet", "10.1.2.9", "255.255.255.0"), [("", "10.1.2.255")])
        self.assertEqual(wol.targets("broadcast", "10.1.2.9", ifaces=ifs),
                         [("", "255.255.255.255"), ("10.1.2.3", "10.1.2.255")])


class XvfbTest(unittest.TestCase):
    """Exercise the real X11 idle backend if Xvfb is installed."""

    def test_x11_idle(self):
        import shutil
        import subprocess
        import time
        if not shutil.which("Xvfb"):
            self.skipTest("Xvfb not installed")
        p = subprocess.Popen(["Xvfb", ":97", "-nolisten", "tcp"], stderr=subprocess.DEVNULL)
        try:
            time.sleep(1)
            old = os.environ.get("DISPLAY")
            os.environ["DISPLAY"] = ":97"
            try:
                self.assertEqual(idle.select_backend({"DISPLAY": ":97"}, {"x11": idle.probe_xss}), "x11")
                b = idle.X11Idle(1, lambda: None, lambda: None)
                self.assertIsNotNone(b.idle_seconds())
            finally:
                if old is None:
                    del os.environ["DISPLAY"]
                else:
                    os.environ["DISPLAY"] = old
        finally:
            p.terminate()


if __name__ == "__main__":
    unittest.main()


class FakeTV:
    def __init__(self, state, app):
        self.state, self.app, self.sent = state, app, []

    def power_state(self):
        return self.state

    def request(self, uri, payload=None):
        self.sent.append(uri)
        return {"appId": self.app} if uri.endswith("getForegroundAppInfo") else {"returnValue": True}

    def close(self):
        pass


class HdmiCheckTest(unittest.TestCase):
    dev = {"name": "TV", "check_hdmi_input_when_power_off": True, "source_hdmi_input": 2}

    def run_off(self, state, app, blank=False, forced=False, dev=None):
        from lgtvc import webos
        tv = FakeTV(state, app)
        act = webos.power_off(dev or self.dev, blank, log=lambda m: None, forced=forced, connect_fn=lambda d: tv)
        return act, tv.sent

    def test_right_input_off(self):
        act, sent = self.run_off("Active", "com.webos.app.hdmi2")
        self.assertTrue(act)
        self.assertEqual(sent[-1], "system/turnOff")

    def test_other_input_skip(self):
        act, sent = self.run_off("Active", "com.webos.app.hdmi3")
        self.assertFalse(act)
        self.assertNotIn("system/turnOff", sent)

    def test_app_skip(self):
        act, sent = self.run_off("Active", "netflix")
        self.assertFalse(act)
        self.assertNotIn("system/turnOff", sent)

    def test_forced_off_without_query(self):
        act, sent = self.run_off("Active", "netflix", forced=True)
        self.assertTrue(act)
        self.assertEqual(sent, ["system/turnOff"])

    def test_blank_right_input(self):
        act, sent = self.run_off("Active", "com.webos.app.hdmi2", blank=True)
        self.assertTrue(act)
        self.assertEqual(sent[-1], "com.webos.service.tvpower/power/turnOffScreen")

    def test_blank_needs_active(self):
        self.assertFalse(self.run_off("Screen Off", "com.webos.app.hdmi2", blank=True)[0])

    def test_already_off(self):
        self.assertEqual(self.run_off("Active Standby", "")[1], [])

    def test_check_disabled(self):
        act, sent = self.run_off("Active", "netflix", dev={"name": "TV"})
        self.assertTrue(act)
        self.assertEqual(sent, ["system/turnOff"])

    def test_power_on_switches_input(self):
        from lgtvc import webos
        tv = FakeTV("Active", "")
        dev = {"name": "TV", "macs": [], "wol": "ip", "ip": "127.0.0.1", "set_hdmi_input_on_power_on": True,
               "set_hdmi_input_on_power_on_delay": 0, "source_hdmi_input": 3}
        self.assertTrue(webos.power_on(dev, 5, log=lambda m: None, connect_fn=lambda d: tv))
        self.assertEqual(tv.sent, ["system.launcher/launch"])

    def test_clamps(self):
        d = config.normalize({"devices": [{"source_hdmi_input": 9, "set_hdmi_input_on_power_on_delay": 99}]})["devices"][0]
        self.assertEqual((d["source_hdmi_input"], d["set_hdmi_input_on_power_on_delay"]), (4, 30))


class PassiveTest(unittest.TestCase):
    """The app never inhibits or changes idle/lock/DPMS behaviour unless the user turns Keep Screen On on."""

    def test_no_idle_inhibitors_or_dpms_calls(self):
        import glob
        d = os.path.join(os.path.dirname(__file__), "..", "lgtvc")
        src = "".join(open(p).read() for p in glob.glob(os.path.join(d, "*.py")) if not p.endswith("keepawake.py"))
        for bad in ("xset", "org.freedesktop.ScreenSaver.Inhibit", "DPMSForceLevel", "zwp_idle_inhibit",
                    "SetIdleHint", "powerdevil", '"idle", "LGTV'):
            self.assertNotIn(bad, src)
        # the only logind inhibitors taken outside keepawake.py are sleep/shutdown delay locks
        self.assertIn('for what in ("sleep", "shutdown")', src)

    def test_keep_screen_on_is_explicit(self):
        from lgtvc import keepawake
        k = keepawake.KeepAwake(lambda m: None)
        self.assertFalse(k.active)       # nothing inhibited until the user toggles it on
        import inspect
        from lgtvc import daemon
        for name in ("run", "reload", "__init__", "apply_tray"):
            self.assertNotIn("keep.enable", inspect.getsource(getattr(daemon.Daemon, name)))

    def test_dpms_field(self):
        with tempfile.TemporaryDirectory() as r:
            d = os.path.join(r, "card0-HDMI-A-1")
            os.makedirs(d)
            for f, v in (("status", "connected"), ("enabled", "enabled"), ("dpms", "Off")):
                open(os.path.join(d, f), "w").write(v + "\n")
            c = topology.connectors(r, overlay=False)[0]
            self.assertFalse(topology.display_on(c))
            self.assertTrue(topology.active(c))


class SessionTopologyTest(unittest.TestCase):
    def c(self, n, dpms="On"):
        return {"connector": n, "connected": True, "enabled": True, "dpms": dpms, "mfr": "GSM", "name": "LG TV"}

    def test_wayland_power_and_enabled(self):
        r = topology.apply_session([self.c("HDMI-A-1"), self.c("DP-2")], {"HDMI-A-1": False}, None, None)
        self.assertEqual((r[0]["dpms"], r[1]["enabled"]), ("Off", False))   # stale sysfs On corrected

    def test_global_dpms_and_xrandr_fallback(self):
        self.assertEqual(topology.apply_session([self.c("DP-1")], None, False, None)[0]["dpms"], "Off")
        edid = bytes.fromhex("00ffffffffffff001e6d") + bytes(118)
        txt = "HDMI-0 connected primary 3840x2160+0+0 (normal) 0mm x 0mm\n\tEDID: \n" + "".join(
            "\t\t%s\n" % edid[i:i + 16].hex() for i in range(0, 128, 16)) + "\tCTM: 0\nDP-1-1 disconnected\n"
        r = topology.apply_session([], None, None, topology.parse_xrandr(txt))
        self.assertEqual([(x["connector"], x["enabled"], x["mfr"]) for x in r],
                         [("HDMI-0", True, "GSM"), ("DP-1-1", False, "")])
