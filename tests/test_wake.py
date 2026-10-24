import json
import os
import tempfile
import time
import unittest

from lgtvc import wake

NIC = "0000:06:00.0"


class SysfsTest(unittest.TestCase):
    """A fake /sys with one network card (wakeup counter, wakeup source, MSI IRQs) and lo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.dev = os.path.join(d, "devices", "pci0000:00", NIC)
        os.makedirs(os.path.join(self.dev, "power"))
        os.makedirs(os.path.join(self.dev, "msi_irqs", "41"))
        self.write(os.path.join(self.dev, "power", "wakeup_count"), "3")
        self.write(os.path.join(self.dev, "irq"), "0")
        self.net = os.path.join(d, "net")
        os.makedirs(os.path.join(self.net, "enp6s0"))
        os.symlink(self.dev, os.path.join(self.net, "enp6s0", "device"))
        os.makedirs(os.path.join(self.net, "lo"))                    # virtual: no device link
        self.wakeup = os.path.join(d, "wakeup")
        for ws, owner, n in (("wakeup5", self.dev, "7"), ("wakeup6", os.path.join(d, "devices", "usb1"), "9")):
            os.makedirs(os.path.join(self.wakeup, ws))
            os.makedirs(owner, exist_ok=True)
            os.symlink(owner, os.path.join(self.wakeup, ws, "device"))
            self.write(os.path.join(self.wakeup, ws, "wakeup_count"), n)
        self.irq = os.path.join(d, "pm_wakeup_irq")
        self.hook = os.path.join(d, "wake.json")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def write(path, text):
        with open(path, "w") as f:
            f.write(text + "\n")

    def detect(self, before):
        return wake.detect(before, self.net, self.wakeup, self.irq, self.hook)

    def test_cards_counts_irqs(self):
        self.assertEqual(wake.network_cards(self.net), {"enp6s0": os.path.realpath(self.dev)})
        self.assertEqual(wake.wakeup_counts(self.net, self.wakeup), {"enp6s0": 3, "enp6s0/wakeup5": 7})
        self.assertEqual(wake.card_irqs(self.net), {41: "enp6s0"})

    def test_counter_went_up(self):
        before = wake.snapshot(self.net, self.wakeup)
        self.write(os.path.join(self.wakeup, "wakeup5", "wakeup_count"), "8")
        self.assertEqual(self.detect(before)[0], "network")

    def test_nothing_moved(self):
        before = wake.snapshot(self.net, self.wakeup)
        self.write(os.path.join(self.wakeup, "wakeup6", "wakeup_count"), "10")   # USB keyboard, not the NIC
        self.assertEqual(self.detect(before), ("other", "no network wake seen"))

    def test_wakeup_irq(self):
        before = wake.snapshot(self.net, self.wakeup)
        self.write(self.irq, "41")
        self.assertEqual(self.detect(before), ("network", "woken by IRQ 41 (enp6s0)"))
        self.write(self.irq, "9")
        self.assertEqual(self.detect(before)[0], "other")

    def test_wake_source_file(self):
        before = wake.snapshot(self.net, self.wakeup)
        with open(self.hook, "w") as f:
            json.dump({"time": time.time() + 1, "source": "network", "detail": "root port PME"}, f)
        self.assertEqual(self.detect(before), ("network", "wake-source file: root port PME"))
        with open(self.hook, "w") as f:                                  # older than the sleep: ignored
            json.dump({"time": before["time"] - 10, "source": "network"}, f)
        self.assertEqual(self.detect(before)[0], "other")
        with open(self.hook, "w") as f:
            f.write("not json")
        self.assertEqual(self.detect(before)[0], "other")


class DecideTest(unittest.TestCase):
    before = {"time": 100.0, "counts": {"enp6s0": 0}}

    def test_file_says_other_beats_counters(self):
        hook = {"time": 200.0, "source": "other", "detail": "power button"}
        self.assertEqual(wake.decide(self.before, {"enp6s0": 1}, None, {}, hook)[0], "other")

    def test_counter(self):
        src, why = wake.decide(self.before, {"enp6s0": 1}, None, {}, None)
        self.assertEqual(src, "network")
        self.assertIn("0 to 1", why)

    def test_no_snapshot(self):
        self.assertEqual(wake.decide(None, {"enp6s0": 5}, None, {}, {"time": 5, "source": "network"})[0], "other")


class FakeDaemon:
    """The Daemon's power/idle methods on a stub, without GLib, logind or TVs."""

    def __init__(self, cfg_flag=True, source=("network", "test")):
        from lgtvc import daemon
        self.D = daemon.Daemon
        self.cfg = {"stay_off_on_network_wake": cfg_flag, "idle": {"mute": False}, "topology": {"enabled": False},
                    "api": {"enabled": False}}
        self.network_woken = self.user_idle = False
        self.sleep_snapshot = None
        self.persist, self.user_off, self.sleep_off_at, self.resumed_at = {}, set(), 0.0, 0.0
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
