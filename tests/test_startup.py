import os
import tempfile
import unittest
from unittest import mock

from lgtvc import startup

try:
    from lgtvc import daemon
except ImportError:          # needs PyGObject
    daemon = None


class StartupTest(unittest.TestCase):
    def test_marker(self):
        with tempfile.TemporaryDirectory() as d:
            path = startup.marker_path({"XDG_RUNTIME_DIR": d})
            self.assertTrue(startup.first_start(path, "boot-a"))      # boot/login
            self.assertFalse(startup.first_start(path, "boot-a"))     # service restart
            self.assertTrue(startup.first_start(path, "boot-b"))      # stale marker from another boot
            self.assertTrue(os.path.exists(path))

    def test_decide(self):
        self.assertTrue(startup.decide(True, None, None)[0])
        self.assertTrue(startup.decide(False, 30, None)[0])           # just logged in
        self.assertTrue(startup.decide(False, 9000, 20)[0])           # someone at the keyboard
        self.assertFalse(startup.decide(False, 9000, 600)[0])         # plain restart, nobody around
        self.assertFalse(startup.decide(False, None, None)[0])


@unittest.skipIf(daemon is None, "needs PyGObject")
class DaemonStartTest(unittest.TestCase):
    def make(self):
        d = object.__new__(daemon.Daemon)
        d.cfg = {"topology": {"keep_on_boot": True, "enabled": False}}
        d.managed = lambda: [{"name": "LG"}]
        d.logs = []
        d.log = d.logs.append
        d.each = mock.Mock()
        return d

    def test_restart_does_not_power_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"XDG_RUNTIME_DIR": tmp}
            check = lambda: startup.decide(startup.first_start(startup.marker_path(env), "b"), 9000, 600)
            d = self.make()
            d.power_on_at_start(check)                                 # login: TV on
            d.each.assert_called_once_with("on", [{"name": "LG"}])
            d = self.make()
            d.power_on_at_start(check)                                 # systemctl --user restart
            d.each.assert_not_called()
            self.assertIn("restarted", d.logs[-1])

    def test_check_failure_powers_on(self):
        d = self.make()
        d.power_on_at_start(mock.Mock(side_effect=OSError("x")))
        d.each.assert_called_once()


if __name__ == "__main__":
    unittest.main()
