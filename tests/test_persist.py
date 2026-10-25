import json
import struct
import threading
import time
import unittest

from lgtvc import config, persist, webos
from lgtvc.ws import WebSocket


def frame(text):
    data = text.encode()
    return struct.pack("!BB", 0x81, len(data)) + data


class FakeSock:
    """Hands out chunks; None means 'raise a timeout here'."""
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = []
        self.delay = 0

    def recv(self, n):
        c = self.chunks.pop(0)
        if c is None:
            time.sleep(self.delay)
            raise TimeoutError("timed out")
        return c

    def sendall(self, b):
        self.sent.append(b)

    def settimeout(self, t):
        pass

    def close(self):
        pass


def fake_ws(chunks):
    ws = object.__new__(WebSocket)
    ws.sock, ws._buf, ws._taken, ws.last_rx = FakeSock(chunks), b"", [], 0
    return ws


def sent_json(ws):
    """Decode the masked text frames the client sent."""
    out = []
    for b in ws.sock.sent:
        if b[0] != 0x81:
            continue
        n, i = b[1] & 0x7F, 2
        if n == 126:
            n, i = struct.unpack("!H", b[2:4])[0], 4
        mask, data = b[i:i + 4], b[i + 4:i + 4 + n]
        out.append(json.loads(bytes(x ^ mask[k % 4] for k, x in enumerate(data))))
    return out


class FakeTV:
    def __init__(self, ws=None):
        self.ws = ws or fake_ws([])
        self.timeout = 3

    def close(self):
        pass


DEV = config.normalize({"devices": [{"name": "LG", "ip": "192.168.50.2", "key": "k"}]})["devices"][0]


class WebSocketTimeoutTest(unittest.TestCase):
    def test_timeout_mid_frame_keeps_stream(self):
        f = frame('{"a": 1}')
        ws = fake_ws([f[:1], None, f[1:5], None, f[5:]])
        with self.assertRaises(TimeoutError):
            ws.recv()
        with self.assertRaises(TimeoutError):
            ws.recv()
        self.assertEqual(json.loads(ws.recv()), {"a": 1})

    def test_ping_frame(self):
        ws = fake_ws([])
        ws.ping()
        self.assertEqual(ws.sock.sent[0][:2], b"\x89\x80")


class PersistentTest(unittest.TestCase):
    def make(self, state="Active", dev=DEV):
        p = persist.Persistent(dev, log=lambda m: None)
        p.tv = FakeTV()
        p.handle({"type": "response", "id": persist.SUB_POWER, "payload": {"state": state, "returnValue": True}})
        return p

    def test_caches_subscriptions(self):
        p = self.make("Screen Off")
        self.assertTrue(p.alive())
        self.assertEqual(p.state, "Screen Off")
        p.handle({"type": "response", "id": persist.SUB_APP, "payload": {"appId": "com.webos.app.hdmi2"}})
        self.assertEqual(p.app, "com.webos.app.hdmi2")

    def test_power_off_now_sends_turn_off(self):
        p = self.make("Active")
        self.assertTrue(p.power_off_now(log=lambda m: None))
        self.assertEqual(sent_json(p.tv.ws)[-1]["uri"], "ssap://system/turnOff")

    def test_power_off_now_blank(self):
        p = self.make("Active")
        p.power_off_now(blank=True, log=lambda m: None)
        self.assertTrue(sent_json(p.tv.ws)[-1]["uri"].endswith("turnOffScreen"))

    def test_power_off_now_skips_standby(self):
        p = self.make("Active Standby")
        self.assertFalse(p.power_off_now(log=lambda m: None))
        self.assertEqual(sent_json(p.tv.ws), [])

    def test_power_off_now_hdmi_check(self):
        dev = dict(DEV, check_hdmi_input_when_power_off=True, source_hdmi_input=1)
        p = self.make("Active", dev)
        p.handle({"type": "response", "id": persist.SUB_APP, "payload": {"appId": "netflix"}})
        self.assertFalse(p.power_off_now(log=lambda m: None))

    def test_not_connected(self):
        p = persist.Persistent(DEV, log=lambda m: None)
        self.assertIsNone(p.power_off_now(log=lambda m: None))

    def test_request_reply_routing(self):
        p = self.make()
        res = {}
        t = threading.Thread(target=lambda: res.update(r=p.request("audio/getVolume", timeout=2)))
        t.start()
        for _ in range(100):
            if p._pending:
                break
            time.sleep(0.01)
        rid = next(iter(p._pending))
        p.handle({"type": "response", "id": rid, "payload": {"volume": 7}})
        t.join(2)
        self.assertEqual(res["r"], {"volume": 7})

    def test_reconnects_after_failure(self):
        calls = []
        tv = FakeTV(fake_ws([frame(json.dumps({"type": "response", "id": persist.SUB_POWER,
                                                "payload": {"state": "Active"}}))] + [None] * 100))
        tv.ws.sock.delay = 0.05

        def connect(d):
            calls.append(1)
            if len(calls) == 1:
                raise webos.TVError("cannot connect")
            return tv
        old = persist.BACKOFF
        persist.BACKOFF = (0.01,)
        try:
            p = persist.Persistent(DEV, log=lambda m: None, connect_fn=connect).start()
            for _ in range(200):
                if p.alive():
                    break
                time.sleep(0.01)
            self.assertTrue(p.alive())
            self.assertEqual(len(calls), 2)
            p.stop()
        finally:
            persist.BACKOFF = old


class PowerOffUntilTest(unittest.TestCase):
    def test_retries_then_succeeds(self):
        tries = []

        class TV(FakeTV):
            def power_state(self):
                return "Active"

            def request(self, uri, payload=None):
                tries.append(uri)
                return {}

        def connect(d, t):
            self.assertLessEqual(t, 1.5)
            if len(tries) < 2:
                tries.append("fail")
                raise webos.TVError("timed out")
            return TV()
        ok = webos.power_off_until(DEV, time.time() + 5, log=lambda m: None, connect_fn=connect)
        self.assertTrue(ok)
        self.assertEqual(tries[-1], "system/turnOff")

    def test_gives_up_at_deadline(self):
        def connect(d, t):
            time.sleep(t)
            raise webos.TVError("timed out")
        t0 = time.time()
        self.assertFalse(webos.power_off_until(DEV, t0 + 1.0, log=lambda m: None, connect_fn=connect))
        self.assertLess(time.time() - t0, 1.3)


if __name__ == "__main__":
    unittest.main()
