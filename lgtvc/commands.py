"""Command-line syntax and command execution, mirroring LGTV Companion's Commandline.md.

Syntax: -command [args...] [Device1|Name ...] -command2 ...
A token beginning with '-' (and not a negative number) starts a new command."""
import json
import re

from . import config, webos
from .data import BUTTONS, COMMANDS

# commands handled by the daemon (session state), not by talking to a TV directly
DAEMON_COMMANDS = {"autoenable": 0, "autodisable": 0, "streaming_connect": 0, "streaming_disconnect": 0,
                   "clearlog": 0, "idle": 0, "unidle": 0}
OUTPUT_COMMANDS = {"od": 0, "of": 0, "ok": 1, "output": 1}
ARGC = {
    "poweron": 0, "poweroff": 0, "screenon": 0, "screenoff": 0,
    "sethdmi1": 0, "sethdmi2": 0, "sethdmi3": 0, "sethdmi4": 0, "sethdmi": 1,
    "set_input_type": 3, "mute": 0, "unmute": 0, "volume": 1,
    "button": 1, "button_nocheck": 1, "freesyncinfo": 0,
    "servicemenu": 0, "servicemenu_legacy_enable": 0, "servicemenu_legacy_disable": 0,
    "servicemenu_tpc_enable": 0, "servicemenu_tpc_disable": 0,
    "servicemenu_gsr_enable": 0, "servicemenu_gsr_disable": 0,
    "get_system_settings": 2, "request": 1, "request_with_param": 2,
    "start_app": 1, "start_app_with_param": 2, "close_app": 1,
    "settings_picture": 1, "settings_other": 1, "settings_options": 1,
    "set_curve_preset": 1, "adjust_curve_preset": 2, "set_curvature": 1,
}
ARGC.update(DAEMON_COMMANDS)
ARGC.update(OUTPUT_COMMANDS)
ARGC.update({k: 1 for k in COMMANDS})

_NUM = re.compile(r"^-\d+$")


class CommandError(Exception):
    pass


def tokenize(argv):
    """Split argv into [(command, [args], [devices])]."""
    out, cur = [], None
    for tok in argv:
        if tok.startswith("-") and len(tok) > 1 and not _NUM.match(tok):
            cur = [tok[1:].lower(), []]
            out.append(cur)
        elif cur is None:
            raise CommandError("expected a -command, got %r" % tok)
        else:
            cur[1].append(tok)
    res = []
    for cmd, words in out:
        if cmd not in ARGC:
            raise CommandError("invalid command: -%s" % cmd)
        n = ARGC[cmd]
        if cmd == "output" and words and words[0] == "key":
            n = 2
        if len(words) < n:
            raise CommandError("insufficient arguments for -%s (needs %d)" % (cmd, n))
        res.append((cmd, words[:n], words[n:]))
    return res


def _validate(cmd, arg):
    spec = COMMANDS[cmd]
    if "Argument" in spec:
        allowed = spec["Argument"].split()
        if arg not in allowed:
            raise CommandError("-%s: invalid argument %r (valid: %s)" % (cmd, arg, spec["Argument"]))
        return arg
    try:
        v = int(arg)
    except ValueError:
        raise CommandError("-%s: argument must be a number" % cmd)
    if not spec.get("Min", v) <= v <= spec.get("Max", v):
        raise CommandError("-%s: %d out of range %s..%s" % (cmd, v, spec.get("Min"), spec.get("Max")))
    return v


def _json(arg):
    try:
        return json.loads(arg)
    except ValueError:
        raise CommandError("invalid JSON: %s" % arg)


