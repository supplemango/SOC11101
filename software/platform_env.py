#!/usr/bin/env python3
# VERSION v20261020.2345   (vYYYYMMDD.HHMM, UK local time of the last edit; bump on every change)
"""
platform_env.py -- shared per-platform environment control for the PQC energy testbed DUTs.

STATUS (2026-10-10): setup(), the thermal gate and teardown() have been run on real hardware on all five
DUTs (Pi 5, CM5, PC, RISC-V, Mac; results in e2-experimenter-design.md sections 13-22). Per platform:
  Pi 5     governor pin, manual fan (user_space thermal policy), vcgencmd throttle bits   -- all exercised
  CM5      governor pin, vcgencmd throttle bits (needs sudo)                              -- exercised
  PC       governor pin (4 policies), Intel thermal_throttle / Processor / powerclamp     -- exercised
           CPU clock option (--cpu-clock): default fixed 2600 MHz, turbo, noturbo, or <MHz>  -- the sysfs limits
           were exercised by hand (2.7 GHz held exactly on 1 core, within 100 MHz on 4); the library code path is
           tested only against a fake sysfs tree until the next PC run
  RISC-V   governor already `performance` (pin is a no-op), cpufreq-cpu0 cooling state    -- exercised
  Mac      no governor or fan control; temperature = `ismc` CPU Die Average (TCMb); fan
           rpm read-only; throttle = `pmset -g therm`, whose format under REAL throttling
           has not been seen                                                              -- partly exercised
NOT exercised anywhere: real throttling, fan failure, signal-handler teardown, sudo -n for sysfs writes
on an unattended run. Kinds outside VERIFIED_KINDS log a warning and use capability detection only.

ROOT RULE (Cameron, 2026-10-10): every program built on this library must be run with sudo on EVERY platform, the Mac
included, and for ALL experiments. Call platform_env.require_root() as the first statement of the experimenter; it prints a clear message and raises EnvError
before anything is changed. --selftest (read-only) needs no root; --gate-test and --fan-test do.

What it does (per the agreed E2 design):
  setup()              once per session: snapshot, pin governor, take fan under manual control (where the
                       platform allows it), confirm the external fan, measure T_ref (quiescent temperature).
                       Also pauses background jobs for the session on Linux (apt/unattended-upgrades and housekeeping
                       timers; needs root/sudo -n) and restores them at teardown. On macOS nothing is paused (no
                       verified way to pause update checking). Radios and other background state are RECORDED in
                       describe(), never changed.
                       On kind=pc it also fixes the CPU clock (cpu_clock option, default 2600 MHz with turbo
                       allowed; or `turbo` = stock behaviour, `noturbo` = 2.0 GHz, or any MHz in the hardware
                       range) by setting scaling_min_freq = scaling_max_freq; restored at teardown.
  prepare_iteration()  before every run: thermal gate to T_ref + 5 C, approached from above.
  throttle_state()     post-run check.
  set_clock_mhz(mhz)   (experimenters' --freqs) pin all cpufreq policies to one clock between pairs; available_clocks_mhz()
                       lists the steps; teardown() puts the starting limits back and checks them by read-back.
  teardown()           restore everything (idempotent; also runs on SIGINT/SIGTERM/exit).

Nothing here is called inside the measured START..STOP window.

Self-tests (run on a DUT):
  python3 platform_env.py --selftest        read-only: detect and print capabilities, read temperature
  python3 platform_env.py --fan-test        set fan, trace rpm for 40 s, restore  (Pi 5 only; needs sudo)
  python3 platform_env.py --gate-test [--cpu-clock default|turbo|noturbo|<MHz>]   (the clock option applies to the PC)
                                            full setup + one thermal gate + 30 s idle trace + restore
                                            (sudo on Linux; not needed on macOS)
"""
PLATFORM_ENV_VERSION = "v20261020.2345"   # keep identical to the VERSION comment on line 2

import argparse
import atexit
import collections
import glob
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time


class EnvError(RuntimeError):
    pass


ROOT_MESSAGE = ("This program must be run as root on every platform (all experiments, all DUTs, including the Mac).\n"
                "  Nothing has been changed. Re-run it with sudo, for example:\n"
                "      sudo python3 {prog} ...")


def require_root(prog=None, log=print):
    """Module-level root check: call it as the very FIRST statement of every experimenter (before argument parsing,
    before building a PlatformEnvironment). Cameron's rule (2026-10-10): sudo is mandatory on all platforms (the Mac
    included) and for all experiments. Prints a clear message and raises EnvError; nothing has been changed by then."""
    if os.geteuid() == 0:
        return
    msg = ROOT_MESSAGE.format(prog=prog or os.path.basename(sys.argv[0]) or "<program>")
    log(msg)
    raise EnvError(msg)


def _read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip().strip("\x00")
    except OSError:
        return default


# Linux background units that may fire during a run (seen on DUT-RISCV 2026-10-10: apt-daily*, unattended-upgrades,
# sysstat-collect every 10 min, man-db, fstrim, logrotate, ...). Those that are ACTIVE at setup() are stopped for the
# session and started again at teardown(); units that do not exist or are inactive are left alone.
LINUX_QUIESCE_UNITS = (
    "unattended-upgrades.service", "apt-daily.timer", "apt-daily-upgrade.timer",
    "sysstat-collect.timer", "sysstat-summary.timer", "phpsessionclean.timer", "man-db.timer",
    "fstrim.timer", "e2scrub_all.timer", "dpkg-db-backup.timer", "logrotate.timer",
    "systemd-tmpfiles-clean.timer", "motd-news.timer", "update-notifier-download.timer",
)

# Preferred thermal zone types, in order (first match wins; else thermal_zone0).
ZONE_TYPE_PREF = ["cpu-thermal", "x86_pkg_temp", "cpu_thermal", "soc-thermal", "soc_thermal", "cpu"]


