# Changelog

## Unreleased

- New test mode (`test_mode_flags`, a list of file paths, empty by default). While any listed file
  exists (and, if it holds a PID, that process is alive) the TV is kept blank but powered, input is
  not a wake signal, and idle and display-off power-offs are skipped. When the flag goes away the
  screen comes back with no power cycle.
- Keep the TV off after a Wake-on-LAN wake, and power it on at the first keyboard or mouse input.
  Wakes from the keyboard, mouse or power button work as before. New option "Keep TV off after a
  network wake" (`stay_off_on_network_wake`, on by default). The wake source comes from the
  kernel's wakeup counters, or from an optional file a machine-specific hook can write (see
  README, "Network wake").
- Turn the TV off reliably at sleep: a persistent connection to each TV (`persistent_connection`,
  on by default, like LGTV Companion's) follows the TV's power state, reconnects with backoff and
  keeps alive, and the power-off at sleep/shutdown goes out over it at once. If it is down, quick
  retries within logind's delay window, which the daemon now reads instead of assuming 5 s. New
  socket command `prepare_sleep` for hooks that run before the network goes down.
- If you turn the TV off at the TV or with its remote while the PC is on (also at the lock screen),
  it stays off until your next keyboard or mouse input; the daemon no longer turns it straight back
  on. Power-on requests are no longer queued up (one at a time per TV), and a running power-on
  stops sending Wake-on-LAN once the TV is turned off.
- Restarting the daemon no longer turns the TV on. It powers the TV on at start only at boot or
  login, or after keyboard or mouse input in the last two minutes.

## 0.1.0

First release.
