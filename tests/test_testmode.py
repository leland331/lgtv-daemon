import os
import tempfile
import unittest
from unittest import mock

from lgtvc import config, testmode

try:
    from lgtvc import daemon
except ImportError:          # needs PyGObject
    daemon = None

DEV = config.normalize({"devices": [{"name": "LG", "ip": "192.168.50.2", "key": "k"}]})["devices"][0]


class FlagTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def flag(self, text=None):
        p = os.path.join(self.dir.name, "flag")
        if text is not None:
            with open(p, "w") as f:
                f.write(text)
        return p

    def test_default_empty(self):
        self.assertEqual(config.normalize({})["test_mode_flags"], [])
        self.assertFalse(testmode.active([]))

    def test_flag_absent(self):
        self.assertFalse(testmode.active([self.flag()]))

    def test_file_flag(self):
        self.assertTrue(testmode.active([self.flag("")]))
        self.assertTrue(testmode.active([self.flag("running tests\n")]))

    def test_live_pid(self):
        self.assertTrue(testmode.active([self.flag(str(os.getpid()))]))

    def test_stale_pid(self):
        self.assertFalse(testmode.active([self.flag("4242")], alive=lambda pid: False))
        self.assertFalse(testmode.active([self.flag("0")]))

    def test_any_flag_counts(self):
        self.assertTrue(testmode.active(["/nonexistent/x", self.flag("")]))


class FakePool:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *a):
        self.calls.append(a)


@unittest.skipIf(daemon is None, "needs PyGObject")
class DaemonTest(unittest.TestCase):
    def make(self):
        d = object.__new__(daemon.Daemon)
        d.cfg = config.normalize({"devices": [DEV], "test_mode_flags": ["/flag"]})
        d.session_off, d.session_on = set(), set()
        d.user_off, d.on_busy, d.own_off_at = set(), set(), {}
        d.sleep_off_at = d.resumed_at = 0.0
        d.network_woken = d.user_idle = d.streaming = d.test_mode = False
        d.test_blank_at, d.persist, d.dpms = {}, {}, {}
        d.pool = FakePool()
        d.logs = []
        d.log = d.logs.append
        d.event = mock.Mock()
        d.watch_input = d.stop_input_watch = mock.Mock()
        d.topo_ok = lambda dev: True
        d.idle_excluded = lambda: ""
        return d

    def poll(self, d, on):
        with mock.patch.object(daemon.testmode, "active", return_value=on):
            d.poll_test_mode()

    def actions(self, d):
        return [c[0].__name__ if hasattr(c[0], "__name__") else c[0] for c in d.pool.calls]

    def test_on_blanks_and_keeps_blank(self):
        d = self.make()
        self.poll(d, True)
        self.assertTrue(d.test_mode)
        self.assertEqual(len(d.pool.calls), 1)          # blank sent
        self.assertIs(d.pool.calls[0][1], daemon.webos.power_off)
        self.assertTrue(d.pool.calls[0][3])             # blank=True
        d.pool.calls.clear()
        p = mock.Mock(state="Active")
        p.alive.return_value = True
        d.persist["LG"] = p
        d.test_blank_at["LG"] = 0
        self.poll(d, True)                              # TV un-blanked: blank again
        self.assertEqual(len(d.pool.calls), 1)

    def test_input_ignored(self):
        d = self.make()
        d.user_idle = True
        d.test_mode = True
        d.on_active()
        d.on_input_after_user_off()
        d.on_input_after_network_wake()
        d.on_input_after_off()
        self.assertEqual(d.pool.calls, [])
        self.assertTrue(d.user_idle)

    def test_idle_ignored(self):
        d = self.make()
        d.test_mode = True
        d.on_idle()
        self.assertFalse(d.user_idle)
        self.assertEqual(d.pool.calls, [])

    def test_displays_off_ignored(self):
        d = self.make()
        d.test_mode = True
        d.dpms["LG"] = True
        with mock.patch.object(daemon.topology, "active", return_value=True), \
                mock.patch.object(daemon.topology, "display_on", return_value=False):
            d.poll_dpms(DEV, {})
        self.assertEqual(d.pool.calls, [])
        d.event.assert_not_called()
        self.assertFalse(d.each("off", [DEV]))
        self.assertEqual(d.pool.calls, [])

    def test_displays_off_acts_when_off(self):
        d = self.make()
        d.dpms["LG"] = True
        with mock.patch.object(daemon.topology, "active", return_value=True), \
                mock.patch.object(daemon.topology, "display_on", return_value=False):
            d.poll_dpms(DEV, {})
        self.assertEqual(len(d.pool.calls), 1)

    def test_restore_on_off(self):
        d = self.make()
        self.poll(d, True)
        d.pool.calls.clear()
        d.user_idle = True
        self.poll(d, False)
        self.assertFalse(d.test_mode)
        self.assertFalse(d.user_idle)
        self.assertEqual(len(d.pool.calls), 1)
        self.assertIs(d.pool.calls[0][1], daemon.webos.screen_on)   # un-blank, not a power on
        self.assertTrue(any("test mode off" in m for m in d.logs))


if __name__ == "__main__":
    unittest.main()