class PlatformEnvironment:
    # Platform kinds whose control path (setup, thermal gate, teardown) has been exercised on real hardware:
    # pi5 (fan, governor), cm5, pc, riscv (governor, gate), mac (ismc sensor, gate; its `pmset -g therm` throttle
    # format under real throttling has NOT been seen), all 2026-10-09/10. Not covered anywhere: real throttling,
    # fan failure, signal-handler teardown.
    VERIFIED_KINDS = {"pi5", "cm5", "pc", "riscv", "mac"}

    def __init__(
        self,
        sysroot="/",
        log=print,
        fan_confirmed=False,
        target_offset_c=5.0,      # gate target = T_ref + this
        burn_overshoot_c=3.0,     # when below target, burn to target + this, then cool down (tuned in pilot)
        poll_s=0.05,              # gate polling interval (ch4: 50 ms)
        abort_temp_c=82.0,        # never burn above this; setup refuses to start above it
        fan_min_rpm=5000,         # fan considered "at max" above this (DUT-Pi5 reaches ~8000)
        ref_window_s=60.0,        # rolling window for the T_ref trend test (single rule; was 120 s at 3 s polling)
        ref_poll_s=1.0,           # 1 s polling: 60 samples in the window, slope noise about 0.3 C/min (e2 design doc sec. 40)
        ref_slope_c_per_min=1.0,  # max |slope| over the window (C/min)
        ref_max_std_c=2.5,        # window std must also be below this (DUT-Mac 2nd run: a background transient gave std 3.7 C and slope +0.73, which passed the slope test alone)
        ref_max_wait_s=600.0,
        gate_timeout_s=600.0,
        gate_filter_s=1.0,        # gate decisions use the median of this many seconds of readings
        burn_min_s=5.0,           # when below target, always burn at least this long
        measure_clock=False,      # after the warm-up, burn all cores for measure_clock_s and record the clock they run at (burn_freq_khz); the E2 experimenter turns it on
        measure_clock_s=1.5,
        plateau_window_s=60.0,    # a burn that rose less than plateau_rise_c over this window (after at least this long) has plateaued
        plateau_rise_c=0.5,
        burn_max_s=150.0,         # ... and at most this long once the reading is above target (a strong fan can hold the burn below target + overshoot)
        quiesce=True,             # pause background update/housekeeping jobs for the session (restored at teardown)
        ntp_server="192.168.55.100",   # JUMP (Stratum 1, GPS/PPS) -- the testbed's only time reference
        sync_max_offset_ms=5.0,   # sync_clock() succeeds when the measured DUT clock error is within this
        sync_max_wait_s=60.0,
        cpu_clock="default",      # "default" | "turbo" | "noturbo" | <MHz>; see _set_cpu_clock. default = 2600 MHz on kind=pc, no change elsewhere
    ):
        self.sysroot = sysroot
        self.log = log
        self.fan_confirmed = fan_confirmed
        self.target_offset_c = target_offset_c
        self.burn_overshoot_c = burn_overshoot_c
        self.poll_s = poll_s
        self.abort_temp_c = abort_temp_c
        self.fan_min_rpm = fan_min_rpm
        self.ref_window_s = ref_window_s
        self.ref_poll_s = ref_poll_s
        self.ref_slope_c_per_min = ref_slope_c_per_min
        self.ref_max_std_c = ref_max_std_c
        self.ref_max_wait_s = ref_max_wait_s
        self.gate_timeout_s = gate_timeout_s
        self.gate_filter_s = gate_filter_s
        self.burn_min_s = burn_min_s
        self.burn_max_s = burn_max_s
        self.measure_clock = measure_clock
        self.measure_clock_s = measure_clock_s
        self._pair_pin_khz = None     # set by set_clock_mhz: the clock the runs are pinned to (the gate warms up at the SESSION clock, then drops to this)
        self.plateau_window_s = plateau_window_s
        self.plateau_rise_c = plateau_rise_c
        self.quiesce = quiesce
        self.ntp_server = ntp_server
        self.sync_max_offset_ms = sync_max_offset_ms
        self.sync_max_wait_s = sync_max_wait_s
        self.sync_info = None
        self.ntp_port = 123          # attribute, not a parameter: only tests change it
        self.cpu_clock_req = str(cpu_clock).strip().lower()

        self.kind = self._detect_kind()
        self.model = _read(self._p("/proc/device-tree/model"), "")
        self.zone_path = self._pick_zone()
        self.fan = self._find_fan()
        self.t_ref = None
        self.t_ref_converged = None
        self.t_target = None
        self._snap = {}
        self._setup_done = False
        self._torn_down = False
        self._burners = []
        if self.kind not in self.VERIFIED_KINDS:
            self.log(f"WARNING: platform kind '{self.kind}' is UNVERIFIED; controls are capability-detected only")

    # ------------------------------------------------------------------ helpers
    def _p(self, path):
        return os.path.join(self.sysroot, path.lstrip("/"))

    def _write(self, path, value):
        """Write a sysfs value; fall back to `sudo -n tee` if not permitted."""
        data = str(value)
        try:
            with open(path, "w") as f:
                f.write(data)
            return
        except PermissionError:
            pass
        r = subprocess.run(["sudo", "-n", "tee", path], input=data, text=True, capture_output=True)
        if r.returncode != 0:
            raise EnvError(f"cannot write {data!r} to {path}: {r.stderr.strip()}")

    def require_root(self, prog=None):
        """Same check as the module-level require_root(); exempt when running against a test tree (sysroot != "/")."""
        if self.sysroot != "/":
            return
        require_root(prog, self.log)

    def _check_privileges(self):
        self.require_root()

    # ------------------------------------------------------------------ detection
    def _detect_kind(self):
        try:
            sysname = os.uname().sysname
            machine = os.uname().machine
        except AttributeError:
            return "generic"
        if sysname == "Darwin":
            return "mac"
        model = _read(self._p("/proc/device-tree/model"), "") or ""
        if "Raspberry Pi 5" in model:
            return "pi5"
        if "Compute Module 5" in model:
            return "cm5"
        if machine.startswith("riscv"):
            return "riscv"
        if machine in ("x86_64", "amd64"):
            return "pc"
        return "generic"

    def _pick_zone(self):
        zones = sorted(glob.glob(self._p("/sys/class/thermal/thermal_zone*")))
        types = {z: _read(os.path.join(z, "type"), "") for z in zones}
        for pref in ZONE_TYPE_PREF:
            for z in zones:
                if types[z] == pref:
                    return z
        return zones[0] if zones else None

    def _find_fan(self):
        """Find a PWM fan by hwmon NAME (the index is not stable: hwmon1 vs hwmon2 seen on DUT-Pi5)."""
        for h in sorted(glob.glob(self._p("/sys/class/hwmon/hwmon*"))):
            if _read(os.path.join(h, "name")) == "pwmfan" and os.path.exists(os.path.join(h, "pwm1")):
                return {
                    "dir": h,
                    "pwm1": os.path.join(h, "pwm1"),
                    "enable": os.path.join(h, "pwm1_enable"),
                    "rpm": os.path.join(h, "fan1_input"),
                    "zone": self._fan_zone(),
                }
        return None

    def _fan_zone(self):
        """The thermal zone whose cooling devices include the pwm-fan (that zone's policy must be user_space)."""
        for z in sorted(glob.glob(self._p("/sys/class/thermal/thermal_zone*"))):
            for link in sorted(glob.glob(os.path.join(z, "cdev*"))):
                if os.path.islink(link) or os.path.isdir(link):
                    if _read(os.path.join(link, "type")) == "pwm-fan":
                        return z
        return self.zone_path

    def fan_mode(self):
        if self.fan:
            return "sysfs-pwm"
        if self.kind == "pc":
            return "bios-manual"      # set in BIOS (Software mode, PWM 255); cannot be read back from the OS
        if self.kind == "mac":
            return "read-only"
        return "none"

    def _governor_files(self):
        return sorted(glob.glob(self._p("/sys/devices/system/cpu/cpufreq/policy*/scaling_governor")))

    # ------------------------------------------------------------------ readings
    # macOS (DUT-Mac, M4 Mac mini): format of `ismc temp` / `ismc fans` seen 2026-10-10 (iSMC, brew cask):
    #   " CPU Die Average                      TCMb    33.639194 C  flt"   (degree sign before C)
    #   " CPU Die Max                          TCMz    36.875 C     flt"
    #   " Fan 1 Current Speed  F0Ac  1000 rpm  flt" (also F0Mx 4900, F0Mn 1000, F0Tg 1000; read-only)
    # The gate uses 'CPU Die Average' (TCMb); 'CPU Die Max' (TCMz) is kept in self.mac_die_max for the record.
    _MAC_BIN_CANDIDATES = ("/opt/homebrew/bin/ismc", "/usr/local/bin/ismc")

    def _ismc(self, what):
        exe = shutil.which("ismc") or next((c for c in self._MAC_BIN_CANDIDATES if os.path.exists(c)), None)
        if not exe:
            return None
        try:
            r = subprocess.run([exe, what], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout if r.returncode == 0 else None

    @staticmethod
    def _parse_ismc_value(text, key):
        """Numeric value on the line carrying SMC key `key` (e.g. TCMz, F0Ac); None if absent."""
        text = re.sub(r"\x1b\[[0-9;]*m", "", text or "")   # ismc colours its table even when piped (seen 2026-10-10)
        m = re.search(r"\b" + re.escape(key) + r"\s+(-?[0-9]+(?:\.[0-9]+)?)", text or "")
        return float(m.group(1)) if m else None

    def _read_temp_mac(self):
        """Gate sensor = CPU Die AVERAGE (TCMb). 'CPU Die Max' (TCMz) follows the single hottest core and showed
        +2..+4 C spikes between consecutive 1 s samples at idle (and one 55 C outlier), whereas TCMb moved smoothly
        (35.80 -> 35.01 C over 2 min, no jumps) on DUT-Mac 2026-10-10. TCMz is kept for the record."""
        out = self._ismc("temp")
        mx = self._parse_ismc_value(out, "TCMz")
        if mx is not None:
            self.mac_die_max = mx
        return self._parse_ismc_value(out, "TCMb")

    def read_temp(self):
        """CPU temperature in C, or None if no sensor is known on this platform."""
        if self.kind == "mac":
            return self._read_temp_mac()
        if not self.zone_path:
            return None
        raw = _read(os.path.join(self.zone_path, "temp"))
        try:
            return float(raw) / 1000.0
        except (TypeError, ValueError):
            return None

    def read_freq(self):
        """Current and maximum CPU frequency (kHz) of the first cpufreq policy, or None."""
        pols = sorted(glob.glob(self._p("/sys/devices/system/cpu/cpufreq/policy*")))
        if not pols:
            return None
        def num(name):
            try:
                return int(_read(os.path.join(pols[0], name)))
            except (TypeError, ValueError):
                return None
        # scaling_min/max are the limits now in force (meaningful after the clock step); cur_khz is ONE sample from
        # policy0, which is idle at setup time, so it reads whatever the idle core is doing and says nothing about the
        # frequency under load (that is burn_freq_khz in the gate result).
        return {"scaling_min_khz": num("scaling_min_freq"), "scaling_max_khz": num("scaling_max_freq"),
                "cur_khz_idle_sample": num("scaling_cur_freq"), "hw_max_khz": num("cpuinfo_max_freq")}

    def read_rpm(self):
        if self.kind == "mac":
            v = self._parse_ismc_value(self._ismc("fans"), "F0Ac")
            return int(round(v)) if v is not None else None
        if not self.fan:
            return None
        try:
            return int(_read(self.fan["rpm"]))
        except (TypeError, ValueError):
            return None

    def _throttle_count(self):
        """Cumulative Intel thermal-throttle event count (core + package), or None if not exposed."""
        files = glob.glob(self._p("/sys/devices/system/cpu/cpu*/thermal_throttle/*_throttle_count"))
        if not files:
            return None
        total = 0
        for f in files:
            try:
                total += int(_read(f, "0"))
            except ValueError:
                pass
        return total

    def read_no_turbo(self):
        """Intel: 1 = Turbo disabled, 0 = enabled (expected 0 on DUT-PC). None where not applicable."""
        v = _read(self._p("/sys/devices/system/cpu/intel_pstate/no_turbo"))
        return int(v) if v in ("0", "1") else None

    def throttle_state(self, since=None):
        """Check for throttling. Pass the dict returned by an earlier call as `since` (e.g. taken just before
        START) so cumulative counters (Intel thermal_throttle) are compared as a delta.
        Returns {'throttled': bool|None, 'detail': str, 'count': int|None}."""
        detail, flag = [], None
        count = self._throttle_count()
        if count is not None:
            detail.append(f"thermal_throttle_count={count}")
            if since is not None and since.get("count") is not None:
                flag = count > since["count"]
                detail.append(f"delta={count - since['count']}")
        if self.sysroot == "/" and shutil.which("vcgencmd"):
            # /dev/vcio is root-only on the Pi 5 family (seen on DUT-CM5), so retry via sudo -n
            cmds = [["vcgencmd", "get_throttled"], ["sudo", "-n", "vcgencmd", "get_throttled"]]
            m = None
            for cmd in cmds:
                r = subprocess.run(cmd, capture_output=True, text=True)
                m = re.search(r"0x([0-9a-fA-F]+)", r.stdout or "")
                if m:
                    break
            if m:
                v = int(m.group(1), 16)
                now = v & 0xF  # bits 0-3 = active now (bits 16-19 are sticky since boot: ignore)
                flag = bool(flag) or bool(now)
                detail.append(f"vcgencmd now-bits=0x{now:x}")
            else:
                detail.append("vcgencmd present but not usable (permissions?)")
        if self.kind == "mac" and self.sysroot == "/":
            # `pmset -g therm` prints "Note: No ... has been recorded" when clean (seen 2026-10-10). The format under
            # real throttling has NOT been observed: flag a CPU_Speed_Limit / CPU_Scheduler_Limit below 100, or a
            # warning-level line that is not a "Note: No ..." line. UNVERIFIED until a throttled sample is seen.
            try:
                r = subprocess.run(["pmset", "-g", "therm"], capture_output=True, text=True, timeout=10)
                txt = r.stdout or ""
            except (OSError, subprocess.TimeoutExpired):
                txt = ""
            if txt:
                bad = []
                for line in txt.splitlines():
                    m = re.match(r"\s*(CPU_Speed_Limit|CPU_Scheduler_Limit)\s*=\s*(\d+)", line)
                    if m and int(m.group(2)) < 100:
                        bad.append(line.strip())
                    elif "warning level" in line.lower() and not line.strip().startswith("Note: No"):
                        bad.append(line.strip())
                flag = bool(flag) or bool(bad)
                detail.append("pmset therm: " + ("; ".join(bad) if bad else "clean"))
            else:
                detail.append("pmset -g therm unavailable")
        for c in sorted(glob.glob(self._p("/sys/class/thermal/cooling_device*"))):
            t = _read(os.path.join(c, "type"), "")
            if t.startswith("cpufreq") or t in ("Processor", "intel_powerclamp"):  # DUT-PC types seen 2026-10-09
                try:
                    st = int(_read(os.path.join(c, "cur_state"), "0"))
                except ValueError:
                    st = 0
                detail.append(f"{t} cur_state={st}")
                flag = bool(flag) or st > 0
        return {"throttled": flag, "detail": "; ".join(detail) or "no throttle source available", "count": count}

    # ------------------------------------------------------------------ commands, radios, background jobs
    def _run(self, cmd, timeout=15):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except (OSError, subprocess.TimeoutExpired) as e:
            return 127, str(e)

    def _priv(self, cmd, timeout=30):
        """Run a command as root: directly if we are root, else via `sudo -n` (never prompts)."""
        if os.geteuid() == 0:
            return self._run(cmd, timeout)
        return self._run(["sudo", "-n"] + list(cmd), timeout)

    def radio_state(self):
        """Radios as found (read-only; recorded in the run record, never changed here).
        Linux: every /sys/class/rfkill entry. macOS: Wi-Fi power via networksetup and Bluetooth state via
        system_profiler (the Bluetooth parse is UNVERIFIED on this macOS: raw 'State:' value is recorded)."""
        out = {}
        if self.kind == "mac":
            if self.sysroot != "/":
                return out
            rc, txt = self._run(["networksetup", "-listallhardwareports"])
            m = re.search(r"Hardware Port: Wi-Fi\s*\nDevice: (\S+)", txt)
            if m:
                rc2, t2 = self._run(["networksetup", "-getairportpower", m.group(1)])
                out["wifi"] = t2.strip().rsplit(":", 1)[-1].strip() if rc2 == 0 else "unknown"
            rc, txt = self._run(["system_profiler", "SPBluetoothDataType"], timeout=30)
            m = re.search(r"State:\s*(\w+)", txt)
            out["bluetooth"] = m.group(1) if m else "unknown"
            return out
        for d in sorted(glob.glob(self._p("/sys/class/rfkill/rfkill*"))):
            name, typ = _read(os.path.join(d, "name"), "?"), _read(os.path.join(d, "type"), "?")
            out[f"{typ}:{name}"] = f"soft={_read(os.path.join(d, 'soft'), '?')},hard={_read(os.path.join(d, 'hard'), '?')}"
        return out

    def background_state(self):
        """Background jobs as found (read-only). Linux: which quiesce units are active. macOS: automatic update
        checking, Spotlight indexing and Time Machine activity (recorded only; not changed)."""
        if self.sysroot != "/":
            return {}
        if self.kind == "mac":
            st = {}
            rc, t = self._run(["softwareupdate", "--schedule"])
            st["auto_update_check"] = "on" if "turned on" in t else ("off" if "turned off" in t else "unknown")
            rc, t = self._run(["mdutil", "-s", "/"])
            st["spotlight"] = "enabled" if "enabled" in t.lower() else ("disabled" if "disabled" in t.lower() else "unknown")
            rc, t = self._run(["tmutil", "status"])
            m = re.search(r"Running\s*=\s*(\d)", t)
            st["time_machine_running"] = m.group(1) if m else "unknown"
            return st
        if not shutil.which("systemctl"):
            return {}
        return {u: "active" for u in LINUX_QUIESCE_UNITS
                if self._run(["systemctl", "is-active", u])[1].strip() == "active"}

    def _found(self, attr, fn):
        """State recorded once at setup() ('as found'). describe() NEVER re-queries it: on the Mac, calling
        `softwareupdate`/`system_profiler` wakes softwareupdated (its log was quiet at idle, 2 lines per 10 min, and
        showed bursts at boot and around our own calls), so no such call may happen near a run. Only a read-only
        --selftest (no setup) queries live, once."""
        if getattr(self, attr, None) is None:
            setattr(self, attr, fn())
        return getattr(self, attr)

    def _quiesce_start(self):
        self._snap["quiesce"] = {"units": [], "mac_updates_paused": False}
        self.background_found = self.background_state()   # as found, before anything is paused
        self.radios_found = self.radio_state()
        if not self.quiesce or self.sysroot != "/":
            return
        if self.kind == "mac":
            # macOS: nothing is paused. Cameron's decision (2026-10-10): DUT-Mac is also his desktop machine and not a
            # like-for-like DUT, so Wi-Fi, Bluetooth, Spotlight, Time Machine and update checking are RECORDED only.
            # Tried and found not to work on macOS 15.3.1: `softwareupdate --schedule off` returns 0 but the check stays
            # "turned on", and writing AutomaticCheckEnabled to /Library/Preferences/com.apple.SoftwareUpdate did not
            # persist ("Domain not found" on delete), so there is no verified way to pause update checking.
            self.log("quiesce: macOS background state is recorded only (no verified way to pause update checking)")
            return
        if not shutil.which("systemctl"):
            return
        for u in LINUX_QUIESCE_UNITS:
            if self._run(["systemctl", "is-active", u])[1].strip() != "active":
                continue
            rc, t = self._priv(["systemctl", "stop", u])
            if rc == 0:
                self._snap["quiesce"]["units"].append(u)
            else:
                self.log(f"quiesce: could not stop {u}: {t.strip()[:80]}")
        n = len(self._snap["quiesce"]["units"])
        self.log(f"quiesce: stopped {n} background unit(s) for this session"
                 + (": " + ", ".join(self._snap["quiesce"]["units"]) if n else ""))

    def _quiesce_restore(self):
        q = self._snap.get("quiesce") or {}
        errors = []
        for u in q.get("units", []):
            rc, t = self._priv(["systemctl", "start", u])
            if rc != 0:
                errors.append(f"start {u}: {t.strip()[:80]}")
        return errors

    # ------------------------------------------------------------------ clock sync (before every run)
    # Design (e2 design doc sec. 4, 21, 39): sync-then-quiesce. Measure the DUT clock error against JUMP with our own
    # SNTP query (the same measurement on every platform, nothing parsed from chronyc/timedatectl), make sure the
    # platform's time daemon is running and let it correct the clock, then STOP the daemon so its polling cannot
    # disturb the thermal gate or the measured window. The daemon's original state is restored at teardown.
    # DUT-Mac: the macOS time daemon cannot be stopped and is not touched (Cameron, 2026-10-10); the offset is only measured.
    TIME_SERVICES = (("chrony", "chrony.service"), ("timesyncd", "systemd-timesyncd.service"))

    def _sntp_query(self, server, timeout=1.0, samples=4):
        """Read-only SNTP query (never sets the clock). Returns the lowest-delay sample as a dict, or None.
        dut_clock_error_ms is positive when the DUT clock is AHEAD of the server."""
        best = None
        for i in range(samples):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            try:
                pkt = bytearray(48)
                pkt[0] = 0x23                      # leap 0, version 4, mode 3 (client)
                t1 = time.time()
                x = t1 + 2208988800
                struct.pack_into("!II", pkt, 40, int(x), int((x % 1) * 2 ** 32))
                sock.sendto(bytes(pkt), (server, self.ntp_port))
                data, _ = sock.recvfrom(512)
                t4 = time.time()
            except OSError:
                continue
            finally:
                sock.close()
            if len(data) < 48 or data[1] == 0 or (data[0] >> 6) == 3:   # kiss-o'-death / unsynchronised server
                continue

            def ts(off):
                sec, frac = struct.unpack_from("!II", data, off)
                return sec - 2208988800 + frac / 2 ** 32
            t2, t3 = ts(32), ts(40)
            offset = ((t2 - t1) + (t3 - t4)) / 2.0          # server minus DUT
            delay = (t4 - t1) - (t3 - t2)
            cand = {"server": server, "stratum": data[1], "dut_clock_error_ms": round(-offset * 1000.0, 3),
                    "delay_ms": round(delay * 1000.0, 3)}
            if best is None or cand["delay_ms"] < best["delay_ms"]:
                best = cand
            if i + 1 < samples:
                time.sleep(0.2)
        return best

    def _time_service(self):
        """(name, unit) of the Linux time daemon in use: the active one, else the enabled one, else None."""
        if self.kind == "mac" or not shutil.which("systemctl"):
            return None
        for name, unit in self.TIME_SERVICES:
            if self._run(["systemctl", "is-active", unit])[1].strip() == "active":
                return name, unit
        for name, unit in self.TIME_SERVICES:
            if self._run(["systemctl", "is-enabled", unit])[1].strip() == "enabled":
                return name, unit
        return None

    def sync_clock(self, server=None, max_wait_s=None, max_offset_ms=None):
        """Sync-then-quiesce. Call before GETREADY on EVERY run. Never raises for a sync failure: returns a dict
        {method, server, ok, dut_clock_error_ms (final), error_before_ms, stratum, delay_ms, waited_s, note} for
        the run record; ok=False means 'flag the run', not 'abort'."""
        server = server or self.ntp_server
        max_wait_s = self.sync_max_wait_s if max_wait_s is None else max_wait_s
        lim = self.sync_max_offset_ms if max_offset_ms is None else max_offset_ms
        t0 = time.monotonic()
        res = {"method": None, "server": server, "ok": False, "dut_clock_error_ms": None, "error_before_ms": None,
               "stratum": None, "delay_ms": None, "waited_s": None, "limit_ms": lim, "note": ""}
        m = self._sntp_query(server)
        if m is None:
            res.update(method="none", note=f"no SNTP reply from {server}", waited_s=round(time.monotonic() - t0, 1))
            self.sync_info = res
            self.log(f"sync_clock: FAILED, {res['note']}")
            return res
        res["error_before_ms"] = m["dut_clock_error_ms"]
        svc = None if self.sysroot != "/" else self._time_service()
        if svc is None:
            res.update(method="sntp-readonly" if self.kind == "mac" else "sntp-no-daemon",
                       dut_clock_error_ms=m["dut_clock_error_ms"], stratum=m["stratum"], delay_ms=m["delay_ms"],
                       ok=abs(m["dut_clock_error_ms"]) <= lim, waited_s=round(time.monotonic() - t0, 1),
                       note="clock not changed (no controllable time daemon)")
            self.sync_info = res
            self.log(f"sync_clock: {res['method']}: error {res['dut_clock_error_ms']:+.3f} ms, stratum {res['stratum']}, "
                     f"ok={res['ok']}")
            return res
        name, unit = svc
        res["method"] = name
        st = self._snap.setdefault("timesvc", {"name": name, "unit": unit,
                                               "was_active": self._run(["systemctl", "is-active", unit])[1].strip() == "active"})
        entry_active = self._run(["systemctl", "is-active", unit])[1].strip() == "active"
        if not entry_active:
            rc, t = self._priv(["systemctl", "start", unit])
            if rc != 0:
                res["note"] = f"could not start {unit}: {t.strip()[:80]}"
        nudges = 0
        last_nudge = 0.0
        deadline = time.monotonic() + max_wait_s
        while True:
            time.sleep(1.0)
            m = self._sntp_query(server, samples=3) or m
            err = m["dut_clock_error_ms"]
            if abs(err) <= lim:
                res["ok"] = True
                break
            if time.monotonic() > deadline:
                res["note"] = (res["note"] + "; " if res["note"] else "") + f"still {err:+.1f} ms off after {max_wait_s:.0f} s"
                break
            # Second case (DUT-PC smoke 2026-10-12, first run of the session: +11.8 ms, still +12.3 ms after 60 s, run
            # flagged and lost): an error above the limit but below 50 ms that has not closed after 15 s gets the same
            # daemon restart, at most twice, 15 s apart.
            now = time.monotonic()
            if (abs(err) > 50.0 and nudges < 4 and now - t0 >= 3.0 and now - last_nudge >= 5.0) or \
                    (abs(err) <= 50.0 and nudges < 2 and now - t0 >= 15.0 and now - last_nudge >= 15.0):
                # A large error: have the daemon step now instead of slewing. DUT-RISCV (chrony journal, 2026-10-10
                # 18:20): `chronyc makestep` on a chrony that has been running a while steps by its LAST measurement,
                # which is stale after a clock jump (it polls every ~128 s), so four makesteps stepped 0.000000 s and
                # chrony only noticed ("System clock wrong by -1.6 s") 45 s later. A freshly started chrony measures
                # the source within ~5 s (iburst) and a makestep then works (535 ms -> 0 in 10.8 s). So: if the
                # daemon was already running, RESTART it first; then makestep every 5 s (at most 4 nudges).
                nudges += 1
                last_nudge = time.monotonic()
                if name == "chrony" and not (entry_active and nudges == 1):
                    self._priv(["chronyc", "makestep"])
                else:
                    self._priv(["systemctl", "restart", unit])
        if nudges:
            res["note"] = (res["note"] + "; " if res["note"] else "") + \
                f"{'restart+makestep' if name == 'chrony' and entry_active else ('makestep' if name == 'chrony' else 'restarted ' + unit)} x{nudges}"
        res.update(dut_clock_error_ms=m["dut_clock_error_ms"], stratum=m["stratum"], delay_ms=m["delay_ms"])
        rc, t = self._priv(["systemctl", "stop", unit])     # quiesce: no time-daemon polling during the run
        if rc != 0:
            res["note"] = (res["note"] + "; " if res["note"] else "") + f"could not stop {unit}: {t.strip()[:80]}"
        res["waited_s"] = round(time.monotonic() - t0, 1)
        self.sync_info = res
        self.log(f"sync_clock: {name}: error {res['error_before_ms']:+.3f} ms before, {res['dut_clock_error_ms']:+.3f} ms after "
                 f"(limit {lim} ms), stratum {res['stratum']}, {res['waited_s']} s, ok={res['ok']}"
                 + (f" [{res['note']}]" if res["note"] else "") + "; daemon stopped until teardown")
        return res

    def _time_service_restore(self):
        st = self._snap.get("timesvc")
        errors = []
        if st and st["was_active"]:
            rc, t = self._priv(["systemctl", "start", st["unit"]])
            if rc != 0:
                errors.append(f"start {st['unit']}: {t.strip()[:80]}")
        return errors

    # ------------------------------------------------------------------ session setup
    def setup(self, measure_ref=True):
        self._check_privileges()
        if self._setup_done:
            return
        t0 = self.read_temp()
        if t0 is not None and t0 >= self.abort_temp_c:
            raise EnvError(f"refusing to start: board already at {t0:.1f} C (abort threshold {self.abort_temp_c})")
        self._install_handlers()
        self._setup_done = True
        self._torn_down = False

        # 1. snapshot, then pin governor
        gov = {f: _read(f) for f in self._governor_files()}
        self._snap["governor"] = gov
        for f in gov:
            self._write(f, "performance")
            if _read(f) != "performance":
                raise EnvError(f"governor did not take at {f}: reads {_read(f)!r}")
        self._set_cpu_clock()
        self.freq_after_pin = self.read_freq()   # after the clock step, so the limits shown are the ones in force
        if gov:
            fp = self.freq_after_pin or {}
            self.log(f"governor: pinned {len(gov)} policy(ies) to performance; limits now min/max "
                     f"{fp.get('scaling_min_khz')}/{fp.get('scaling_max_khz')} kHz (hardware max {fp.get('hw_max_khz')}; "
                     f"the frequency under load is measured by the gate burn, not here)")
        else:
            self.log("governor: no cpufreq policy files found (not controllable here)")

        # 1b. pause background update/housekeeping jobs (restored at teardown)
        self._quiesce_start()

        # 2. fan under manual control (DUT-Pi5 method verified 2026-10-09)
        if self.fan:
            self._set_fan_max()
        else:
            self.log(f"fan: no sysfs-controllable fan (mode={self.fan_mode()})")

        # 3. external fan prompt
        self._confirm_external_fan()

        # 4. quiescent reference
        if measure_ref:
            self.measure_t_ref()

    # ------------------------------------------------------------------ CPU clock (fixed frequency / turbo)
    # Fixed clocks accepted on kind=pc: every 100 MHz from 800 to 3600 MHz. Verified 2026-10-10 (settled probe, one busy
    # core reached each value to within 1 MHz). The earlier "900-2300 MHz do not take" result was a read-back timing
    # artefact (limits are applied ~10 ms after the write). Values that are not multiples of 100 MHz are rounded down by
    # the hardware (2050 MHz ran at 2000 MHz), so they are refused. 2.0 GHz is also available as `noturbo`.
    PC_VALID_CLOCKS_MHZ = list(range(800, 3601, 100))
    PC_DEFAULT_CLOCK_MHZ = 2600  # DUT-PC: 4 cores free-turbo settle at 2.7 GHz (12 W limit); 2.6 leaves margin (sec. 29-30 of the E2 design notes)

    def _policy_dirs(self):
        return sorted(glob.glob(self._p("/sys/devices/system/cpu/cpufreq/policy*")))

    def _set_limits_all(self, dirs, lo_khz, hi_khz, floors=None):
        """Set min=lo and max=hi on EVERY policy in the order that works on DUT-PC (2026-10-10 15:39 diagnostic):
        (0) only where the current min is above the new max, drop min to the hardware floor; (1) max on all
        policies; (2) min on all policies. Writing min/max/min policy by policy (the obvious order) left the pin
        undone on all four policies: after the first policy's `min` write failed to stick, later writes were
        silently ignored. `lo_khz`/`hi_khz` may be dicts keyed by directory (restore) or ints."""
        lo = lo_khz if isinstance(lo_khz, dict) else {d: lo_khz for d in dirs}
        hi = hi_khz if isinstance(hi_khz, dict) else {d: hi_khz for d in dirs}
        for d in dirs:
            cur_min = int(_read(os.path.join(d, "scaling_min_freq"), "0") or 0)
            if cur_min > hi[d]:
                self._write(os.path.join(d, "scaling_min_freq"), int(_read(os.path.join(d, "cpuinfo_min_freq"), "0") or 0))
        for d in dirs:
            self._write(os.path.join(d, "scaling_max_freq"), hi[d])
        for d in dirs:
            self._write(os.path.join(d, "scaling_min_freq"), lo[d])

    def _clock_plan(self):
        """Resolve cpu_clock to ('none'|'turbo'|'noturbo'|'fixed', khz|None)."""
        req = self.cpu_clock_req
        if req in ("", "default"):
            return ("fixed", self.PC_DEFAULT_CLOCK_MHZ * 1000) if self.kind == "pc" else ("none", None)
        if req in ("turbo", "noturbo"):
            return (req, None)
        try:
            mhz = int(req)
        except ValueError:
            raise EnvError(f"cpu_clock must be default, turbo, noturbo or a number of MHz, not {req!r}")
        if self.kind == "pc" and mhz not in self.PC_VALID_CLOCKS_MHZ:
            raise EnvError(f"cpu_clock {mhz} MHz is not an accepted fixed clock on kind=pc; use one of "
                           f"{self.PC_VALID_CLOCKS_MHZ} MHz, or `noturbo` (2000 MHz)")
        return ("fixed", mhz * 1000)

    def reassert_pins(self):
        """Put the governor and the CPU clock back as setup() set them, after a tool (sbc-bench steps the CPU through
        its own frequency list) changed them. DUT-PC smoke 2026-10-12: sbc-bench left min/max at 800/3600 MHz and the
        Geekbench run after it ran unpinned (cpu_khz 800-3600) without any flag. The teardown snapshot is NOT retaken.
        Returns a list of error strings (empty = pins are back)."""
        errors = []
        try:
            for f in self._snap.get("governor", {}):
                self._write(f, "performance")
                if _read(f) != "performance":
                    errors.append(f"governor did not take at {f}: reads {_read(f)!r}")
            self._set_cpu_clock(resnap=False)
        except Exception as e:                              # EnvError from the read-back check, or an OS error
            errors.append(str(e))
        return errors

    def _set_cpu_clock(self, resnap=True):
        """Fix or release the CPU clock (cpufreq min/max and Intel turbo). Snapshot first, restore in teardown().
        resnap=False (reassert_pins) applies the same plan again WITHOUT retaking the snapshot, so teardown still
        restores the original pre-session state.
        turbo = no_turbo 0, limits untouched; noturbo = no_turbo 1 (DUT-PC: flat 2.0 GHz); <MHz> = turbo allowed,
        scaling_min_freq = scaling_max_freq = value on every policy (DUT-PC verified 2.7 GHz: exact for one core,
        within one 100 MHz step for four). Verified on hardware only for kind=pc; elsewhere the policy files are
        written the same way but the result is UNVERIFIED."""
        mode, khz = self._clock_plan()
        nt_path = self._p("/sys/devices/system/cpu/intel_pstate/no_turbo")
        pols = self._policy_dirs()
        self.clock_info = {"requested": self.cpu_clock_req, "mode": mode, "target_khz": khz}
        if mode == "none":
            self.log("cpu clock: not changed (governor pin only)")
            return
        if resnap:
            self._snap["clock"] = {
                "no_turbo": _read(nt_path),
                "limits": {d: (_read(os.path.join(d, "scaling_min_freq")), _read(os.path.join(d, "scaling_max_freq")))
                           for d in pols},
            }
        if mode in ("turbo", "fixed") and os.path.exists(nt_path):
            self._write(nt_path, 0)
        if mode == "noturbo":
            if not os.path.exists(nt_path):
                raise EnvError("cpu_clock=noturbo needs intel_pstate/no_turbo, which this platform does not have")
            self._write(nt_path, 1)
        if mode == "fixed":
            if not pols:
                raise EnvError("cpu_clock: no cpufreq policy directories to set a fixed frequency on")
            if self.kind != "pc":
                self.log(f"WARNING: fixed cpu clock on kind={self.kind} is UNVERIFIED (only DUT-PC has been tested)")
            self._apply_fixed_khz(khz, pols)
        self.clock_info.update({"no_turbo_now": self.read_no_turbo(),
                                "limits_now": {os.path.basename(d): [_read(os.path.join(d, "scaling_min_freq")),
                                                                    _read(os.path.join(d, "scaling_max_freq"))] for d in pols}})
        self.log(f"cpu clock: mode={mode}" + (f" fixed at {khz // 1000} MHz" if khz else "") +
                 f"; no_turbo={self.read_no_turbo()}")

    def _apply_fixed_khz(self, khz, pols):
        """min = max = khz on every policy, with the range check and the read-back poll (shared by the session clock
        step and set_clock_mhz). Raises EnvError if the value does not take."""
        for d in pols:
            lo = int(_read(os.path.join(d, "cpuinfo_min_freq"), "0") or 0)
            hi = int(_read(os.path.join(d, "cpuinfo_max_freq"), "0") or 0)
            if not (lo <= khz <= hi):
                raise EnvError(f"cpu_clock {khz // 1000} MHz outside hardware range {lo // 1000}-{hi // 1000} MHz at {d}")
        bad = {}
        for attempt in (1, 2):
            self._set_limits_all(pols, khz, khz)
            # The kernel applies limit changes asynchronously: DUT-PC showed the new min/max about 11 ms after the
            # write and the OLD values when read straight back (2026-10-10 diagnostics), so poll before judging.
            deadline = time.monotonic() + 2.0
            while True:
                bad = {os.path.basename(d): (_read(os.path.join(d, "scaling_min_freq")), _read(os.path.join(d, "scaling_max_freq")))
                       for d in pols
                       if (_read(os.path.join(d, "scaling_min_freq")), _read(os.path.join(d, "scaling_max_freq"))) != (str(khz), str(khz))}
                if not bad or time.monotonic() > deadline:
                    break
                time.sleep(0.02)
            if not bad:
                break
            self.log(f"cpu clock: attempt {attempt} did not take within 2 s on {bad}" + ("; retrying" if attempt == 1 else ""))
        if bad:
            raise EnvError(f"cpu clock did not take (min/max read back): {bad}, wanted {khz}")

    def _apply_session_clock(self):
        """Put the clock limits back to the SESSION clock (what T_ref was measured at): the fixed value of the session plan
        (kind=pc: 2600 MHz) or, where the session left the clock alone, the limits as found at the start. Used by the gate to
        warm up at the session clock while a per-pair pin is active; the caller pins the pair's clock again afterwards."""
        pols = self._policy_dirs()
        mode, khz = self._clock_plan()
        if mode == "fixed":
            self._apply_fixed_khz(khz, pols)
            return
        snap = (self._snap.get("clock") or {}).get("limits") or {}
        ok = {d: v for d, v in snap.items() if v[0] and v[1]}
        if not ok:
            return
        want = {d: (int(v[0]), int(v[1])) for d, v in ok.items()}
        for attempt in (1, 2):
            self._set_limits_all(list(ok), {d: w[0] for d, w in want.items()}, {d: w[1] for d, w in want.items()})
            deadline = time.monotonic() + 2.0
            while True:
                bad = {os.path.basename(d): (_read(os.path.join(d, "scaling_min_freq")), _read(os.path.join(d, "scaling_max_freq")))
                       for d, w in want.items()
                       if (_read(os.path.join(d, "scaling_min_freq")), _read(os.path.join(d, "scaling_max_freq"))) != (str(w[0]), str(w[1]))}
                if not bad or time.monotonic() > deadline:
                    break
                time.sleep(0.02)
            if not bad:
                return
        raise EnvError(f"session clock limits did not come back (min/max read back): {bad}")

    def available_clocks_mhz(self):
        """Fixed clocks this platform offers (MHz, sorted floats), or None if it cannot be listed.
        PC: the verified 100 MHz list inside the hardware range. Others: scaling_available_frequencies of the first policy."""
        pols = self._policy_dirs()
        if not pols:
            return None
        lo = int(_read(os.path.join(pols[0], "cpuinfo_min_freq"), "0") or 0)
        hi = int(_read(os.path.join(pols[0], "cpuinfo_max_freq"), "0") or 0)
        if self.kind == "pc":
            return [float(m) for m in self.PC_VALID_CLOCKS_MHZ if lo <= m * 1000 <= hi]
        raw = _read(os.path.join(pols[0], "scaling_available_frequencies"))
        if raw:
            try:
                return sorted(int(x) / 1000.0 for x in raw.split())
            except ValueError:
                pass
        return None

    def set_clock_mhz(self, mhz):
        """Pin every cpufreq policy to `mhz` (scaling_min = scaling_max, read back) WITHOUT touching turbo. Used between
        pairs by the experimenters' --freqs option. The pre-session limits are snapshotted the first time (unless the
        session clock step already did) and put back by teardown(). Returns {target_khz, limits}. Raises EnvError."""
        khz = int(round(float(mhz) * 1000))
        pols = self._policy_dirs()
        if not pols:
            raise EnvError("set_clock_mhz: no cpufreq policy directories on this platform")
        if "clock" not in self._snap:
            nt_path = self._p("/sys/devices/system/cpu/intel_pstate/no_turbo")
            self._snap["clock"] = {
                "no_turbo": _read(nt_path),
                "limits": {d: (_read(os.path.join(d, "scaling_min_freq")), _read(os.path.join(d, "scaling_max_freq"))) for d in pols},
            }
        if self.kind != "pc" and not getattr(self, "_warned_clock", False):
            self._warned_clock = True
            self.log(f"WARNING: per-pair cpu clock on kind={self.kind} is UNVERIFIED beyond the read-back check")
        self._apply_fixed_khz(khz, pols)
        self._pair_pin_khz = khz
        lim = {os.path.basename(d): [_read(os.path.join(d, "scaling_min_freq")), _read(os.path.join(d, "scaling_max_freq"))] for d in pols}
        self.clock_info = dict(getattr(self, "clock_info", None) or {}, pair_pin_khz=khz, limits_now=lim)
        return {"target_khz": khz, "limits": lim}

    def _restore_cpu_clock(self):
        errors = []
        snap = self._snap.get("clock")
        if not snap:
            return errors
        try:
            ok = {d: (lo, hi) for d, (lo, hi) in snap["limits"].items() if lo and hi}
            self._set_limits_all(list(ok), {d: int(v[0]) for d, v in ok.items()}, {d: int(v[1]) for d, v in ok.items()})
        except Exception as e:
            errors.append(str(e))
        try:
            nt_path = self._p("/sys/devices/system/cpu/intel_pstate/no_turbo")
            if snap["no_turbo"] is not None and os.path.exists(nt_path):
                self._write(nt_path, snap["no_turbo"])
        except Exception as e:
            errors.append(str(e))
        # read back: the limits must be what the session started with (the kernel applies them asynchronously, so poll)
        try:
            deadline = time.monotonic() + 2.0
            while True:
                bad = {os.path.basename(d): (_read(os.path.join(d, "scaling_min_freq")), _read(os.path.join(d, "scaling_max_freq")))
                       for d, (lo, hi) in snap["limits"].items() if lo and hi
                       and (_read(os.path.join(d, "scaling_min_freq")), _read(os.path.join(d, "scaling_max_freq"))) != (lo, hi)}
                if not bad or time.monotonic() > deadline:
                    break
                time.sleep(0.02)
            if bad:
                errors.append(f"cpu clock limits not restored (min/max now): {bad}")
        except Exception as e:
            errors.append(str(e))
        return errors

    def read_cur_freqs(self):
        """scaling_cur_freq (kHz) of every cpufreq policy. Only meaningful on a BUSY core: idle cores report stale values."""
        out = []
        for d in self._policy_dirs():
            try:
                out.append(int(_read(os.path.join(d, "scaling_cur_freq"))))
            except (TypeError, ValueError):
                pass
        return out

    def _set_fan_max(self):
        f = self.fan
        zone = f["zone"]
        self._snap["fan"] = {
            "pwm1": _read(f["pwm1"]),
            "enable": _read(f["enable"]),
            "zone": zone,
            "zone_policy": _read(os.path.join(zone, "policy")) if zone else None,
        }
        # order as in the verified shell history: kernel policy off, manual mode, then full PWM
        if zone and os.path.exists(os.path.join(zone, "policy")):
            self._write(os.path.join(zone, "policy"), "user_space")
        if os.path.exists(f["enable"]):
            self._write(f["enable"], "1")
        self._write(f["pwm1"], "255")
        # verify: the kernel's default policy would stop the fan within seconds (seen on DUT-Pi5)
        deadline = time.monotonic() + 30
        ok = False
        while time.monotonic() < deadline:
            time.sleep(1)
            rpm = self.read_rpm()
            if rpm is not None and rpm >= self.fan_min_rpm:
                ok = True
                break
        if ok:
            time.sleep(5)
            rpm2 = self.read_rpm()
            ok = rpm2 is not None and rpm2 >= self.fan_min_rpm
        if not ok:
            raise EnvError(f"fan did not reach/hold >= {self.fan_min_rpm} rpm (last {self.read_rpm()}); not proceeding")
        self.log(f"fan: manual, pwm 255, {self.read_rpm()} rpm (zone policy user_space)")

    def _confirm_external_fan(self):
        if self.fan_confirmed:
            self.log("external fan: confirmed by flag")
            return
        msg = "external FAN RUNNING (own supply)? [y/n]: "
        if sys.stdin.isatty():
            if input(msg).strip().lower() not in ("y", "yes"):
                raise EnvError("external fan not confirmed")
        else:
            raise EnvError("external fan not confirmed; pass fan_confirmed=True / --fan-confirmed for unattended runs")

    def measure_t_ref(self):
        """Poll until the temperature trend is flat and quiet, then T_ref = MEDIAN of the window.

        History: rule 1 (max-min <= 0.5 C over 30 s) never converged on DUT-CM5 in 10 min; rule 2 (half-window
        means within 0.3 C) converged but was partly luck, because the CM5 sensor has 0.55 C steps and about
        0.7 C sample noise, so a 60 s window cannot resolve a drift that small. Rule 3 (this one): least-squares
        slope over a longer window must be <= ref_slope_c_per_min. T_ref itself only needs to be roughly
        right (the gate target is T_ref + 5 C), so the point is to reject a board that is still warming up
        or cooling, not to measure it to a fraction of a degree. The window noise is logged and recorded.
        A second condition (rule 4) was added after DUT-Mac: the window std must also be <= ref_max_std_c,
        because a background transient (36.9 -> 49.5 C) gave slope +0.73 C/min, which passed rule 3 alone.
        Window medians, not means, are used because DUT-PC showed a start-up spike in the first sample."""
        if self.read_temp() is None:
            self.log("T_ref: no temperature sensor on this platform")
            return None
        n = max(6, int(round(self.ref_window_s / self.ref_poll_s)))
        win = collections.deque(maxlen=n)   # (monotonic_time, temp)
        start = time.monotonic()
        last_log = 0.0
        last_std_log = -1e9

        def slope_of(pts):
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            den = sum((x - mx) ** 2 for x in xs)
            return 60.0 * sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0

        def std_of(vals):
            m = sum(vals) / len(vals)
            return (sum((x - m) ** 2 for x in vals) / len(vals)) ** 0.5

        while True:
            t0 = time.monotonic()
            t = self.read_temp()
            now = time.monotonic()
            win.append((now, t))
            sl = slope_of(list(win)) if len(win) == n else None
            if now - last_log > 15:
                self.log(f"T_ref wait: {t:.2f} C; window {len(win)}/{n}; slope "
                         f"{'n/a' if sl is None else f'{sl:+.2f}'} C/min (need |slope| <= {self.ref_slope_c_per_min}); "
                         f"{now - start:.0f} s elapsed")
                last_log = now
            if sl is not None and abs(sl) <= self.ref_slope_c_per_min:
                wstd = std_of([p[1] for p in win])
                if wstd <= self.ref_max_std_c:
                    self.t_ref_converged = True
                    break
                if now - last_std_log > 15:
                    self.log(f"T_ref: slope ok but window std {wstd:.2f} C > {self.ref_max_std_c} C "
                             f"(a transient is in the window); waiting")
                    last_std_log = now
            if now - start > self.ref_max_wait_s:
                self.t_ref_converged = False
                self.log("T_ref: NOT converged within max wait; proceeding (flagged)")
                break
            # fixed poll period: subtract the time the sensor read itself took (the Mac's ismc call is ~0.2 s)
            time.sleep(max(0.0, self.ref_poll_s - (time.monotonic() - t0)))
        w = [p[1] for p in win]
        std = std_of(w)
        sw = sorted(w)
        self.t_ref = (sw[len(sw) // 2] + sw[(len(sw) - 1) // 2]) / 2.0   # median: robust to start-up spikes
        self.t_ref_noise = {"n": len(w), "min": min(w), "max": max(w), "std": std,
                            "slope_c_per_min": slope_of(list(win)) if len(w) > 2 else None,
                            "window_s": self.ref_window_s, "poll_s": self.ref_poll_s,
                            "waited_s": round(time.monotonic() - start, 1)}
        self.t_target = self.t_ref + self.target_offset_c
        self.log(f"T_ref={self.t_ref:.2f} C (converged={self.t_ref_converged}); "
                 f"window min/max/std = {min(w):.2f}/{max(w):.2f}/{std:.2f}; gate target={self.t_target:.2f} C")
        return self.t_ref

    # ------------------------------------------------------------------ per-run thermal gate
    def prepare_iteration(self):
        if self.t_target is None:
            raise EnvError("T_ref not measured; call setup() first")
        return self.reach_target(self.t_target)

    def reach_target(self, target):
        """Bring the board to `target` C, always approaching from above.
        Below target: burn (at least burn_min_s, until the filtered reading >= target + overshoot), then cool.
        Above target: cool. Every decision uses the MEDIAN of the last gate_filter_s seconds, not a single
        sample: on DUT-PC a single-sample test ended the burn after 0.16 s because the package sensor spiked
        at the start of the burn (and the first reading after pinning the governor was 34 C against a resting
        26 C), so the board was never warmed. The median also removes the +/-1 sensor-step jitter seen on the
        Pi 5 family.
        Returns {'met','waited_s','burned','burn_s','burn_peak','start_temp','end_temp','end_temp_raw'}."""
        t = self.read_temp()
        res = {"met": False, "waited_s": 0.0, "burned": False, "burn_s": 0.0, "burn_peak": None,
               "start_temp": t, "end_temp": t, "end_temp_raw": t, "burn_freq_khz": None}
        if t is None:
            self.log("gate: no temperature sensor; cannot gate")
            return res
        t0 = time.monotonic()
        deadline = t0 + self.gate_timeout_s
        buf = collections.deque()

        def sample():
            now = time.monotonic()
            raw = self.read_temp()
            buf.append((now, raw))
            while buf and now - buf[0][0] > self.gate_filter_s:
                buf.popleft()
            vals = sorted(x[1] for x in buf)
            return now, raw, vals[len(vals) // 2]

        try:
            filt = t
            if t < target:
                res["burned"] = True
                pin = self._pair_pin_khz
                if pin:
                    self._apply_session_clock()       # warm up at the session clock; dropped to the pin when the burn ends
                self._burn_start()
                burn_t0 = time.monotonic()
                goal = min(target + self.burn_overshoot_c, self.abort_temp_c - 1.0)
                peak = filt
                last_burn_log = 0.0
                hist = collections.deque()
                while time.monotonic() < deadline:
                    time.sleep(self.poll_s)
                    now, raw, filt = sample()
                    peak = max(peak, filt)
                    if now - last_burn_log > 1.0:
                        self.log(f"gate: warming up {filt:.2f} C (raw {raw:.2f}) -> {goal:.2f} C, {now - burn_t0:.0f} s")
                        last_burn_log = now
                    if raw >= self.abort_temp_c:
                        self.log(f"gate: ABORT burn, raw reading {raw:.1f} C")
                        break
                    if now - burn_t0 >= max(self.burn_min_s, self.gate_filter_s) and filt >= goal:
                        break
                    hist.append((now, filt))
                    while hist and now - hist[0][0] > self.plateau_window_s:
                        hist.popleft()
                    if now - burn_t0 >= self.burn_max_s and filt > target:
                        # DUT-CM5 2026-10-13: with the fan on, a full burn plateaued at 30.35 C, below target + 3 (31.2 C),
                        # so the burn ran to the 600 s timeout. Above target is enough to start cooling from above.
                        self.log(f"gate: burn plateau {filt:.2f} C below the goal {goal:.2f} C after {now - burn_t0:.0f} s; cooling from above")
                        break
                    if (now - burn_t0 >= self.plateau_window_s and hist and now - hist[0][0] >= self.plateau_window_s - 2.0
                            and filt - hist[0][1] < self.plateau_rise_c):
                        # DUT-Pi5 2026-10-20 (clock pinned at 1600 MHz, fan at full): the burn settled at 32 C, below the
                        # target of 33.7 C, and ran on to the 600 s timeout. A burn that has stopped rising cannot get there:
                        # end it. The board is then below target, so the gate is met and the run starts at its plateau.
                        res["plateau_below_target"] = True
                        self.log(f"gate: burn plateau {filt:.2f} C (rose {filt - hist[0][1]:.2f} C in {now - hist[0][0]:.0f} s), target {target:.2f} C "
                                 f"not reachable at this clock after {now - burn_t0:.0f} s; stopping the burn")
                        break
                res["burn_s"] = time.monotonic() - burn_t0
                res["burn_peak"] = peak
                fr = self.read_cur_freqs()  # all cores are busy here, so these are meaningful
                res["warm_freq_khz" if pin else "burn_freq_khz"] = {"min": min(fr), "max": max(fr)} if fr else None
                self._burn_stop()
                if pin:
                    self._apply_fixed_khz(pin, self._policy_dirs())     # now the pair's clock; cooling below runs at it
            if self.measure_clock and self._policy_dirs():
                # the clock the cores really run at under load at THIS run's setting, also when the board was warm enough to need no
                # warm-up burn (idle cores report stale values, so they have to be busy to be read)
                # ONE busy process, like the keygen it stands for: an all-core burn is power-limited on the PC (N97: about 2.7 GHz
                # on four cores) and would read lower than the clock one busy core holds at the pin (DUT-PC smoke 2026-10-20).
                # The maximum over the policies is the busy core's; idle cores report stale lower values.
                self._burn_start(1)
                time.sleep(self.measure_clock_s)
                fr = self.read_cur_freqs()
                self._burn_stop()
                res["burn_freq_khz"] = {"min": min(fr), "max": max(fr)} if fr else None
            cool_t0 = time.monotonic()
            last_log = 0.0
            while time.monotonic() < deadline:
                now, raw, filt = sample()
                if filt <= target and now - cool_t0 >= self.gate_filter_s:
                    break
                if now - last_log > 1.0:
                    self.log(f"gate: {filt:.2f} C (raw {raw:.2f}) -> {target:.2f} C")
                    last_log = now
                time.sleep(self.poll_s)
            res["end_temp"], res["end_temp_raw"] = filt, raw
        finally:
            self._burn_stop()
        res["waited_s"] = time.monotonic() - t0
        res["met"] = res["end_temp"] is not None and res["end_temp"] <= target
        if res["met"]:
            self.log(f"gate met: {res['end_temp']:.1f} C <= {target:.1f} C after {res['waited_s']:.0f} s" + (" (cooled after a warm-up burn)" if res.get("burned") else ""))
        if not res["met"]:
            self.log(f"gate: TIMEOUT after {res['waited_s']:.0f} s at {res['end_temp']} C (target {target:.2f})")
        return res

    def _burn_start(self, n=None):
        """Start n busy-loop processes (default: one per CPU)."""
        if self._burners:
            return
        n = n or os.cpu_count() or 1
        for _ in range(n):
            self._burners.append(subprocess.Popen(
                [sys.executable, "-c", "while True: pass"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    def _burn_stop(self):
        for p in self._burners:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        self._burners = []

    # ------------------------------------------------------------------ teardown
    def teardown(self):
        if self._torn_down or not self._setup_done:
            return
        self._torn_down = True
        self._burn_stop()
        errors = []
        fan = self._snap.get("fan")
        if self.fan and fan:
            for path, val in ((self.fan["pwm1"], fan["pwm1"]), (self.fan["enable"], fan["enable"])):
                try:
                    if val is not None and os.path.exists(path):
                        self._write(path, val)
                except Exception as e:
                    errors.append(str(e))
            try:
                if fan["zone"] and fan["zone_policy"]:
                    self._write(os.path.join(fan["zone"], "policy"), fan["zone_policy"])
            except Exception as e:
                errors.append(str(e))
        for f, val in self._snap.get("governor", {}).items():
            try:
                if val:
                    self._write(f, val)
            except Exception as e:
                errors.append(str(e))
        errors += self._restore_cpu_clock()
        errors += self._quiesce_restore()
        errors += self._time_service_restore()
        self.log("teardown: restored governor/fan/thermal policy/cpu clock and background jobs" + (f" (ERRORS: {errors})" if errors else ""))

    def _install_handlers(self):
        atexit.register(self.teardown)

        def handler(signum, frame):
            self.teardown()
            sys.exit(128 + signum)

        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(s, handler)
            except ValueError:
                pass  # not main thread

    # ------------------------------------------------------------------ reporting
    def describe(self):
        """Goes into the run record's `extra` JSON as `env_controls`."""
        return {
            "library_version": PLATFORM_ENV_VERSION,
            "kind": self.kind,
            "verified_kind": self.kind in self.VERIFIED_KINDS,
            "model": self.model,
            "governor": {"controllable": bool(self._governor_files()),
                         "files": len(self._governor_files())},
            "cpu_freq_after_pin": getattr(self, "freq_after_pin", None),
            "intel_no_turbo": self.read_no_turbo(),
            "cpu_clock": getattr(self, "clock_info", None) or {"requested": self.cpu_clock_req, "mode": self._clock_plan()[0]},
            "fan_mode": self.fan_mode(),
            "fan_dir": self.fan["dir"] if self.fan else None,
            "temp_sensor": ("ismc:TCMb (CPU Die Average)" if self.kind == "mac" else
                            (os.path.basename(self.zone_path) + ":" + (_read(os.path.join(self.zone_path, "type"), ""))
                             if self.zone_path else None)),
            "mac_die_max_c": getattr(self, "mac_die_max", None),
            "throttle_probe": self.throttle_state()["detail"],
            "radios": self._found("radios_found", self.radio_state),
            "background_found": self._found("background_found", self.background_state),
            "quiesced": {"linux_units": (self._snap.get("quiesce") or {}).get("units", []),
                         "mac_update_check_paused": False},
            "t_ref_c": self.t_ref,
            "t_ref_converged": self.t_ref_converged,
            "t_ref_noise": getattr(self, "t_ref_noise", None),
            "t_target_c": self.t_target,
            "time_sync": self.sync_info,
        }


# ---------------------------------------------------------------------- CLI self-tests
def _main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="store_true", help="print the library version and exit")
    ap.add_argument("--selftest", action="store_true", help="read-only detection, no changes")
    ap.add_argument("--fan-test", action="store_true", help="set fan to max, trace rpm 40 s, restore")
    ap.add_argument("--gate-test", action="store_true", help="setup, one thermal gate, restore")
    ap.add_argument("--sync-test", action="store_true",
                    help="run sync_clock() three times (sync, then stop the time daemon), print the results, restore the daemon")
    ap.add_argument("--ntp-server", default="192.168.55.100", help="time reference (JUMP)")
    ap.add_argument("--fan-confirmed", action="store_true")
    ap.add_argument("--sysroot", default="/")
    ap.add_argument("--cpu-clock", default="default",
                    help="default (2600 MHz on DUT-PC, unchanged elsewhere) | turbo | noturbo | <MHz>, e.g. 2000")
    a = ap.parse_args()
    if a.version:
        print("platform_env.py", PLATFORM_ENV_VERSION)
        return
    print("platform_env.py", PLATFORM_ENV_VERSION)

    env = PlatformEnvironment(sysroot=a.sysroot, fan_confirmed=a.fan_confirmed, cpu_clock=a.cpu_clock, ntp_server=a.ntp_server)
    if a.sync_test:
        # needs root (starts/stops the time daemon); does not touch governor, fan or clock pin
        env._check_privileges()
        try:
            for i in range(3):
                print(json.dumps(env.sync_clock(), indent=2))
                time.sleep(2)
        finally:
            errs = env._time_service_restore()
            print("restored time daemon" + (f" (ERRORS: {errs})" if errs else ""))
        return
    if a.selftest or not (a.fan_test or a.gate_test):
        print(json.dumps(env.describe(), indent=2))
        print("temp now:", env.read_temp(), "C; fan rpm:", env.read_rpm())
        if env.kind == "mac":
            t0 = time.monotonic()
            for _ in range(5):
                env.read_temp()
            print(f"mac sensor read time: {(time.monotonic() - t0) / 5 * 1000:.0f} ms per ismc call (5 calls)")
        return
    try:
        env.setup(measure_ref=a.gate_test)
        if a.fan_test:
            for i in range(20):
                time.sleep(2)
                print(f"t+{(i+1)*2}s rpm={env.read_rpm()} temp={env.read_temp()}")
        if a.gate_test:
            print(json.dumps(env.prepare_iteration(), indent=2))
            # rebound check: does the sensor climb back up after reaching the gate target while idle?
            print("post-gate trace (1 Hz, 30 s, idle): " +
                  " ".join(f"{env.read_temp():.2f}" for _ in range(30) if not time.sleep(1)))
            print(json.dumps(env.describe(), indent=2))
    finally:
        env.teardown()


if __name__ == "__main__":
    try:
        _main()
    except EnvError as e:
        # the message has already been printed (e.g. by require_root); no traceback for the operator
        if "must be run as root" not in str(e):
            print("ERROR:", e)
        sys.exit(1)
