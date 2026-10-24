# Changelog

## Unreleased

- Keep the TV off after a Wake-on-LAN wake, and power it on at the first keyboard or mouse input.
  Wakes from the keyboard, mouse or power button work as before. New option "Keep TV off after a
  network wake" (`stay_off_on_network_wake`, on by default) and a root system-sleep hook that
  records which device woke the PC.
- Turn the TV off reliably at sleep. NetworkManager takes the network down within milliseconds of
  sleep starting, so connecting then often failed ("pairing failed: The read operation timed
  out"). New persistent connection to each TV (`persistent_connection`, on by default, like LGTV
  Companion's): it follows the TV's power state, reconnects with backoff and keeps alive, and the
  power-off at sleep/shutdown goes out over it at once. If it is down, quick retries within logind's
  delay window. A NetworkManager pre-down hook asks the daemon to turn the TV off before the
  network goes down, and `install.sh` raises logind's `InhibitDelayMaxSec` to 15 s (after the next
  reboot) unless it is set elsewhere.

## 0.1.0

First release.
