#!/usr/bin/env python3
# VERSION v20261013.1537   (vYYYYMMDD.HHMM, UK local time of the last edit; bump on every change)
"""
harness.py -- pieces shared by every experimenter (E1-E8), kept OUT of platform_env.py so that the hardware-verified
board-control library does not change when an experimenter does.

Contents (all standard library, Python >= 3.9 syntax because DUT-Mac runs 3.9.6):
  invoking_user_home() / invoking_ids()   the user who ran sudo (root's $HOME is never used for output)
  chown_to_user(path)                     hand files back to that user (the programs run as root)
  ignore_sighup()                         a dropped SSH connection must not kill a long batch
  fmt_duration(seconds)                   0d1h12m
  find_tool(name, extra_dirs)             locate a program even though sudo resets PATH
  openssl_runtime_env(openssl_path)       environment for a source-built OpenSSL (see below)
  SessionLog                              timestamped log: printed AND written to a file; collects errors
  StatusFile                              the one-line STATUS file (decision 8, 2026-10-11):
        RUNNING.   33/85. Started: 2026-10-11 11:40. Running for: 0d1h12m. Updated: 12:52. 0 errors.
        COMPLETED. 85/85. Started: 2026-10-11 11:40. Ran for: 1d3h38m. NO ERRORS.
        COMPLETED. 85/85. Started: ... Ran for: ... 2 ERRORS - see log /path
        ABORTED.   41/85. Started: ... Ran for: ... <reason> - see log /path
    A RUNNING line whose Updated time is old means the process died or hung.

Nothing here is called inside a measured window.
"""
import datetime
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

HARNESS_VERSION = "v20261013.1537"


# ------------------------------------------------------------------ the invoking user
def invoking_ids():
    """(uid, gid) of the user who ran sudo, or None when not run through sudo."""
    try:
        return int(os.environ["SUDO_UID"]), int(os.environ.get("SUDO_GID", os.environ["SUDO_UID"]))
    except (KeyError, ValueError):
        return None


def invoking_user_home():
    """Home directory of the user who ran sudo (not root's), falling back to the current $HOME."""
    name = os.environ.get("SUDO_USER")
    if name and name != "root":
        try:
            import pwd
            return pwd.getpwnam(name).pw_dir
        except (ImportError, KeyError):
            pass
    return os.path.expanduser("~")


def chown_to_user(path):
    """Give `path` (file or directory tree) to the invoking user. No-op when not run through sudo."""
    ids = invoking_ids()
    if ids is None or not os.path.exists(path):
        return
    uid, gid = ids
    try:
        os.chown(path, uid, gid)
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                for n in dirs + files:
                    try:
                        os.chown(os.path.join(root, n), uid, gid)
                    except OSError:
                        pass
    except OSError:
        pass


def ignore_sighup():
    """Survive the SSH connection dropping (SIGHUP). SIGINT/SIGTERM still run the library's teardown."""
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    except (AttributeError, ValueError):
        pass


# ------------------------------------------------------------------ finding programs and the built OpenSSL
def find_tool(name, extra_dirs=("/usr/local/bin", "/opt/homebrew/bin", "/usr/local/ssl/bin", "/usr/bin", "/bin")):
    """`sudo` resets PATH (secure_path on Linux, and Homebrew's /opt/homebrew/bin is not on root's PATH on the Mac),
    so look on PATH first and then in the usual places."""
    import shutil
    p = shutil.which(name)
    if p:
        return p
    for d in extra_dirs:
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def openssl_runtime_env(openssl_path):
    """The testbed's OpenSSL 3.5.8 is built into /usr/local/ssl and found at run time through LD_LIBRARY_PATH set in
    /etc/profile.d (runbooks: lib on the ARM/RISC-V boards, lib64 on DUT-PC). `sudo` resets the environment, so
    without this the binary silently loads the system libssl 3.0 and fails with 'version OPENSSL_3.5.0 not found'.
    Returns a copy of os.environ with the library directories next to the binary's prefix prepended."""
    env = dict(os.environ)
    if openssl_path:
        prefix = os.path.dirname(os.path.dirname(os.path.realpath(openssl_path)))
        libs = [d for d in (os.path.join(prefix, "lib64"), os.path.join(prefix, "lib")) if os.path.isdir(d)]
        if libs:
            env["LD_LIBRARY_PATH"] = ":".join(libs + ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else []))
    return env


