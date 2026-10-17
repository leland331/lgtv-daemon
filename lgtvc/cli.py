"""lgtv-daemon command line (LGTV Companion / LGTVcli compatible syntax)."""
import json
import os
import shlex
import socket
import sys

from . import __version__, commands, config, ssdp, webos

USAGE = """usage: lgtv-daemon -command [args] [Device1|Name ...] [-command ...]
       lgtv-daemon --scan | --add-scanned | --pair DEVICE | --list | --buttons | --commands
       lgtv-daemon --daemon | --gui | --version
See README.md for the full command list (same as LGTV Companion's Commandline.md)."""


def daemon_send(line, timeout=60):
    from .daemon import SOCKET_PATH
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(SOCKET_PATH)
    s.sendall((line + "\n").encode())
    data = b""
    while not data.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    return json.loads(data or b"null")


def pair(name, cfg, out=print):
    devs = config.find_devices(cfg, [name])
    if not devs:
        out("no such device: %s" % name)
        return 1
    d = devs[0]
    d["key"] = ""
    try:
        tv = webos.connect(d, pair_timeout=60, on_prompt=lambda: out("Accept the pairing prompt on the TV."))
        d["model"] = tv.request("system/getSystemInfo").get("modelName", "") or d["model"]
        tv.close()
    except webos.TVError as e:
        out(str(e))
        return 1
    config.save(cfg)
    out("paired %s" % d["name"])
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "-help"):
        print(USAGE)
        return 0
    cfg = config.load()
    a = argv[0]
    if a == "--version":
        print(__version__)
        return 0
    if a == "--daemon":
        from . import daemon
        daemon.main()
        return 0
    if a == "--gui":
        from . import gui
        return gui.main()
    if a == "--buttons":
        print(" ".join(commands.BUTTONS))
        return 0
    if a == "--commands":
        print(" ".join("-" + c for c in sorted(commands.ARGC)))
        return 0
    if a == "--list":
        for i, d in enumerate(cfg["devices"]):
            print("Device%d\t%s\t%s\t%s\t%s\t%s" % (i + 1, d["name"], d["ip"], ",".join(d["macs"]),
                  "managed" if d["enabled"] else "unmanaged", "paired" if d["key"] else "not paired"))
        return 0
    if a in ("--scan", "--add-scanned"):
        found = ssdp.discover()
        known = {d["ip"] for d in cfg["devices"]}
        for d in found:
            print("%s\t%s\t%s\t%s" % (d["ip"], d.get("mac", ""), d["name"], d["model"]))
            if a == "--add-scanned" and d["ip"] not in known:
                cfg["devices"].append({"name": d["name"], "ip": d["ip"], "model": d["model"],
                                       "macs": [d["mac"]] if d.get("mac") else []})
        if a == "--add-scanned":
            config.save(cfg)
        return 0
    if a == "--pair":
        return pair(argv[1] if len(argv) > 1 else "Device1", cfg)

    try:
        parsed = commands.tokenize(argv)
    except commands.CommandError as e:
        print(json.dumps({"error": str(e)}))
        return 2
    mode, key, rc = "default", "", 0
    for cmd, args, devsel in parsed:
        if cmd == "od":
            mode = "default"
        elif cmd == "of":
            mode = "friendly"
        elif cmd == "ok":
            mode, key = "key", args[0]
        elif cmd == "output":
            mode = args[0]
            key = args[1] if len(args) > 1 else ""
        elif cmd in commands.DAEMON_COMMANDS:
            try:
                res = daemon_send(shlex.join(["-" + cmd] + devsel))
            except OSError:
                res = {"error": "the lgtv-daemon daemon is not running (systemctl --user start lgtv-daemon)"}
                rc = 1
            print(commands.format_output(res, mode, key))
        else:
            devs = config.find_devices(cfg, devsel)
            if not devs:
                print(json.dumps({"error": "no matching device(s)"}))
                rc = 1
                continue
            try:
                steps = commands.plan(cmd, args)
            except commands.CommandError as e:
                print(json.dumps({"error": str(e)}))
                rc = 2
                continue
            for d in devs:
                try:
                    res = commands.run_steps(d, steps, cfg, log=lambda m: print(m, file=sys.stderr))
                    res = res[0] if len(res) == 1 else res
                except (webos.TVError, OSError) as e:
                    res, rc = {"error": str(e)}, 1
                print(commands.format_output(res if mode == "key" else {d["name"]: res}, mode, key))
    return rc


if __name__ == "__main__":
    sys.exit(main())
