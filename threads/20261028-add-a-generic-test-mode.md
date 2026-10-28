# Thread 20261028-add-a-generic-test-mode

- Goal: Add a generic 'test mode' to lgtv-daemon (public repo: keep it general, no homelab paths or names in code, docs or defaults).
- Project: lgtv-daemon, branch `thread/20261028-add-a-generic-test-mode`
- Machine: tower
- Agent: claude:sonnet
- Started: 2026-10-28 00:01 CST (by `lab thread`)
- Status: running

## Brief

Add a generic 'test mode' to lgtv-daemon (public repo: keep it general, no homelab paths or names in code, docs or defaults).
Config key test_mode_flags: list of file paths (default empty). Test mode is on while any listed file exists and, if its content is a number, that PID is alive (stale PID file = off). While on: (1) blank the panel with the LG screen-off command (same call blank_instead_of_poweroff uses) and keep it blank (re-blank if the TV un-blanks), TV stays powered, never power-cycle; (2) ignore all input as a wake signal; (3) skip idle power-off and the SYSTEM_DISPLAYS_OFF / DPMS power-off; (4) when it turns off, restore normal behaviour (un-blank the screen, resume normal handling) with no power cycle. Poll the flags cheaply (every few seconds) or use inotify. Log transitions.
Also: search the daemon for anything that could start a KDE/logind lock screen (lock-session, ScreenSaver Lock, loginctl) and report; it must not lock.
Unit tests for: flag absent, file flag, live PID, stale PID, input ignored while on, displays-off ignored while on, restore on off. Update README + CHANGELOG. Run the repo's tests. Draft PR, never merge.
Report in the handoff: the config key and an example.

## Where it stands

(The thread keeps this current: what's done, commit SHAs, next steps, blockers.)