# ------------------------------------------------------------------ time formatting
def fmt_duration(seconds):
    s = int(max(0, seconds))
    return f"{s // 86400}d{(s % 86400) // 3600}h{(s % 3600) // 60:02d}m"


def _now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


# ------------------------------------------------------------------ network checks and operator pauses
def host_reachable(ip, timeout=2):
    """One ICMP echo (the testbed's JUMP is checked this way). True/False."""
    try:
        return subprocess.run(["ping", "-c", "1", "-W", str(int(timeout)), ip], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=timeout + 3).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def internet_reachable(timeout=3):
    """True if a TCP connection to a well-known public address works (no DNS needed, so a missing resolver is not
    mistaken for 'offline'). Two independent addresses so one provider being down is not mistaken for offline."""
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            with socket.create_connection((host, 443), timeout=timeout):
                return True
        except OSError:
            pass
    return False


def operator_wait(message, flag_dir, log, on_wait=None, check=None, poll_s=1.0, short=False, ok_msg=None, say=print, override_after=None):
    """Pause until the operator presses Enter on the terminal OR creates <flag_dir>/CONTINUE (the second route works
    when the SSH session has died). Then run check() -> (ok, why); if not ok, say why and wait again.
    short=True: one line, "<message> [y/n]: ", continue only on y / yes (or the flag file); after a passing check
    ok_msg (if given) is shown. say(text) prints a line to the operator.
    override_after=N (short mode): after N failed checks the operator may answer `go` to carry on anyway; returns True
    in that case (the caller records it), else None."""
    fails, override = 0, False
    flag = os.path.join(flag_dir, "CONTINUE")
    use_stdin = sys.stdin is not None and sys.stdin.isatty()
    while True:
        try:
            os.remove(flag)
        except OSError:
            pass
        log("WAITING FOR OPERATOR: " + message)
        if short:
            print(f"{message} [y/n]: ", end="", flush=True)
        else:
            print(f"   >>> {message}\n   >>> Press Enter to continue (or, from another session: touch {flag})", flush=True)
        if on_wait:
            on_wait(message)
        while True:
            if os.path.exists(flag):
                if short:
                    print("(continued from another session)", flush=True)
                break
            if use_stdin:
                try:
                    r, _, _ = select.select([sys.stdin], [], [], poll_s)
                    if r:
                        line = sys.stdin.readline()
                        if line == "":                       # EOF: the terminal has gone; only the flag file works now
                            use_stdin = False
                            continue
                        if short and override_after and fails >= override_after and line.strip().lower() == "go":
                            override = True
                            break
                        if short and line.strip().lower() not in ("y", "yes"):
                            print(f"waiting - answer y when done: {message} [y/n]: ", end="", flush=True)
                            continue
                        break
                except (OSError, ValueError):
                    use_stdin = False
            else:
                time.sleep(poll_s)
        try:
            os.remove(flag)
        except OSError:
            pass
        if override:
            log("operator chose to go ahead although the check failed")
            say("  carrying on anyway; runs will carry a flag")
            return True
        if check is None:
            return
        ok, why = check()
        if ok:
            if short and ok_msg:
                say(ok_msg)
            return
        fails += 1
        log("check failed: " + why + " -- put it right and continue again")
        if short:
            more = " (or answer go to carry on anyway)" if override_after and fails >= override_after else ""
            say("  check failed: " + why + " -- put it right, then answer y again" + more)


