"""WebOS SSAP client. Handshake/manifest and luna-via-alert technique follow LGTV Companion
(Common/lg_api.h, event.cpp), (c) JPersson77, MIT licence."""
import json
import time

from . import wol
from .ws import WebSocket, WSError

PERMISSIONS = ["LAUNCH", "LAUNCH_WEBAPP", "APP_TO_APP", "CLOSE", "TEST_OPEN", "TEST_PROTECTED", "CONTROL_AUDIO", "CONTROL_DISPLAY", "CONTROL_INPUT_JOYSTICK", "CONTROL_INPUT_MEDIA_RECORDING", "CONTROL_INPUT_MEDIA_PLAYBACK", "CONTROL_INPUT_TV", "CONTROL_POWER", "CONTROL_TV_SCREEN", "READ_APP_STATUS", "READ_CURRENT_CHANNEL", "READ_INPUT_DEVICE_LIST", "READ_NETWORK_STATE", "READ_RUNNING_APPS", "READ_TV_CHANNEL_LIST", "WRITE_NOTIFICATION_TOAST", "READ_POWER_STATE", "READ_COUNTRY_INFO", "READ_SETTINGS", "CONTROL_INPUT_TEXT", "CONTROL_MOUSE_AND_KEYBOARD", "WRITE_SETTINGS", "WRITE_NOTIFICATION_ALERT", "READ_INSTALLED_APPS", "READ_RUNNING_APPS", "READ_UPDATE_INFO"]

LUNA_SET_SYSTEM_SETTINGS = "luna://com.webos.settingsservice/setSystemSettings"
LUNA_SET_DEVICE_INFO = "luna://com.webos.service.eim/setDeviceInfo"


class TVError(Exception):
    pass


def register_payload(key=""):
    p = {"forcePairing": False, "pairingType": "PROMPT",
         "manifest": {"manifestVersion": 1, "permissions": PERMISSIONS}}
    if key:
        p["client-key"] = key
    return {"type": "register", "id": "register_0", "payload": p}


def luna_payload(uri, params):
    """Luna calls are smuggled through a createAlert whose close/fail action runs the luna URI."""
    req = {"uri": uri, "params": params}
    return {"message": " ", "buttons": [{"label": "", "onClick": uri, "params": params}],
            "onclose": req, "onfail": req}


class TV:
    def __init__(self, dev, timeout=5.0):
        self.dev = dev
        self.timeout = timeout
        self.ws = None
        self._id = 0

    def connect(self, pair_timeout=60.0, on_prompt=None):
        """Connect and register. Returns the client key (new or existing)."""
        d = self.dev
        url = "wss://%s:3001/" % d["ip"] if d.get("ssl", True) else "ws://%s:3000/" % d["ip"]
        try:
            self.ws = WebSocket(url, self.timeout)
        except (OSError, WSError) as e:
            raise TVError("cannot connect to %s: %s" % (d["ip"], e))
        self.ws.send(json.dumps(register_payload(d.get("key", ""))))
        end = time.time() + pair_timeout
        while time.time() < end:
            self.ws.settimeout(max(0.5, end - time.time()))
            try:
                msg = json.loads(self.ws.recv())
            except (OSError, WSError) as e:
                raise TVError("pairing failed: %s" % e)
            if msg.get("type") == "registered":
                d["key"] = msg["payload"]["client-key"]
                return d["key"]
            if msg.get("type") == "error":
                raise TVError("pairing rejected: %s" % msg.get("error"))
            if msg.get("payload", {}).get("pairingType") == "PROMPT" and on_prompt:
                on_prompt()
        raise TVError("pairing timed out - accept the prompt on the TV")

    def request(self, uri, payload=None, wait=True):
        if not uri.startswith(("ssap://", "luna://")):
            uri = "ssap://" + uri
        self._id += 1
        rid = "req_%d" % self._id
        msg = {"type": "request", "id": rid, "uri": uri}
        if payload is not None:
            msg["payload"] = payload
        self.ws.send(json.dumps(msg))
        if not wait:
            return None
        self.ws.settimeout(self.timeout)
        while True:
            r = json.loads(self.ws.recv())
            if r.get("id") == rid:
                if r.get("type") == "error":
                    pl = r.get("payload") or {}
                    return {"returnValue": False, "error": r.get("error"), "errorText": pl.get("errorText", "")}
                return r.get("payload", {})

    def luna(self, uri, params):
        return self.request("system.notifications/createAlert", luna_payload(uri, params))

    def set_settings(self, category, settings):
        return self.luna(LUNA_SET_SYSTEM_SETTINGS, {"category": category, "settings": settings})

    def button(self, name):
        r = self.request("com.webos.service.networkinput/getPointerInputSocket")
        path = r.get("socketPath")
        if not path:
            raise TVError("no pointer input socket")
        s = WebSocket(path, self.timeout)
        try:
            s.send("type:button\nname:%s\n\n" % name)
        finally:
            s.close()
        return {"returnValue": True}

    def power_state(self):
        try:
            return self.request("com.webos.service.tvpower/power/getPowerState").get("state", "")
        except (OSError, WSError):
            return ""

    def close(self):
        if self.ws:
            self.ws.close()
            self.ws = None