def plan(cmd, args):
    """Turn one TV command into a list of ('request'|'luna'|'settings'|'button'|'power', ...) steps."""
    if cmd in ("poweron", "poweroff", "screenon", "screenoff"):
        return [("power", cmd)]
    if cmd.startswith("sethdmi"):
        n = cmd[7:] or args[0][:1]
        if n not in "1234" or not n:
            raise CommandError("HDMI input must be 1-4")
        return [("request", "system.launcher/launch", {"id": "com.webos.app.hdmi" + n})]
    if cmd == "set_input_type":
        return [("luna", webos.LUNA_SET_DEVICE_INFO, {"id": args[0], "icon": args[1] + ".png", "label": args[2]})]
    if cmd == "mute":
        return [("request", "audio/setMute", {"mute": True})]
    if cmd == "unmute":
        return [("request", "audio/setMute", {"mute": False})]
    if cmd == "volume":
        v = int(args[0])
        if not 0 <= v <= 100:
            raise CommandError("volume must be 0-100")
        return [("request", "audio/setVolume", {"volume": v})]
    if cmd in ("button", "button_nocheck"):
        b = args[0].upper() if cmd == "button" else args[0]
        if cmd == "button" and b not in BUTTONS:
            raise CommandError("invalid button %r (see lgtv-daemon --buttons)" % args[0])
        return [("button", b)]
    if cmd == "freesyncinfo":
        return [("request", "system.launcher/launch", {"id": "com.webos.app.tvhotkey", "params": {"activateType": "freesync-info"}})]
    if cmd == "servicemenu":
        return [("request", "system.launcher/launch", {"id": "com.webos.app.factorywin", "params": {"irKey": "inStart"}})]
    if cmd.startswith("servicemenu_legacy"):
        return [("settings", "other", {"svcMenuFlag": cmd.endswith("disable")})]
    if cmd.startswith("servicemenu_tpc"):
        return [("luna", "luna://com.webos.service.oledepl/setTemporalPeakControl", {"enable": cmd.endswith("enable")})]
    if cmd.startswith("servicemenu_gsr"):
        return [("luna", "luna://com.webos.service.oledepl/setGlobalStressReduction", {"enable": cmd.endswith("enable")})]
    if cmd == "get_system_settings":
        return [("request", "settings/getSystemSettings", {"category": args[0], "keys": _json(args[1])})]
    if cmd == "request":
        return [("request", args[0], None)]
    if cmd == "request_with_param":
        return [("request", args[0], _json(args[1]))]
    if cmd == "start_app":
        return [("request", "system.launcher/launch", {"id": args[0]})]
    if cmd == "start_app_with_param":
        return [("request", "system.launcher/launch", {"id": args[0], "params": _json(args[1])})]
    if cmd == "close_app":
        return [("request", "system.launcher/close", {"id": args[0]})]
    if cmd.startswith("settings_"):
        return [("settings", cmd[9:], _json(args[0]))]
    if cmd == "set_curve_preset":
        p = "flat" if args[0].lower() in ("flat", "0") else "curvature" if args[0] in "123" else ""
        if not p:
            raise CommandError("-set_curve_preset: flat or 1-3")
        return [("luna", "luna://com.webos.service.rollingscreen/changeCurve", {"type": p, "reason": "com.pal.app.settings"})]
    if cmd == "adjust_curve_preset":
        if args[0] not in ("1", "2", "3"):
            raise CommandError("-adjust_curve_preset: preset 1-3")
        return [("luna", "luna://com.webos.service.rollingscreen/updateCurvature", {"type": "curvature", "value": args[1] + "%"})]
    if cmd == "set_curvature":
        if args[0].lower() in ("flat", "0"):
            return plan("set_curve_preset", ["flat"])
        return plan("adjust_curve_preset", ["2", args[0]]) + plan("adjust_curve_preset", ["3", args[0]]) + \
            plan("set_curve_preset", ["3"]) + plan("set_curve_preset", ["2"])
    if cmd in COMMANDS:
        spec = COMMANDS[cmd]
        val = _validate(cmd, args[0])
        if spec.get("ValFormat") == "int" or "Argument" not in spec:
            val = int(val)
        elif val in ("true", "false") and spec["Category"] == "network":
            val = val
        setting = spec["Setting"]
        m = re.match(r"(.*)_hdmi(\d)$", setting)
        if m:
            return [("settings", spec["Category"], {m.group(1): {"hdmi" + m.group(2): val}})]
        return [("settings", spec["Category"], {setting: val})]
    raise CommandError("not a device command: -%s" % cmd)


def run_steps(dev, steps, cfg=None, log=print):
    """Execute planned steps on one device; returns a list of responses."""
    cfg = cfg or config.normalize({})
    results = []
    tv = None
    try:
        for step in steps:
            kind = step[0]
            if kind == "power":
                what = step[1]
                if what == "poweron":
                    ok = webos.power_on(dev, cfg["power_on_timeout"], log)
                elif what == "poweroff":
                    ok = webos.power_off(dev, False, log, forced=True)
                elif what == "screenoff":
                    ok = webos.power_off(dev, True, log, forced=True)
                else:
                    ok = webos.screen_on(dev, log)
                results.append({"returnValue": bool(ok)})
                continue
            if tv is None:
                tv = webos.connect(dev, timeout=5, pair_timeout=5)
            if kind == "request":
                results.append(tv.request(step[1], step[2]))
            elif kind == "luna":
                results.append(tv.luna(step[1], step[2]))
            elif kind == "settings":
                results.append(tv.set_settings(step[1], step[2]))
            elif kind == "button":
                results.append(tv.button(step[1]))
    finally:
        if tv:
            tv.close()
    return results


def format_output(obj, mode="default", key=""):
    if mode == "friendly":
        return json.dumps(obj, indent=2)
    if mode == "key":
        found = []

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k == key:
                        found.append(v)
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(obj)
        return "\n".join(v if isinstance(v, str) else json.dumps(v) for v in found)
    return json.dumps(obj, separators=(",", ":"))
