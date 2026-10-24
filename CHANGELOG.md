# Changelog

## Unreleased

- Keep the TV off after a Wake-on-LAN wake, and power it on at the first keyboard or mouse input.
  Wakes from the keyboard, mouse or power button work as before. New option "Keep TV off after a
  network wake" (`stay_off_on_network_wake`, on by default) and a root system-sleep hook that
  records which device woke the PC.

## 0.1.0

First release.