# ------------------------------------------------------------------ session log
class SessionLog:
    """log(msg): timestamped line to the screen and the log file. error(msg): same, and remembered for the
    end-of-session summary (agreed decision 4d: every caught error is printed at the end)."""

    # Per-second polling lines: always written to the file, shown on screen only with verbose=True.
    # Anything that signals a problem (TIMEOUT, ABORT, no sensor) is always shown.
    QUIET_PREFIXES = ("gate: ", "T_ref wait: ")
    LOUD_WORDS = ("TIMEOUT", "ABORT", "no temperature sensor")

    def __init__(self, path, verbose=True):
        self.path = path
        self.verbose = verbose
        self.hide = None            # callable(msg) -> True: write to the file only (screen shows it only with verbose)
        self.render_quiet = None    # callable(msg) -> text for the self-overwriting status line, or None
        self._inline = False
        self._lock = threading.Lock()
        self.errors = []
        self._f = open(path, "a", buffering=1)

    def log(self, msg):
        line = f"{datetime.datetime.now().strftime('%H:%M:%S')} {msg}"
        loud = any(w in msg for w in self.LOUD_WORDS)
        quiet = not self.verbose and msg.startswith(self.QUIET_PREFIXES) and not loud
        hidden = (not self.verbose and not quiet and not loud and self.hide is not None and bool(self.hide(msg)))
        with self._lock:
            if quiet:
                # one self-overwriting progress line on a terminal; nothing at all when output is piped
                if sys.stdout.isatty():
                    text = None
                    if self.render_quiet is not None:
                        try:
                            text = self.render_quiet(msg)
                        except Exception:
                            text = None
                    width = max(20, shutil.get_terminal_size((100, 24)).columns - 1)
                    print("\r\x1b[K" + (text or line)[:width], end="", flush=True)
                    self._inline = True
            elif not hidden:
                self._wipe()
                print(line, flush=True)
            try:
                self._f.write(line + "\n")
            except (OSError, ValueError):
                pass

    def _wipe(self):
        if self._inline:
            print("\r\x1b[K", end="", flush=True)
            self._inline = False

    def clear_inline(self):
        with self._lock:
            self._wipe()

    def screen(self, msg, file=False):
        """A permanent line for the operator (no timestamp). Written to the log file too if file=True."""
        with self._lock:
            self._wipe()
            print(msg, flush=True)
            if file:
                try:
                    self._f.write(msg + "\n")
                except (OSError, ValueError):
                    pass

    def progress(self, msg):
        """Screen-only live status (overwrites one line on a terminal; not written to the log file, not shown when
        output is piped or in verbose mode, where the once-a-minute log line is enough)."""
        if self.verbose or not sys.stdout.isatty():
            return
        with self._lock:
            width = max(20, shutil.get_terminal_size((100, 24)).columns - 1)
            print("\r\x1b[K" + msg[:width], end="", flush=True)
            self._inline = True

    def error(self, msg):
        self.errors.append(msg)
        self.log("ERROR: " + msg)

    def summary(self):
        if not self.errors:
            self.log("No errors.")
            return
        self.log(f"{len(self.errors)} ERROR(S) THIS SESSION:")
        for e in self.errors:
            self.log("  - " + e)

    def close(self):
        try:
            self._f.close()
        except OSError:
            pass


# ------------------------------------------------------------------ STATUS line
class StatusFile:
    def __init__(self, path, total, log_path):
        self.path = path
        self.total = total
        self.log_path = log_path
        self.t0 = time.monotonic()
        self.started = _now_str()

    def _write(self, line):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            f.write(line + "\n")
        os.replace(tmp, self.path)          # atomic: a reader never sees a half-written line
        chown_to_user(self.path)

    def running(self, done, n_errors):
        errs = f"{n_errors} error" + ("" if n_errors == 1 else "s")
        self._write(f"RUNNING.   {done}/{self.total}. Started: {self.started}. "
                    f"Running for: {fmt_duration(time.monotonic() - self.t0)}. "
                    f"Updated: {datetime.datetime.now().strftime('%H:%M')}. {errs}.")

    def completed(self, done, n_errors):
        tail = "NO ERRORS." if n_errors == 0 else f"{n_errors} ERROR{'S' if n_errors != 1 else ''} - see log {self.log_path}"
        self._write(f"COMPLETED. {done}/{self.total}. Started: {self.started}. "
                    f"Ran for: {fmt_duration(time.monotonic() - self.t0)}. {tail}")

    def waiting(self, done, n_errors, what):
        errs = f"{n_errors} error" + ("" if n_errors == 1 else "s")
        self._write(f"WAITING.   {done}/{self.total}. Started: {self.started}. "
                    f"Running for: {fmt_duration(time.monotonic() - self.t0)}. "
                    f"Updated: {datetime.datetime.now().strftime('%H:%M')}. {errs}. WAITING FOR OPERATOR: {what}")

    def aborted(self, done, reason):
        self._write(f"ABORTED.   {done}/{self.total}. Started: {self.started}. "
                    f"Ran for: {fmt_duration(time.monotonic() - self.t0)}. {reason} - see log {self.log_path}")
