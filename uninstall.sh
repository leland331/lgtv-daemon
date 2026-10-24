#!/usr/bin/env bash
# Remove LGTV Daemon. Keeps ~/.config/lgtv-daemon unless --purge is given.
set -uo pipefail
command -v systemctl >/dev/null && systemctl --user disable --now lgtv-daemon.service 2>/dev/null
pkill -u "$(id -u)" -f "lgtvc --daemon" 2>/dev/null
rm -f "$HOME/.config/systemd/user/lgtv-daemon.service" "$HOME/.local/bin/lgtv-daemon" \
      "$HOME/.local/share/applications/io.github.lgtv_daemon.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/io.github.lgtv_daemon.svg" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/io.github.lgtv_daemon-awake.svg" \
      "$HOME/.config/autostart/lgtv-daemon.desktop"
rm -rf "$HOME/.local/share/lgtv-daemon"
command -v systemctl >/dev/null && systemctl --user daemon-reload
if [ -f /usr/lib/systemd/system-sleep/lgtv-daemon-wake ]; then
    sudo rm -f /usr/lib/systemd/system-sleep/lgtv-daemon-wake /run/lgtv-daemon-wake.json
fi
if [ -f /etc/ufw/applications.d/lgtv-daemon ]; then
    sudo ufw delete allow lgtv-daemon
    sudo rm -f /etc/ufw/applications.d/lgtv-daemon
fi
if [ -f /etc/firewalld/services/lgtv-daemon.xml ]; then
    sudo firewall-cmd --permanent --remove-service=lgtv-daemon
    sudo rm -f /etc/firewalld/services/lgtv-daemon.xml
    sudo firewall-cmd --reload
fi
[ "${1:-}" = "--purge" ] && rm -rf "$HOME/.config/lgtv-daemon" "$HOME/.local/state/lgtv-daemon"
echo "Uninstalled."
