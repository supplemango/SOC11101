#!/usr/bin/env python3
# VERSION v20261014.2301   (vYYYYMMDD.HHMM, UK local time of the last edit; bump on every change)
"""
e2_common.py -- pieces shared by calibrate_e2.py and experimenter_e2.py (so both read the same conditions and time the
same keygen call, which is what lets a calibration number carry over to the experiment).

  load_conditions(path)        conditions.csv -> list of dicts (label, openssl_args, unit, execute_order)
  sort_for_execution(conds)    execute_order: first, then shuffled (file order here), then last
  openssl_path(arg)            --openssl PATH, else the testbed's 3.5.8 build, else the first openssl found
  openssl_version(exe, env)    the first line of `openssl version`
  keygen_cmd(exe, openssl_args)  the one keygen command line: openssl genpkey <args> -out /dev/null
  time_one_key(cmd, env, timeout)  run it once, return (seconds, returncode, stderr tail); seconds None on timeout
  load_calibration(path) / save_calibration(path, rows, order)   calibration_<platform>.csv
  missing_calibration(conds, cal) / calibration_error_text(missing)

conditions.csv columns: label,openssl_args,unit,execute_order   (execute_order: first | shuffled | last)
calibration_<platform>.csv columns: label,seconds_per_key,keys_measured   (<platform> = the hostname, e.g. DUT-Pi5)
Both live next to the experimenters (~/pqc/E2) and are tracked in git.
"""
import csv
import os
import shlex
import socket
import subprocess
import sys
import time

E2_COMMON_VERSION = "v20261014.2301"

OPENSSL_EXPECTED = "3.5.8"                      # the benchmark build; anything else is refused
DEFAULT_OPENSSL = "/usr/local/ssl/bin/openssl"
ORDER_RANK = {"first": 0, "shuffled": 1, "last": 2}
CONDITION_COLUMNS = ["label", "openssl_args", "unit", "execute_order"]
CALIBRATION_COLUMNS = ["label", "seconds_per_key", "keys_measured"]


def platform_name():
    return socket.gethostname()


def calibration_path(folder, host=None):
    return os.path.join(folder, "calibration_%s.csv" % (host or platform_name()))


def load_conditions(path):
    """Read conditions.csv. Raises ValueError with a plain message on anything wrong."""
    try:
        with open(path, newline="") as f:
            rd = csv.DictReader(f)
            if rd.fieldnames is None or [c.strip() for c in rd.fieldnames] != CONDITION_COLUMNS:
                raise ValueError("%s: header must be %s, found %s" % (path, ",".join(CONDITION_COLUMNS), rd.fieldnames))
            rows = [{k: (v or "").strip() for k, v in r.items()} for r in rd]
    except OSError as e:
        raise ValueError("cannot read %s: %s" % (path, e))
    seen = set()
    for i, r in enumerate(rows, 2):
        if not r["label"] or not r["openssl_args"]:
            raise ValueError("%s line %d: label and openssl_args must not be empty" % (path, i))
        if r["label"] in seen:
            raise ValueError("%s line %d: duplicate label %s" % (path, i, r["label"]))
        seen.add(r["label"])
        if r["execute_order"] not in ORDER_RANK:
            raise ValueError("%s line %d: execute_order must be first, shuffled or last, not %r" % (path, i, r["execute_order"]))
    if not rows:
        raise ValueError("%s has no conditions" % path)
    return rows


def sort_for_execution(conds):
    """Stable: first, then shuffled, then last (file order within each)."""
    return sorted(conds, key=lambda c: ORDER_RANK[c["execute_order"]])


def openssl_path(arg=None):
    if arg:
        return arg
    if os.path.isfile(DEFAULT_OPENSSL):
        return DEFAULT_OPENSSL
    import shutil
    return shutil.which("openssl")


def openssl_version(exe, env=None):
    """First line of `openssl version` (None if it cannot be run)."""
    try:
        r = subprocess.run([exe, "version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, timeout=30)
        return r.stdout.decode("utf-8", "replace").strip().splitlines()[0] if r.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return None


def keygen_cmd(exe, openssl_args):
    """THE keygen command line, used for every timed key (calibration and experiment). The key is written to
    /dev/null: only the generation time is of interest."""
    return [exe, "genpkey"] + shlex.split(openssl_args) + ["-out", os.devnull]


def time_one_key(cmd, env=None, timeout=None):
    """Run one keygen. Returns (seconds, returncode, stderr_tail). seconds is None when the timeout expired (the
    process is killed). Wall time of the whole process: it includes the process start-up (about 5 ms on the Mac,
    about 29 ms on DUT-RISCV), the same in the experiment as here."""
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, -9, "timeout"
    except OSError as e:
        return time.monotonic() - t0, 127, str(e)
    dt = time.monotonic() - t0
    return dt, p.returncode, p.stderr.decode("utf-8", "replace").strip()[-300:]


def load_calibration(path):
    """{label: (seconds_per_key, keys_measured)}; {} when the file does not exist. Raises ValueError if unreadable."""
    if not os.path.exists(path):
        return {}
    out = {}
    try:
        with open(path, newline="") as f:
            rd = csv.DictReader(f)
            if rd.fieldnames is None or [c.strip() for c in rd.fieldnames] != CALIBRATION_COLUMNS:
                raise ValueError("%s: header must be %s" % (path, ",".join(CALIBRATION_COLUMNS)))
            for i, r in enumerate(rd, 2):
                try:
                    spk, n = float(r["seconds_per_key"]), int(r["keys_measured"])
                except (TypeError, ValueError):
                    raise ValueError("%s line %d: seconds_per_key / keys_measured not numeric" % (path, i))
                if spk <= 0 or n < 1:
                    raise ValueError("%s line %d: seconds_per_key must be > 0 and keys_measured >= 1" % (path, i))
                out[r["label"].strip()] = (spk, n)
    except OSError as e:
        raise ValueError("cannot read %s: %s" % (path, e))
    return out


def save_calibration(path, rows, order):
    """Write the whole file atomically. rows = {label: (seconds_per_key, keys_measured)}; labels in `order` come first
    (in that order), any other labels already in the file follow."""
    labels = [l for l in order if l in rows] + [l for l in rows if l not in order]
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(CALIBRATION_COLUMNS)
        for l in labels:
            spk, n = rows[l]
            w.writerow([l, "%.6g" % spk, n])
    os.replace(tmp, path)


def missing_calibration(conds, cal):
    return [c["label"] for c in conds if c["label"] not in cal]


def calibration_error_text(missing, folder_hint="~/pqc/E2"):
    return ("Calibration required for: %s\nRun:  sudo python3 %s/calibrate_e2.py" % (", ".join(missing), folder_hint))
