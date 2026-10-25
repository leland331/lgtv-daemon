import time
import unittest
from unittest import mock

from lgtvc import config, webos

try:
    from lgtvc import daemon
except ImportError:          # needs PyGObject
    daemon = None

DEV = config.normalize({"devices": [{"name": "LG", "ip": "192.168.50.2", "key": "k"}]})["devices"][0]


class PowerOnCancelTest(unittest.TestCase):
    def test_cancel_stops_wol(self):
        with mock.patch.object(webos.wol, "wake") as wake:
            ok = webos.power_on(DEV, timeout=5, log=lambda m: None, connect_fn=lambda d: None, cancel=lambda: True)
        self.assertFalse(ok)
        wake.assert_not_called()


class FakePool:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *a):
        self.calls.append(a)


@unittest.skipIf(daemon is None, "needs PyGObject")
class UserOffTest(unittest.TestCase):
    def make(self):
        d = object.__new__(daemon.Daemon)
        d.cfg = config.normalize({"devices": [DEV]})
        d.session_off, d.session_on = set(), set()
        d.user_off, d.on_busy, d.own_off_at = set(), set(), {}
        d.sleep_off_at = d.resumed_at = 0.0
        d.network_woken = False
        d.pool = FakePool()
        d.logs = []
        d.log = d.logs.append
        d.watch_input = mock.Mock()
        d.stop_input_watch = mock.Mock()
        d.topo_ok = lambda dev: True
        return d

    def test_external_off_detection(self):
        d = self.make()
        now = time.time()
        self.assertTrue(d.is_external_off("LG", "Active", "Active Standby", now))
        self.assertTrue(d.is_external_off("LG", "Active", "", now))           # socket dropped while on
        self.assertFalse(d.is_external_off("LG", "", "Active Standby", now))  # first report, not a change
        self.assertFalse(d.is_external_off("LG", "Screen Off", "", now))
        self.assertFalse(d.is_external_off("LG", "Active Standby", "Active", now))
        d.own_off_at["LG"] = now - 3                                          # we turned it off ourselves
        self.assertFalse(d.is_external_off("LG", "Active", "Active Standby", now))
        d.own_off_at["LG"] = 0
        d.resumed_at = now - 2                                                # sockets dying around resume
        self.assertFalse(d.is_external_off("LG", "Active", "", now))

    def test_stays_off_until_input(self):
        d = self.make()
        d.mark_user_off("LG")
        d.watch_input.assert_called_once_with(d.on_input_after_user_off)
        d.each("on")                    # e.g. display came on at the lock screen
        d.each("unblank")
        self.assertEqual(d.pool.calls, [])
        d.on_input_after_user_off()     # fresh keyboard/mouse input
        self.assertEqual(len(d.pool.calls), 1)
        self.assertEqual(d.user_off, set())

    def test_power_on_debounced(self):
        d = self.make()
        d.each("on")
        d.each("on")
        d.each("on")
        self.assertEqual(len(d.pool.calls), 1)

    def test_turned_on_at_tv_clears(self):
        d = self.make()
        d.mark_user_off("LG")
        d.clear_user_off("LG")
        d.each("on")
        self.assertEqual(len(d.pool.calls), 1)

    def test_own_off_recorded(self):
        d = self.make()
        d.each("off")
        self.assertIn("LG", d.own_off_at)


if __name__ == "__main__":
    unittest.main()
