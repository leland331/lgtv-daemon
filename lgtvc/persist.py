"""Persistent connection to a TV (LGTV Companion's PersistentConnectionLevel).

One background thread per managed TV keeps a registered websocket open while the TV answers,
subscribes to its power state (and foreground app when the HDMI-input check is on), and
reconnects with backoff. At suspend the daemon can then send the power-off at once, from the
cached state, instead of connecting and pairing while the system is already shutting the
network down."""
import json
import threading
import time

from . import webos
from .ws import WSError

BACKOFF = (1, 2, 4, 8, 15, 20)    # seconds between reconnect attempts; resets after a good connect
KEEPALIVE = 20                    # ping after this many quiet seconds; drop after two silent pings
SUB_POWER = "sub_power"
SUB_APP = "sub_app"


class Persistent:
    def __init__(self, dev, log=print, connect_fn=None):
        self.dev = dict(dev)
        self.log = log
        self.connect_fn = connect_fn or (lambda d: webos.connect(d, timeout=3, pair_timeout=5))
        self.tv = None
        self.state = ""            # cached power state from the subscription
        self.app = None            # cached foreground appId (only with the HDMI-input check)
        self.connected_at = 0.0
        self._lock = threading.Lock()      # guards tv, pending, _id
        self._send_lock = threading.Lock()
        self._pending = {}
        self._id = 0
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._thread = None

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="persist-" + self.dev["name"], daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._kick.set()
        self._drop()

    def kick(self):
        """Reconnect now (after resume or a power on) instead of waiting for the backoff."""
        self._kick.set()
        self._drop()

    def alive(self):
        return self.tv is not None and self.state != ""

    def same_device(self, dev):
        return all(self.dev.get(k) == dev.get(k) for k in ("ip", "ssl", "key", "check_hdmi_input_when_power_off"))

    def _run(self):
        fails = 0
        while not self._stop.is_set():
            try:
                tv = self.connect_fn(dict(self.dev))
            except Exception as e:
                if fails == 0:
                    self.log("%s: persistent connection down (%s), retrying" % (self.dev["name"], e))
                fails += 1
                self._wait(BACKOFF[min(fails, len(BACKOFF)) - 1])
                continue
            with self._lock:
                self.tv = tv
            self.connected_at = time.time()
            if fails or not getattr(self, "_logged_up", False):
                self.log("%s: persistent connection up" % self.dev["name"])
                self._logged_up = True
            fails = 0
            try:
                self._subscribe()
                self._read_loop(tv)
            except Exception as e:
                if not self._stop.is_set() and not self._kick.is_set():
                    self.log("%s: persistent connection lost (%s)" % (self.dev["name"], e))
                    fails = 1
            finally:
                self._drop()
            if self._kick.is_set():
                self._kick.clear()
            else:
                self._wait(BACKOFF[0])

    def _wait(self, secs):
        if self._kick.wait(secs):
            self._kick.clear()

    def _drop(self):
        with self._lock:
            tv, self.tv = self.tv, None
            pending, self._pending = self._pending, {}
        self.state = ""
        self.app = None
        for box in pending.values():
            box["ev"].set()
        if tv:
            try:
                tv.close()
            except Exception:
                pass

    # ---------------------------------------------------------------- traffic
    def _send(self, msg):
        tv = self.tv
        if tv is None:
            raise webos.TVError("not connected")
        with self._send_lock:
            tv.ws.send(json.dumps(msg))

    def _subscribe(self):
        self._send({"type": "subscribe", "id": SUB_POWER, "uri": "ssap://com.webos.service.tvpower/power/getPowerState"})
        if self.dev.get("check_hdmi_input_when_power_off"):
            self._send({"type": "subscribe", "id": SUB_APP,
                        "uri": "ssap://com.webos.applicationManager/getForegroundAppInfo"})

    def _read_loop(self, tv):
        tv.ws.settimeout(KEEPALIVE)
        while not self._stop.is_set():
            try:
                raw = tv.ws.recv()
            except TimeoutError:
                if time.time() - tv.ws.last_rx > 2.5 * KEEPALIVE:
                    raise webos.TVError("no answer to keepalive")
                with self._send_lock:
                    tv.ws.ping()
                continue
            self.handle(json.loads(raw))

    def handle(self, msg):
        """One message from the TV: subscription update or a reply to request()."""
        rid = msg.get("id")
        pl = msg.get("payload") or {}
        if rid == SUB_POWER:
            if "state" in pl:
                self.state = pl["state"]
        elif rid == SUB_APP:
            if "appId" in pl:
                self.app = pl["appId"]
        else:
            with self._lock:
                box = self._pending.pop(rid, None)
            if box is not None:
                box["msg"] = msg
                box["ev"].set()

    def request(self, uri, payload=None, wait=True, timeout=5.0):
        if not uri.startswith(("ssap://", "luna://")):
            uri = "ssap://" + uri
        with self._lock:
            self._id += 1
            rid = "p_%d" % self._id
            box = {"ev": threading.Event(), "msg": None}
            if wait:
                self._pending[rid] = box
        msg = {"type": "request", "id": rid, "uri": uri}
        if payload is not None:
            msg["payload"] = payload
        try:
            self._send(msg)
        except (OSError, WSError) as e:
            self.kick()
            raise webos.TVError("send failed: %s" % e)
        if not wait:
            return None
        if not box["ev"].wait(timeout) or box["msg"] is None:
            with self._lock:
                self._pending.pop(rid, None)
            raise webos.TVError("no reply from the TV")
        r = box["msg"]
        if r.get("type") == "error":
            pl = r.get("payload") or {}
            return {"returnValue": False, "error": r.get("error"), "errorText": pl.get("errorText", "")}
        return r.get("payload", {})

    def power_off_now(self, blank=False, forced=False, log=print):
        """Power off / blank from the cached state; sends without waiting for a reply.
        Returns True (sent), False (decided not to act) or None (not connected)."""
        if not self.alive():
            return None
        act, why = webos.off_decision(self.dev, self.state, self.app, blank, forced)
        if act:
            try:
                self.request("com.webos.service.tvpower/power/turnOffScreen" if blank else "system/turnOff", wait=False)
            except webos.TVError as e:
                log("%s: power off over the persistent connection failed: %s" % (self.dev["name"], e))
                return None
        log("%s: %s (persistent connection)" % (self.dev["name"], why))
        return act
