#!/usr/bin/env bash
# Install LGTV Daemon for the current user.
# Distros: Arch (pacman), Debian/Ubuntu (apt), Fedora (dnf), openSUSE (zypper); others get a manual list.
# Flags: --dry-run (print the dependency command, change nothing), --deps-only (dependencies only).
set -uo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
SHARE="$HOME/.local/share/lgtv-daemon"
BIN="$HOME/.local/bin"
DRY=0; DEPS_ONLY=0
for a in "$@"; do case "$a" in --dry-run) DRY=1;; --deps-only) DEPS_ONLY=1;; esac; done
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
run() { if [ $DRY = 1 ]; then echo "would run: $*"; else "$@"; fi; }
has_systemd() { [ -d /run/systemd/system ] && command -v systemctl >/dev/null; }

# GTK4 + libadwaita (1.4+ for the settings app), PyGObject + typelibs, libXss/libXext (X11 idle/DPMS),
# iproute2. No websockets or libwayland packages: both are built in.
echo "== dependencies"
if command -v pacman >/dev/null; then
    run $SUDO pacman -S --needed --noconfirm python python-gobject gtk4 libadwaita libxss libxext iproute2
elif command -v apt-get >/dev/null; then
    run $SUDO apt-get install -y python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 libxss1 libxext6 iproute2
elif command -v dnf >/dev/null; then
    run $SUDO dnf install -y python3 python3-gobject gtk4 libadwaita libXScrnSaver libXext iproute
elif command -v zypper >/dev/null; then
    run $SUDO zypper --non-interactive install python3 python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 \
        libXss1 libXext6 iproute2
else
    echo "unknown package manager; install manually: python3 (3.9+), PyGObject, GTK 4 and libadwaita"
    echo "  typelibs (Gtk-4.0, Adw-1), libXss, libXext, iproute2 (ip)"
fi
[ $DRY = 1 ] || [ $DEPS_ONLY = 1 ] && exit 0

echo "== files"
mkdir -p "$SHARE" "$BIN" "$HOME/.local/share/applications" "$HOME/.config/autostart"
rm -rf "$SHARE/lgtvc"
cp -r "$SRC/lgtvc" "$SHARE/"
cat > "$BIN/lgtv-daemon" <<WRAP
#!/bin/sh
PYTHONPATH="$SHARE" exec python3 -m lgtvc "\$@"
WRAP
chmod +x "$BIN/lgtv-daemon"
sed "s|^Exec=lgtv-daemon|Exec=$BIN/lgtv-daemon|" "$SRC/packaging/io.github.lgtv_daemon.desktop" \
    > "$HOME/.local/share/applications/io.github.lgtv_daemon.desktop"
rm -f "$HOME/.local/share/applications/lgtv-daemon.desktop"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$ICONS" && cp "$SRC/packaging/io.github.lgtv_daemon.svg" "$SRC/packaging/io.github.lgtv_daemon-awake.svg" "$ICONS/"
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

echo "== firewall (SSDP scan replies to UDP 41900)"
if command -v ufw >/dev/null && $SUDO ufw status 2>/dev/null | grep -q "Status: active"; then
    $SUDO install -m 644 "$SRC/firewall/ufw-lgtv-daemon" /etc/ufw/applications.d/lgtv-daemon
    $SUDO ufw allow lgtv-daemon
elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    $SUDO install -m 644 "$SRC/firewall/firewalld-lgtv-daemon.xml" /etc/firewalld/services/lgtv-daemon.xml
    $SUDO firewall-cmd --reload
    $SUDO firewall-cmd --permanent --add-service=lgtv-daemon
    $SUDO firewall-cmd --reload
else
    echo "no active ufw or firewalld, nothing to do"
fi

echo "== wake-source hook (keeps the TV off after a Wake-on-LAN wake)"
# Runs as root around each suspend: reads which device sent the PCIe wake message (needs root).
SLEEP_HOOKS=/usr/lib/systemd/system-sleep
if has_systemd && [ -d "$SLEEP_HOOKS" ]; then
    $SUDO install -m 755 "$SRC/lgtvc/wake.py" "$SLEEP_HOOKS/lgtv-daemon-wake" \
        || echo "could not install the hook; network wakes will turn the TV on like any other wake"
else
    echo "no systemd sleep hooks directory; network wakes will turn the TV on like any other wake"
fi

echo "== service"
# Autostart works everywhere: with systemd it hands the session environment to the user unit (for
# desktops that never start graphical-session.target: i3, startx...), otherwise it runs the daemon.
if has_systemd; then
    mkdir -p "$HOME/.config/systemd/user"
    cp "$SRC/packaging/lgtv-daemon.service" "$HOME/.config/systemd/user/"
    START="sh -c 'systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XDG_SESSION_TYPE XDG_CURRENT_DESKTOP; systemctl --user start lgtv-daemon.service'"
    systemctl --user daemon-reload
    systemctl --user enable --now lgtv-daemon.service || echo "could not start the user service now; it starts at next login"
else
    echo "no systemd: the daemon starts from XDG autostart at login (shutdown/sleep handling needs logind/elogind)"
    START="$BIN/lgtv-daemon --daemon"
fi
printf '[Desktop Entry]\nType=Application\nName=LGTV Daemon\nExec=%s\nNoDisplay=true\nX-GNOME-Autostart-enabled=true\n' \
    "$START" > "$HOME/.config/autostart/lgtv-daemon.desktop"
echo "Installed. Open 'LGTV Daemon' from the app menu, or run: $BIN/lgtv-daemon --gui"
