"""Test mode: a flag file (optionally holding a PID) tells the daemon to keep the TV blank and hands-off."""
import os


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True            # exists, owned by someone else
    except OSError:
        return False
    return True


def flag_active(path, alive=pid_alive):
    """A flag is on if the file exists and, when its content is a number, that process is alive."""
    try:
        with open(path) as f:
            text = f.read(64).strip()
    except OSError:
        return os.path.exists(path)    # unreadable but present (or a directory): counts as on
    if text.isdigit():
        pid = int(text)
        return pid > 0 and alive(pid)
    return True


def active(paths, alive=pid_alive):
    return any(flag_active(p, alive) for p in paths or [])
