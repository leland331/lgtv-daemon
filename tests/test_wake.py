import json
import os
import struct
import tempfile
import unittest

from lgtvc import wake

NIC = "0000:06:00.0"


def root_port_cfg(status=0, port_type=wake.PORT_ROOT):
    """256 bytes of config space: capability list -> PCIe capability at 0x40 with Root Status."""
    b = bytearray(256)
    struct.pack_into("<H", b, 0x06, 0x10)          # capability list present
    b[0x34] = 0x50
    b[0x50], b[0x51] = 0x01, 0x40                   # power management cap, then PCIe
    b[0x40], b[0x41] = wake.PCI_CAP_EXP, 0x00
    struct.pack_into("<H", b, 0x42, (port_type << 4) | 2)
    struct.pack_into("<I", b, 0x40 + wake.ROOT_STATUS, status)
    return bytes(b)


class ConfigSpaceTest(unittest.TestCase):
    def test_root_status_and_requester(self):
        off, v = wake.root_status(root_port_cfg(wake.PME_STATUS | 0x0600))
        self.assertEqual(off, 0x60)
        self.assertTrue(v & wake.PME_STATUS)
        self.assertEqual(wake.requester("0000:00:01.2", v), NIC)
        self.assertEqual(wake.requester("0000:00:08.1", 0x0C03), "0000:0c:00.3")

    def test_not_a_root_port(self):
        self.assertIsNone(wake.root_status(root_port_cfg(port_type=0)))   # endpoint
        self.assertIsNone(wake.root_status(bytes(256)))                   # no capability list
        self.assertIsNone(wake.root_status(b""))


class HookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pci = os.path.join(self.tmp.name, "devices")
        self.state = os.path.join(self.tmp.name, "wake.json")
        for addr, cfg in (("0000:00:01.2", root_port_cfg(wake.PME_STATUS | 0x0600)),
                          ("0000:00:08.1", root_port_cfg(0)), (NIC, root_port_cfg(port_type=0))):
            os.makedirs(os.path.join(self.pci, addr))
            with open(os.path.join(self.pci, addr, "config"), "wb") as f:
                f.write(cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_post_records_requester(self):
        wake.hook("post", self.pci, self.state, wait=0)
        st = json.load(open(self.state))
        self.assertEqual(st["phase"], "post")
        self.assertEqual(st["pme"], [{"port": "0000:00:01.2", "requester": NIC}])

    def test_pre_clears_latched_status(self):
        wake.hook("pre", self.pci, self.state)
        with open(os.path.join(self.pci, "0000:00:01.2", "config"), "rb") as f:
            cfg = f.read()
        # a real register clears on writing 1; the fake file just holds what was written
        self.assertEqual(struct.unpack_from("<I", cfg, 0x60)[0], wake.PME_STATUS)
        with open(os.path.join(self.pci, "0000:00:08.1", "config"), "rb") as f:
            self.assertEqual(struct.unpack_from("<I", f.read(), 0x60)[0], 0)   # untouched
        self.assertEqual(json.load(open(self.state))["phase"], "pre")

    def test_main_never_raises(self):
        self.assertEqual(wake.main(["hook", "post"]), 0)   # real paths, likely not writable here


class NetTest(unittest.TestCase):
    def test_cards_and_counts(self):
        with tempfile.TemporaryDirectory() as d:
            dev = os.path.join(d, "pci0000:00", NIC)
            os.makedirs(os.path.join(dev, "power"))
            with open(os.path.join(dev, "power", "wakeup_count"), "w") as f:
                f.write("3\n")
            net = os.path.join(d, "net")
            os.makedirs(os.path.join(net, "enp6s0"))
            os.symlink(dev, os.path.join(net, "enp6s0", "device"))
            os.makedirs(os.path.join(net, "lo"))                 # virtual: no device link
            self.assertEqual(wake.network_cards(net), {NIC: "enp6s0"})
            self.assertEqual(wake.wakeup_counts(net), {"enp6s0": 3})


class DecideTest(unittest.TestCase):
    cards = {NIC: "enp6s0"}
    before = {"time": 100.0, "counts": {"enp6s0": 0}}

    def post(self, *requesters, t=200.0):
        return {"phase": "post", "time": t, "pme": [{"port": "0000:00:01.2", "requester": r} for r in requesters]}

    def test_pme_from_network_card(self):
        src, why = wake.decide(self.before, {"enp6s0": 0}, self.post(NIC), self.cards)
        self.assertEqual(src, "network")
        self.assertIn("enp6s0", why)

    def test_pme_from_usb_is_other(self):
        src, why = wake.decide(self.before, {"enp6s0": 0}, self.post("0000:0c:00.3"), self.cards)
        self.assertEqual((src, why), ("other", "woken by 0000:0c:00.3"))

    def test_power_button_nothing_latched(self):
        self.assertEqual(wake.decide(self.before, {"enp6s0": 0}, self.post(), self.cards)[0], "other")

    def test_stale_hook_state_ignored(self):
        src, why = wake.decide(self.before, {"enp6s0": 0}, self.post(NIC, t=50.0), self.cards)
        self.assertEqual(src, "other")
        self.assertIn("not installed", why)

    def test_counter_fallback(self):
        src, why = wake.decide(self.before, {"enp6s0": 1}, None, self.cards)
        self.assertEqual(src, "network")
        self.assertIn("0 to 1", why)

    def test_no_snapshot_no_hook(self):
        self.assertEqual(wake.decide(None, {"enp6s0": 5}, None, self.cards)[0], "other")


class FakeDaemon:
    """The Daemon's power/idle methods on a stub, without GLib, logind or TVs."""

    def __init__(self, cfg_flag=True, source=("network", "test")):
        from lgtvc import daemon
        self.D = daemon.Daemon
        self.cfg = {"stay_off_on_network_wake": cfg_flag, "idle": {"mute": False}, "topology": {"enabled": False},
                    "api": {"enabled": False}}
        self.network_woken = self.user_idle = False
        self.sleep_snapshot = None
        self.calls, self.logs, self.watch = [], [], None
        daemon.wake.detect = lambda before: source

    def __getattr__(self, name):
        return getattr(self.D, name).__get__(self)

    def each(self, action, devices=None, wait=False):
        self.calls.append(action)

    def managed(self):
        return []

    def event(self, name):
        self.calls.append(name)

    def log(self, m):
        self.logs.append(m)

    def watch_input(self, on_input=None, on_unavailable=None):
        self.watch = (on_input, on_unavailable)

    def stop_input_watch(self):
        pass


@unittest.skipUnless(__import__("importlib").util.find_spec("gi"), "PyGObject not installed")
class DaemonFlowTest(unittest.TestCase):
    def setUp(self):
        from lgtvc import daemon
        self.orig = daemon.wake.detect
        self.addCleanup(setattr, daemon.wake, "detect", self.orig)

    def test_network_wake_stays_off_then_input_powers_on(self):
        d = FakeDaemon()
        d.on_resume()
        self.assertEqual(d.calls, ["SYSTEM_RESUME"])
        self.assertTrue(d.network_woken)
        d.watch[0]()                      # first keyboard/mouse input
        self.assertEqual(d.calls, ["SYSTEM_RESUME", "on"])
        self.assertFalse(d.network_woken)
        d.watch[0]()                      # later input does nothing more
        self.assertEqual(d.calls.count("on"), 1)

    def test_user_wake_powers_on(self):
        d = FakeDaemon(source=("other", "power button"))
        d.on_resume()
        self.assertEqual(d.calls, ["SYSTEM_RESUME", "on"])
        self.assertFalse(d.network_woken)

    def test_option_off_powers_on(self):
        d = FakeDaemon(cfg_flag=False)
        d.on_resume()
        self.assertEqual(d.calls, ["SYSTEM_RESUME", "on"])

    def test_no_input_detection_falls_back_to_on(self):
        d = FakeDaemon()
        d.on_resume()
        d.watch[1]()
        self.assertEqual(d.calls, ["SYSTEM_RESUME", "on"])

    def test_unidle_after_network_wake_powers_on(self):
        d = FakeDaemon()
        d.on_resume()
        d.user_idle = True                # idle blank happened while the TV was off
        d.on_active()
        self.assertEqual(d.calls, ["SYSTEM_RESUME", "SYSTEM_USER_BUSY", "on"])
        self.assertFalse(d.user_idle or d.network_woken)


if __name__ == "__main__":
    unittest.main()