def connect(dev, timeout=5.0, pair_timeout=60.0, on_prompt=None):
    tv = TV(dev, timeout)
    tv.connect(pair_timeout, on_prompt)
    return tv


def power_on(dev, timeout=40, log=print, connect_fn=None, cancel=None):
    """WOL until the TV answers, then turn the screen on if it is only blanked; then optionally
    switch to the PC's HDMI input after the configured delay (LGTV Companion behaviour)."""
    connect_fn = connect_fn or (lambda d: connect(d, timeout=3, pair_timeout=10))
    end = time.time() + timeout
    while time.time() < end:
        if cancel and cancel():          # e.g. turned off at the TV meanwhile: stop sending wakes
            log("%s: power on cancelled" % dev["name"])
            return False
        wol.wake(dev)
        try:
            tv = connect_fn(dev)
        except TVError:
            time.sleep(1)
            continue
        try:
            st = tv.power_state()
            if st == "Screen Off":
                tv.request("com.webos.service.tvpower/power/turnOnScreen")
            elif st in ("Active Standby", "Suspend", "Power Off"):
                tv.close()
                time.sleep(1)
                continue
            log("%s: powered on (%s)" % (dev["name"], st or "on"))
            if dev.get("set_hdmi_input_on_power_on"):
                time.sleep(dev.get("set_hdmi_input_on_power_on_delay", 1))
                tv.request("system.launcher/launch", {"id": "com.webos.app.hdmi%d" % dev.get("source_hdmi_input", 1)})
                log("%s: set hdmi-input [%d]" % (dev["name"], dev.get("source_hdmi_input", 1)))
            return True
        finally:
            tv.close()
    log("%s: power on timed out" % dev["name"])
    return False


HDMI_APP = "com.webos.app.hdmi"


def off_decision(dev, state, app, blank, forced):
    """(act, reason) for power off / blank, as in LGTV Companion's web_os_client.cpp.

    app is the foreground appId, or None when not queried. Power off acts on 'Active' or
    'Screen Off'; blank only on 'Active'. With check_hdmi_input_when_power_off (and not forced),
    act only when the foreground app is the PC's HDMI input."""
    if blank and state != "Active":
        return False, "Power state is %s (screen is blanked or off)" % (state or "unknown")
    if not blank and state not in ("Active", "Screen Off"):
        return False, "Power state is OFF"
    verb = "Screen will be blanked" if blank else "Device will be turned off"
    neg = "Screen will not be blanked" if blank else "Device will not be turned off"
    if not dev.get("check_hdmi_input_when_power_off") or forced:
        return True, verb
    app = app or ""
    if app.startswith(HDMI_APP):
        n = app[len(HDMI_APP):]
        if n == str(dev.get("source_hdmi_input", 1)):
            return True, "HDMI input %s is active. %s" % (n, verb)
        return False, "HDMI input %s is active. %s" % (n, neg)
    return False, "Foreground app is %s. %s" % (app or "unknown", neg)


def _off_with(tv, dev, blank, log, forced):
    st = tv.power_state()
    app = None
    if dev.get("check_hdmi_input_when_power_off") and not forced and st in ("Active", "Screen Off"):
        app = tv.request("com.webos.applicationManager/getForegroundAppInfo").get("appId", "")
    act, why = off_decision(dev, st, app, blank, forced)
    log("%s: %s" % (dev["name"], why))
    if act:
        tv.request("com.webos.service.tvpower/power/turnOffScreen" if blank else "system/turnOff")
    return act


def power_off(dev, blank=False, log=print, forced=False, connect_fn=None):
    connect_fn = connect_fn or (lambda d: connect(d, timeout=3, pair_timeout=5))
    try:
        tv = connect_fn(dev)
    except TVError as e:
        log("%s: power off skipped: %s" % (dev["name"], e))
        return False
    try:
        return _off_with(tv, dev, blank, log, forced)
    finally:
        tv.close()


def power_off_until(dev, deadline, blank=False, log=print, connect_fn=None, tries=3):
    """Power off before a hard deadline (suspend/shutdown inhibitor window): short timeouts and
    a few quick retries instead of one long connect."""
    connect_fn = connect_fn or (lambda d, t: connect(d, timeout=t, pair_timeout=t))
    err = "no time left"
    for _ in range(tries):
        left = deadline - time.time()
        if left < 0.3:
            break
        t = min(1.5, left)
        try:
            tv = connect_fn(dev, t)
        except TVError as e:
            err = str(e)
            time.sleep(min(0.2, max(0, deadline - time.time() - 0.3)))
            continue
        try:
            tv.timeout = max(0.3, min(tv.timeout, deadline - time.time()))
            return _off_with(tv, dev, blank, log, False)
        except (OSError, WSError, ValueError) as e:
            err = str(e)
        finally:
            tv.close()
    log("%s: power off skipped: %s" % (dev["name"], err))
    return False


def screen_on(dev, log=print):
    try:
        tv = connect(dev, timeout=3, pair_timeout=5)
    except TVError:
        return power_on(dev, log=log)
    try:
        if tv.power_state() == "Screen Off":
            tv.request("com.webos.service.tvpower/power/turnOnScreen")
        log("%s: screen on" % dev["name"])
        return True
    finally:
        tv.close()
