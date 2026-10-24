# LGTV Daemon

Makes an LG webOS TV behave like a PC monitor on Linux. It turns the TV off when the PC shuts
down or sleeps, wakes it again on resume, and can blank the screen when you step away.

This is a Linux port of [LGTV Companion](https://github.com/JPersson77/LGTVCompanion) by Jörgen
Persson. It aims to match its features and settings, and the command line is compatible with it.

## Features

- TV turns off at shutdown and before sleep, and back on at boot and after resume. Reboots are
  left alone.
- Wake-on-LAN aware: if the PC was woken over the network (for example a remote job), the TV
  stays off until you touch the keyboard or mouse. Woken by keyboard, mouse or power button, it
  turns on as usual. On by default ("Keep TV off after a network wake"; config key
  `stay_off_on_network_wake`). See [Network wake](#network-wake).
- Idle blanking: blank the screen after a set time without input and bring it back on the next
  input. Paused while video is playing or while chosen programs run.
- Follows your display setup: if the TV's output is disabled or unplugged, the TV turns off.
- Only acts while the TV is showing the PC's HDMI input, so it won't switch off Netflix or
  another device. Can also switch to the PC's input when powering on.
- Tray icon. Left click opens settings, middle click toggles Keep Screen On (blocks screen off
  and locking), right click has TV on/off and blank.
- Full command line and a local socket for scripts.
- Works on Wayland and X11.

## Install

Download a release, or clone the repo, then run:

```
./install.sh
```

The installer pulls in Python 3, PyGObject, GTK 4 and libadwaita with your package manager
(pacman, apt, dnf or zypper) and installs the app for your user. If ufw or firewalld is active it
opens UDP port 41900 for network scan replies. Use `./install.sh --dry-run` to see what it would
install first.

To remove it: `./uninstall.sh` (add `--purge` to also delete settings).

## Setup

1. On the TV, turn on *Turn on via Wi-Fi* (called *TV On with Mobile* on some models) so it can
   be woken over the network.
2. Open **LGTV Daemon** and press **Scan** (or **Add** to enter the TV's IP yourself).
3. Press **Configure > Pair** and accept the prompt on the TV.
4. Press **Test**. The TV should turn off and back on.

Giving the TV a fixed IP address in your router is recommended.

## Command line

Same syntax as LGTV Companion: `-command [args] [Device1|"Name"]`. With no device given, the
command goes to all TVs.

```
lgtv-daemon -poweron
lgtv-daemon -screenoff
lgtv-daemon -sethdmi 2 Device1
lgtv-daemon -backlight 40
lgtv-daemon -button HOME
lgtv-daemon -idle
lgtv-daemon --commands     # list every command
```

### Sunshine

To turn the TV on and off around streaming sessions, enable *Remote streaming host support* and
add `lgtv-daemon -streaming_connect` / `lgtv-daemon -streaming_disconnect` as Sunshine's
prep commands.

### Scripting

The daemon listens on `$XDG_RUNTIME_DIR/lgtv-daemon.sock`. Send it one command per line and it
replies with JSON. With *External API* enabled it also sends events such as `SYSTEM_SUSPEND`
and `SYSTEM_USER_IDLE`.

```
printf -- '-backlight 30\n' | socat - UNIX-CONNECT:$XDG_RUNTIME_DIR/lgtv-daemon.sock
```

## Compatibility

Tested on KDE Plasma 6 (Wayland). GNOME, wlroots compositors (Sway, Hyprland and others),
COSMIC and X11 desktops are supported but less tested. On GNOME the tray icon needs the
AppIndicator extension. Anything a desktop can't support (for example idle detection) is shown
as unavailable in the settings rather than failing.

Settings are stored in `~/.config/lgtv-daemon/config.json`, the log in
`~/.local/state/lgtv-daemon/log.txt`.

## Network wake

To tell a network wake from any other, `install.sh` adds a small root hook,
`/usr/lib/systemd/system-sleep/lgtv-daemon-wake` (it is `lgtvc/wake.py`). Before suspend it clears
the PCIe root ports' "PME Status"; after resume it records which device sent the wake message
in `/run/lgtv-daemon-wake.json`. If that device is a network card, the daemon skips the resume
power-on and waits for the first keyboard or mouse input. Where the kernel itself counts the
wake (the network card's `power/wakeup_count` goes up), that is used too. The log says which
path was taken (`wake source: network (...)` or `wake source: other (...)`).

Anything unclear counts as an ordinary wake, so without the hook, or on hardware where neither
signal is available, the TV turns on at resume as before. Wake-on-LAN itself must be enabled on
the network card (`ethtool -s <interface> wol g`) and in the firmware; this feature only decides
what the TV does afterwards.

## Development

The code is in `lgtvc/`. Run the tests with `python3 -m unittest discover -s tests`, and
re-run `./install.sh` to try changes.

To release, bump the version in `lgtvc/__init__.py`, add a `CHANGELOG.md` entry, then tag and
push (`git tag v0.1.0 && git push origin v0.1.0`). GitHub Actions builds the tarball and
publishes the release.

## License

MIT. Based on LGTV Companion by Jörgen Persson (also MIT); the command tables and pairing
manifest come from it.
