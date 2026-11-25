"""
Module Name: collector.py

Author: Cameron Patterson (original Windows-based responder.py, see Patterson2025),
        adapted for headless Linux operation on a dedicated Raspberry Pi 5 collector
        (UnitC, static address 192.168.55.200) as part of this project's testbed.
Date First Adapted: 2026-09-18
Version: see COLLECTOR_VERSION below (vYYYYMMDD.HHMM, UK local time of the last edit; bump on every change)

Description:
------------
** Ensure the Power and PD switches on the TC66 meter are turned OFF before testing **

This is a Python script/program which runs on the collecting device (the Pi 5 collector,
UnitC) for this project's energy benchmarking programme. It is a Linux port of Cameron
Patterson's original responder.py, adapted to run headlessly on Ubuntu Server 24.04 LTS.

It runs as a persistent service (Ctrl+C to stop): the device under test (DUT) sends one UDP message at a time,
fire-and-forget, with no replies, and the Collector measures each run with the TC66C energy meter:

    GETREADY {json}   run_id, role, pair_id, algorithm, n_target, sample_period, platform, session_id,
                      schedule_index, experiment, subexp, ...   (the meter is checked and the trace file opened)
    START <run_id>    the first meter reading that BEGINS after this arrives is the start of the energy window
    STOP {json}       run_id, n_completed, dut_time_to_run_s, status, t_mono0, t_mono1, wall stamps ...
                      the first meter reading that BEGINS after this arrives is the end of the energy window
    SUMMARY {json}    run_id, t_start_c, t_stop_c, throttled, target_mhz, measured_mhz, iter_p50_s, iter_p95_s,
                      iter_max_s, iter_cv_pct, null_slip_s, flags, ... (everything else goes into Extra)

State is kept per run_id. Energy is the difference of the meter's cumulative G0+G1 mWh counters between the two
window edges, x 3.6 (so it is quantised in 3.6 J steps). The duration reported is the DUT's own monotonic time.
A row is written to AllResults.csv when the SUMMARY arrives, or --summary-timeout seconds after STOP without it
(flagged no_summary). Anything unusual is flagged in the row and printed with a leading '!!'.

Output, under <outroot>/E<Experiment> (default ~/E2, ~/E3 ...):
    1) <session>-Exp<exp><sub>-<seq>-<role>_<algorithm>-n<count>.csv   one per run (the row's Experiment_ID):
       the meter readings (about one a second from GETREADY to STOP) with a header row.
    2) AllResults.csv   one row per run, 32 columns (see COLUMNS). If an existing AllResults.csv has a different
       header (an older version) it is renamed AllResults_<date-time>.csv and a new one is started; nothing is
       ever rewritten. Extra holds a JSON object whose keys all end in _EX.

When the Collector is stopped (Ctrl+C or SIGTERM) it prints the core figures of every row it wrote in that run: grouped by
session, one line per row (Exp, slot #, platform, algorithm, role, keys, joules, seconds, status, flags, trace file name) and, under each
NULL row, the delta against its LIVE row (joules, J per key, watts). AllResults.csv itself is not touched.

Notable Dependencies:
---------------------
pyserial, pycryptodome

Usage (from --help):
--------------------
usage: collector.py [-h] [--device DEVICE_PATH] [--port PORT] [--outroot DIR] [--summary-timeout SECONDS]
                    [--nonet-delaystart SECONDS] [--nonet-duration SECONDS]

Adaptions from the original Windows responder.py:
------------------------------------------------
- Removed the Windows-only platform guard (this now runs on Ubuntu Server 24.04 LTS on the Pi collector).
- Removed the interactive COM-port picker (list_com_ports, msvcrt-based) entirely. Linux's udev gives the
  TC66C a stable device path under /dev/serial/by-id/ that survives replugging/reboots, so an interactive
  per-run picker is unnecessary. The device path is a fixed default with a command-line override instead.
- Renamed --com/com_port to --device/device_path throughout, since "COM port" is a Windows-only concept.
- The TC66C class (AES decryption, polling) is unchanged from Patterson25's original.
- Added a platform column to the results output as can be different DUTs not just Pi5 of Patterson25.
- AllResults.csv is set to RO unless being written (if the user leaves it open in an editor this stops the log
  being updated, which is bad).

Changes for E2 and later (this file's design record is in the project: collector-design-notes.md):
-----------------------------------------------------------------------------------------------
- Persistent service, one meter object opened once at startup (no new TC66C per GETREADY), state per run_id.
- No replies, duplicates ignored, START/STOP/SUMMARY without their GETREADY handled and flagged (no_getready,
  no_start, no_stop, no_summary, aborted_by_new_getready, duration_mismatch ...).
- Energy window from START/STOP-triggered readings; DUT monotonic duration reported; SUMMARY message added.
- 32-column AllResults.csv per experiment folder (the two per-1000 columns were dropped); per-run trace file names
  now carry date-time, DUT, experiment, sequence, role, algorithm and count.
- UDP only (the unused TCP stub was removed); datagrams up to 64 KB are accepted (SUMMARY can be several KB).
- The meter's input buffer is flushed before every poll (a late reply can no longer be read as the next one).
- TODO: Only a single TC66C/device is supported (see the dissertation design, Section 3.5).

Included licence notice from TheHWCave library below this section.
Started collecting data from G0 and G1 channels as seems to flip flop between (1-at-a-time so aggregating both).
Sanity Check: if a negative cumulative mWh is seen then it is corruption on the serial connection, so the reading
is treated as failed.

"""

# Includes Code from HWCave for the Ruideng RDTech TC66[c] https://github.com/TheHWcave/TC66/tree/main
# Data recovery from the TC66C energy meter is based on work by theHWcave and subject to Licence from TheHWcave Code which is used...
#
# MIT License
#
# Copyright (c) 2022 TheHWcave
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
#
#

import os
import stat
import struct
import socket
import threading
import json
import time
from datetime import datetime
import sys
import argparse
import serial
import math
import csv
import re
import signal
from collections import deque
from Crypto.Cipher import AES
from collections import namedtuple

# --- Version (vYYYYMMDD.HHMM, UK local time of the last edit; bump on every change) ---
COLLECTOR_VERSION = "v20261016.1410"

# --- Configuration ---
HOST = "0.0.0.0"  # Listen on all available interfaces (the collector itself is statically addressed
                   # at 192.168.55.200 on VLAN 55, port 7 of the testbed switch - see the addressing scheme)
PORT = 65432  # UDP only
outroot = os.path.expanduser("~")  # output goes to <outroot>/E<Experiment>
summary_timeout = 10.0  # seconds after STOP before a row is written without a SUMMARY
MIN_POLL_PERIOD = 0.1  # a TC66C read takes about 0.1 s (measured 101 ms), so never ask for faster than that
RESULTS_NAME = "AllResults.csv"

# Default TC66C device path. This is the stable udev by-id symlink rather than /dev/ttyACM0, since the
# by-id path survives replugging the meter or rebooting the Pi, whereas the ttyACMx enumeration can shift
# if other USB-serial devices are ever plugged in alongside it.
device_path = "/dev/serial/by-id/usb-RuiDeng_TC66_USB_Type-C_Meter_TC66-if00"

# AllResults.csv columns, in order. S1..S9 are never written to: they are blank padding so that Extra stays at
# column 32 (a new column replaces the next free S slot; a file with a different header is an older version).
COLUMNS = [
    "Timestamp", "Platform", "Experiment", "SubExp", "Iterations", "Algorithm", "Role", "Experiment_ID",
    "TotalJoules", "DurationSeconds", "IterP50Sec", "IterP95Sec", "IterMaxSec", "IterCV", "NullSlipSec",
    "StartTempC", "StopTempC", "Throttled", "TargetMHz", "MeasuredMHz", "Status", "Flags",
    "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "Extra",
]

# Per-run trace file header (unchanged from v9.23)
TRACE_HEADER = ["Timestamp", "Platform", "Iterations", "Algorithm", "Experiment_ID", "Sample_Period", "Joules", "Volt",
                "Current", "Power", "Resistance", "G0_mAh", "G1_mAh", "Temp", "D_plus", "D_minus"]

# Keys the Collector turns into columns (or deliberately does not repeat). Every OTHER key a DUT sends in
# GETREADY, STOP or SUMMARY is kept in Extra as "<key>_EX".
CONSUMED_KEYS = {
    "run_id", "session_id", "schedule_index", "role", "platform", "algorithm", "experiment", "subexp",
    "n_completed", "dut_time_to_run_s", "status", "t_start_c", "t_stop_c", "throttled", "target_mhz",
    "measured_mhz", "flags", "iter_p50_s", "iter_p95_s", "iter_max_s", "iter_cv_pct", "null_slip_s",
}


# --- TC66C Class (Extracted + Adapted from 3rd party code, licence and links above)
class TC66C:
    _SIF: None
    _AES: None

    PollData = namedtuple(
        "PollData",
        [
            "Name",
            "Version",
            "SN",
            "Runs",
            "Volt",
            "Current",
            "Power",
            "Resistance",
            "G0_mAh",
            "G0_mWh",
            "G1_mAh",
            "G1_mWh",
            "Temp",
            "D_plus",
            "D_minus",
        ],
    )

    RecData = namedtuple("RecData", ["Volt", "Current"])

    def __init__(self, port_dev=None):
        STATIC_KEY = [
            0x58,
            0x21,
            0xFA,
            0x56,
            0x01,
            0xB2,
            0xF0,
            0x26,
            0x87,
            0xFF,
            0x12,
            0x04,
            0x62,
            0x2A,
            0x4F,
            0xB0,
            0x86,
            0xF4,
            0x02,
            0x60,
            0x81,
            0x6F,
            0x9A,
            0x0B,
            0xA7,
            0xF1,
            0x06,
            0x61,
            0x9A,
            0xB8,
            0x72,
            0x88,
        ]

        if port_dev is None:
            port_dev = device_path
        try:
            self._SIF = serial.Serial(
                port=port_dev,
                baudrate=115200,
                bytesize=8,
                parity="N",
                stopbits=2,
                xonxoff=0,
                rtscts=0,
                dsrdtr=1,
                timeout=5,
            )
            time.sleep(1.0)
        except serial.SerialException as e:
            print("failed to open:" + port_dev)
            print(e)
            sys.exit(1)

        self._AES = AES.new(bytes(STATIC_KEY), AES.MODE_ECB)

    def Poll(self):
        """
        Polls the TC66C for new data and returns it in form of
        a PollData record

        The data comes in a 192 byte package AES encrypted
        """
        if not self._SIF.isOpen():
            self._SIF.open()
        self.SendCmd("getva")

        buf = self._SIF.read(192)
        try:
            data = self._AES.decrypt(buf)
        except Exception as e:
            print("decrypt error")
            return None

        pac1 = struct.unpack("<4s4s4s13I", data[0:64])
        pac2 = struct.unpack("<4s15I", data[64:128])
        pac3 = struct.unpack("<4s15I", data[128:192])

        if pac2[6] == 1:
            tsign = -1
        else:
            tsign = 1

        pd = self.PollData(
            Name=pac1[1].decode(),
            Version=pac1[2].decode(),
            SN=pac1[3],
            Runs=pac1[11],
            Volt=float(pac1[12]) * 1e-4,
            Current=float(pac1[13]) * 1e-5,
            Power=float(pac1[14]) * 1e-4,
            Resistance=float(pac2[1]) * 1e-1,
            G0_mAh=pac2[2],
            G0_mWh=pac2[3],
            G1_mAh=pac2[4],
            G1_mWh=pac2[5],
            Temp=pac2[7] * tsign,
            D_plus=float(pac2[8]) * 1e-2,
            D_minus=float(pac2[9]) * 1e-2,
        )

        return pd

    def GetRec(self):
        """
        Fetches the complete used recording buffer (up to 1440 entries)
        from the TC66C and returns it in from of a list of RecData
        Each RecData entry is a Volt , Current pair
        """
        rd = []
        if not self._SIF.isOpen():
            self._SIF.open()
        self.SendCmd("gtrec")
        rec = bytearray()
        while True:
            buf = self._SIF.read(8)

            if len(buf) == 0:
                break
            rec.extend(buf)
            if len(rec) >= 8:
                r = struct.unpack("<2I", rec[0:8])
                rd_entry = self.RecData(
                    Volt=float(r[0]) * 1e-4, Current=float(r[1]) * 1e-5
                )
                rd.append(rd_entry)
                rec = rec[8:]
        return rd

    def SendCmd(self, msg):
        """
        sends a command string to the TC66C. There are only 7 valid ones (so far):

            query     response    4 bytes     'firm' or 'boot'
            getva     response 192 bytes     (see Poll function)
            gtrec     response variable       (see GetRec function)
            lastp     response    0 bytes     (previous page on the TC66 display)
            nextp     response    0 bytes     (next page on the TC66 display)
            rotat     response    0 bytes     (rotate TC66 screen)
            update    response    5 bytes     'uprdy' = prepare to load new firmware
        """
        self._SIF.write(msg.encode("ascii"))
        return



# --- Shared state -------------------------------------------------------------------------------------------
meter = None                      # the one TC66C object, opened once at startup
meter_lock = threading.Lock()     # only one thread talks to the serial port at a time
state_lock = threading.RLock()    # guards runs, sampling_run and the counters below
stop_event = threading.Event()
wake = threading.Event()          # START/STOP arrival wakes the sampler so the trigger reading happens at once
runs = {}                         # run_id -> Run, every run that has not been written to AllResults.csv yet
sampling_run = None               # the single run being sampled (GETREADY .. STOP); at most one
done_ids = deque(maxlen=500)      # run_ids already written (to recognise late duplicates)
live_rows = {}                    # pair_id -> (joules, n) of a LIVE row, for the on-screen delta on the NULL
checked_folders = set()
rows_written = 0
rows_flagged = 0
session_rows = []                 # core figures of every row written in this run of the Collector (for the summary at exit)
fail_streak = 0

out_lock = threading.Lock()
inline_active = False


# --- Screen output ------------------------------------------------------------------------------------------
def hhmmss():
    return datetime.now().strftime("%H:%M:%S")


def _wipe():
    global inline_active
    if inline_active:
        sys.stdout.write("\r\x1b[K")
        inline_active = False


def say(msg):
    with out_lock:
        _wipe()
        print(msg, flush=True)


def loud(msg):
    """Anything abnormal: a '!!' line so it stands out from the normal flow."""
    say(f"!! {hhmmss()} {msg}")


def live(msg):
    """One self-overwriting status line (terminal only)."""
    if not sys.stdout.isatty():
        return
    global inline_active
    with out_lock:
        sys.stdout.write("\r\x1b[K" + msg)
        sys.stdout.flush()
        inline_active = True


# --- Small helpers ------------------------------------------------------------------------------------------
def sanitise(text):
    """Filename-safe: anything other than letters, digits, '.' and '-' becomes '_' (runs collapse to one)."""
    return re.sub(r"[^A-Za-z0-9.\-]+", "_", str(text)).strip("_") or "x"


def num(v):
    try:
        if v is None or isinstance(v, bool):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def fmt(v, digits):
    x = num(v)
    return "" if x is None else f"{x:.{digits}f}"


def fmt_int(v):
    x = num(v)
    return "" if x is None else str(int(round(x)))


def fmt_tri(v):
    """Throttled: 1 = throttled, 0 = checked and clean, blank = could not tell."""
    if v is None or v == "":
        return ""
    if isinstance(v, str):
        low = v.strip().lower()
        if low in ("1", "true", "yes"):
            return "1"
        if low in ("0", "false", "no"):
            return "0"
        return ""
    return "1" if v else "0"


def wall_iso(dt):
    return dt.astimezone().isoformat(timespec="milliseconds")


def own(path):
    """When run with sudo, give the file back to the user who ran sudo."""
    try:
        if os.geteuid() == 0 and os.environ.get("SUDO_UID"):
            os.chown(path, int(os.environ["SUDO_UID"]), int(os.environ.get("SUDO_GID", os.environ["SUDO_UID"])))
    except OSError:
        pass


def split_flags(v):
    if not v:
        return []
    if isinstance(v, str):
        return [t.strip() for t in re.split(r"[;,]", v) if t.strip()]
    if isinstance(v, (list, tuple)):
        return [str(t) for t in v if str(t)]
    return [str(v)]


# --- One run ------------------------------------------------------------------------------------------------
class Run:
    def __init__(self, run_id):
        self.run_id = run_id
        self.params = {}              # the GETREADY fields
        self.label = run_id           # "<seq> <role>" for the screen
        self.have_getready = False
        self.stop_data = None
        self.summary = None
        self.flags = []               # the Collector's own flags
        self.wall_any = datetime.now()
        self.t_recv_start = None      # monotonic time the START / STOP datagram was received
        self.t_recv_stop = None
        self.start_edge = None        # {"mwh", "t", "wall", "lag"} of the first reading that began after START
        self.stop_edge = None
        self.no_edges = False         # START or STOP was missing: there is no valid energy window
        self.read_errors = 0
        self.last_read_end = 0.0
        self.period = 1.0
        self.trace = None
        self.trace_writer = None
        self.trace_origin_mwh = None
        self.filename = None
        self.folder = None
        self.finalized = False

    def add_flag(self, name):
        if name not in self.flags:
            self.flags.append(name)


# --- Meter ----------------------------------------------------------------------------------------------------
def meter_read():
    """One poll of the TC66C -> dict, or None if the read failed. The input buffer is flushed first so that a late
    reply from an earlier request can never be mistaken for this one."""
    try:
        with meter_lock:
            try:
                meter._SIF.reset_input_buffer()
            except Exception:
                pass
            pd = meter.Poll()
    except Exception:
        return None
    if not pd:
        return None
    d = {
        "Volt": pd.Volt, "Current": pd.Current, "Power": pd.Power, "Resistance": pd.Resistance,
        "G0_mAh": pd.G0_mAh, "G0_mWh": pd.G0_mWh, "G1_mAh": pd.G1_mAh, "G1_mWh": pd.G1_mWh,
        "Temp": pd.Temp, "D_plus": pd.D_plus, "D_minus": pd.D_minus,
    }
    # we do not know whether channel G0 or G1 is in use (only one at a time), so use both
    if d["G0_mWh"] + d["G1_mWh"] < 0:   # sanity check from v9.23: a negative total is serial corruption
        return None
    return d


# --- Files --------------------------------------------------------------------------------------------------
def build_filename(p):
    sess = sanitise(p.get("session_id", "nosession"))
    exp = sanitise(p.get("experiment", "x"))
    sub = sanitise(p.get("subexp")) if p.get("subexp") not in (None, "") else ""
    seq = p.get("schedule_index")
    try:
        seq = f"{int(seq):04d}"
    except (TypeError, ValueError):
        seq = sanitise(seq if seq is not None else "0000")
    role = sanitise(p.get("role", "RUN"))
    algo = sanitise(p.get("algorithm", "unknown"))
    try:
        n = str(int(float(p.get("n_target"))))
    except (TypeError, ValueError):
        n = "x"
    return f"{sess}-Exp{exp}{sub}-{seq}-{role}_{algo}-n{n}.csv"


def ensure_results_file(folder):
    """Make sure <folder>/AllResults.csv exists with the current header. An older/different header: the old file is
    renamed (never rewritten) and a new one started."""
    path = os.path.join(folder, RESULTS_NAME)
    if folder in checked_folders:
        return path
    header_line = ",".join(COLUMNS)
    if os.path.exists(path):
        if os.path.getsize(path) > 0:
            with open(path, "r") as f:
                first = f.readline().rstrip("\r\n")
            if first != header_line:
                new_name = f"AllResults_{datetime.now():%Y%m%d-%H%M%S}.csv"
                os.rename(path, os.path.join(folder, new_name))
                loud(f"{path} has a different (older) header: renamed to {new_name}, starting a new {RESULTS_NAME}")
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        try:
            if os.path.exists(path):
                os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
            with open(path, "w", newline="") as f:
                f.write(header_line + "\n")
            os.chmod(path, stat.S_IREAD)
            own(path)
        except OSError as e:
            loud(f"cannot create {path}: {e}")
    checked_folders.add(folder)
    return path


def write_row(folder, row):
    """Append one row; the file is read-only (as in v9.23) except while it is being written."""
    path = os.path.join(folder, RESULTS_NAME)
    try:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    except OSError as e:
        loud(f"cannot set {path} read-write: {e}")
    try:
        with open(path, "a", newline="") as f:
            csv.writer(f, lineterminator="\n").writerow(row)
        return True
    except OSError as e:
        loud(f"cannot write the row to {path}: {e}")
        return False
    finally:
        try:
            os.chmod(path, stat.S_IREAD)
        except OSError as e:
            loud(f"cannot set {path} back to read-only: {e}")
        own(path)


def open_trace(run):
    """Open the per-run trace file (named like the row's Experiment_ID)."""
    base = run.filename
    for attempt in range(100):
        name = base if attempt == 0 else base[:-4] + f"_dup{attempt}.csv"
        try:
            f = open(os.path.join(run.folder, name), "x", newline="")
        except FileExistsError:
            continue
        except OSError as e:
            loud(f"cannot open trace file {os.path.join(run.folder, name)}: {e}")
            return
        if attempt:
            run.filename = name
            run.add_flag("duplicate_file")
        run.trace = f
        run.trace_writer = csv.writer(f, lineterminator="\n")
        run.trace_writer.writerow(TRACE_HEADER)
        own(os.path.join(run.folder, name))
        return


def close_trace(run):
    if run.trace is not None:
        try:
            run.trace.close()
        except OSError:
            pass
        run.trace = None
        run.trace_writer = None


# --- Readings -----------------------------------------------------------------------------------------------
def record_reading(run, d, t_begin, t_end, wall_end):
    """Handle one successful reading for a run (called with state_lock held)."""
    global sampling_run
    mwh = d["G0_mWh"] + d["G1_mWh"]
    if run.trace_origin_mwh is None:
        run.trace_origin_mwh = mwh
    if run.trace_writer is not None:
        try:
            run.trace_writer.writerow([
                wall_end.isoformat(), run.params.get("platform", "unknown"), run.params.get("n_target", "NA"),
                run.params.get("algorithm", "NA"), run.filename, f"{run.period:.2f}",
                f"{(mwh - run.trace_origin_mwh) * 3.6:.2f}", f"{d['Volt']:.4f}", f"{d['Current']:.4f}",
                f"{d['Power']:.4f}", f"{d['Resistance']:.4f}", d["G0_mAh"], d["G1_mAh"], f"{d['Temp']:.2f}",
                f"{d['D_plus']:.4f}", f"{d['D_minus']:.4f}"])
            run.trace.flush()
        except (OSError, ValueError):
            pass
    # the first reading that BEGAN after START arrived is the start of the window ...
    if run.t_recv_start is not None and run.start_edge is None and t_begin >= run.t_recv_start:
        run.start_edge = {"mwh": mwh, "t": t_end, "wall": wall_end, "lag": t_end - run.t_recv_start}
    # ... and the first that began after STOP arrived is the end of it
    if (run.t_recv_stop is not None and run.stop_edge is None and run.start_edge is not None
            and t_begin >= run.t_recv_stop):
        run.stop_edge = {"mwh": mwh, "t": t_end, "wall": wall_end, "lag": t_end - run.t_recv_stop}
        close_trace(run)
        if sampling_run is run:
            sampling_run = None
        maybe_finalize(run)
        return
    if run.start_edge is not None and run.stop_edge is None:
        el = t_end - run.start_edge["t"]
        live(f"  Joules thus far: {(mwh - run.start_edge['mwh']) * 3.6:.1f} J, in {el:.1f} seconds, (now: {d['Power']:.1f} W)")


def sampler_loop():
    """Reads the meter about once per sample_period from GETREADY to STOP, and immediately after START and STOP."""
    global fail_streak
    while not stop_event.is_set():
        wake.clear()
        with state_lock:
            run = sampling_run
            trig = run is not None and (
                (run.t_recv_start is not None and run.start_edge is None)
                or (run.t_recv_stop is not None and run.stop_edge is None))
        if run is None:
            wake.wait(0.2)
            continue
        elapsed = time.monotonic() - run.last_read_end
        if not trig and elapsed < run.period:
            wake.wait(min(run.period - elapsed, 0.2))
            continue
        t_begin = time.monotonic()
        d = meter_read()
        t_end = time.monotonic()
        wall_end = datetime.now()
        with state_lock:
            if sampling_run is not run:      # the run ended or was aborted while we were reading: drop it
                continue
            run.last_read_end = t_end
            if d is None:
                run.read_errors += 1
                fail_streak += 1
                if fail_streak == 3:
                    loud("meter not answering (3 failed reads in a row)")
            else:
                if fail_streak >= 3:
                    say(f"{hhmmss()} meter reads recovered")
                fail_streak = 0
                record_reading(run, d, t_begin, t_end, wall_end)
        if d is None:
            time.sleep(0.1)    # do not spin if the serial port fails instantly


# --- Writing the row ----------------------------------------------------------------------------------------
def maybe_finalize(run):
    if run.finalized:
        return
    if run.summary is not None and (run.stop_edge is not None or run.no_edges):
        finalize(run)


def stop_sampling(run):
    global sampling_run
    close_trace(run)
    if sampling_run is run:
        sampling_run = None


def finalize(run):
    """Build and write the row for a run, print it, and forget the run (called with state_lock held)."""
    global rows_written, rows_flagged
    if run.finalized:
        return
    run.finalized = True
    stop_sampling(run)
    stop = run.stop_data or {}
    summ = run.summary or {}
    p = run.params

    aborted = "aborted_by_new_getready" in run.flags or "aborted" in run.flags
    joules = None
    window = None
    if run.start_edge and run.stop_edge and not aborted:
        dm = run.stop_edge["mwh"] - run.start_edge["mwh"]
        if dm < 0:
            run.add_flag("energy_invalid")
        else:
            joules = dm * 3.6
            window = run.stop_edge["t"] - run.start_edge["t"]
    dur = num(stop.get("dut_time_to_run_s"))
    n_done = stop.get("n_completed")
    if joules is not None and dur is not None and abs(window - dur) > 0.5:
        run.add_flag("duration_mismatch")

    flags = split_flags(summ.get("flags")) + [f for f in run.flags if f not in split_flags(summ.get("flags"))]

    extra = {}
    for src in (p, stop, summ):
        for k, v in src.items():
            if k not in CONSUMED_KEYS:
                extra[f"{k}_EX"] = v
    extra.update({
        "collector_version_EX": COLLECTOR_VERSION,
        "read_errors_EX": run.read_errors,
    })
    if run.start_edge:
        extra["mwh_start_EX"] = run.start_edge["mwh"]
        extra["start_lag_s_EX"] = round(run.start_edge["lag"], 3)
        extra["start_read_time_EX"] = wall_iso(run.start_edge["wall"])
    if run.stop_edge:
        extra["mwh_stop_EX"] = run.stop_edge["mwh"]
        extra["stop_lag_s_EX"] = round(run.stop_edge["lag"], 3)
        extra["stop_read_time_EX"] = wall_iso(run.stop_edge["wall"])
    if window is not None:
        extra["window_s_EX"] = round(window, 3)

    when = run.start_edge["wall"] if run.start_edge else run.wall_any
    status = stop.get("status", "")
    row = [
        wall_iso(when), p.get("platform", ""), p.get("experiment", ""), p.get("subexp", "") or "",
        "" if n_done is None else n_done, p.get("algorithm", ""), p.get("role", ""), run.filename or run.run_id,
        fmt(joules, 1), fmt(dur, 3),
        fmt(summ.get("iter_p50_s"), 6), fmt(summ.get("iter_p95_s"), 6), fmt(summ.get("iter_max_s"), 6),
        fmt(summ.get("iter_cv_pct"), 2), fmt(summ.get("null_slip_s"), 6),
        fmt(summ.get("t_start_c"), 1), fmt(summ.get("t_stop_c"), 1), fmt_tri(summ.get("throttled")),
        fmt_int(summ.get("target_mhz")), fmt_int(summ.get("measured_mhz")), status, ";".join(flags),
        "", "", "", "", "", "", "", "", "",
        json.dumps(extra, separators=(",", ":"), default=str),
    ]
    assert len(row) == len(COLUMNS)

    if not run.folder:
        # no GETREADY ever arrived, so the experiment is unknown: keep the row rather than lose it
        run.folder = os.path.join(outroot, "Eunknown")
        try:
            os.makedirs(run.folder, exist_ok=True)
            own(run.folder)
        except OSError as e:
            loud(f"cannot create {run.folder}: {e}")
    ensure_results_file(run.folder)
    ok = write_row(run.folder, row)
    runs.pop(run.run_id, None)
    done_ids.append(run.run_id)
    if ok:
        rows_written += 1
        if flags:
            rows_flagged += 1
        session_rows.append({
            "session": str(p.get("session_id") or "nosession"), "pair": p.get("pair_id"),
            "exp": f"{p.get('experiment', '')}{p.get('subexp') or ''}", "slot": p.get("schedule_index"),
            "platform": p.get("platform", ""), "algorithm": p.get("algorithm", ""), "role": str(p.get("role", "")).upper(),
            "keys": num(n_done), "joules": joules, "seconds": dur, "status": status or "-", "flags": ";".join(flags),
            "unit": summ.get("unit") or p.get("unit") or "iteration", "file": run.filename})

    # --- the screen ---
    what = "SUMMARY  " if run.summary is not None else "ROW      "
    jtxt = "-" if joules is None else f"{joules:.1f} J"
    dtxt = "-" if dur is None else f"{dur:.3f} s"
    say(f"{hhmmss()} {what} {run.label}  {jtxt}  {dtxt}  status {status or '-'}  flags {';'.join(flags) or '-'}")
    nn = num(n_done)
    unit = summ.get("unit") or p.get("unit") or "iteration"
    role = str(p.get("role", "")).upper()
    pair = p.get("pair_id")
    if joules is not None and nn:
        if role == "NULL" and pair in live_rows:
            lj, ln = live_rows.pop(pair)
            say(f"                    delta {(lj - joules) / nn:.4g} J/{unit}  (LIVE - NULL)")
        else:
            say(f"                    {joules / nn:.4g} J/{unit}  (gross, includes the idle baseline)")
        if role == "LIVE" and pair is not None:
            live_rows[pair] = (joules, nn)
            while len(live_rows) > 200:
                live_rows.pop(next(iter(live_rows)))


def check_timeouts():
    """Rows that never got their SUMMARY (or never got their closing reading) are written after summary_timeout."""
    now = time.monotonic()
    with state_lock:
        for run in list(runs.values()):
            if run.t_recv_stop is None or now - run.t_recv_stop < summary_timeout:
                continue
            if run.summary is None:
                run.add_flag("no_summary")
                loud(f"no SUMMARY for {run.label} after {summary_timeout:g} s: row written with no_summary")
            if run.stop_edge is None and not run.no_edges:
                run.add_flag("energy_missing")
                loud(f"no closing meter reading for {run.label}: energy left blank")
            finalize(run)


# --- Messages -------------------------------------------------------------------------------------------------
def on_getready(p, t_recv, wall_recv):
    global sampling_run, fail_streak
    run_id = str(p.get("run_id", "")).strip()
    if not run_id:
        loud("GETREADY without a run_id: ignored")
        return
    with state_lock:
        if run_id in runs or run_id in done_ids:
            loud(f"duplicate GETREADY {run_id}: ignored")
            return
        # anything still open from before is an anomaly: the DUT serialises runs, SUMMARY normally comes first
        for old in list(runs.values()):
            if old.t_recv_stop is None:
                old.add_flag("aborted_by_new_getready")
                loud(f"GETREADY {run_id} while {old.label} still measuring: {old.label} written as aborted_by_new_getready")
            else:
                old.add_flag("no_summary")
                loud(f"GETREADY {run_id} while {old.label} still waiting for its SUMMARY: written as no_summary")
            finalize(old)

        run = Run(run_id)
        run.params = p
        run.have_getready = True
        run.wall_any = wall_recv
        run.period = max(MIN_POLL_PERIOD, num(p.get("sample_period")) or 1.0)
        seq = p.get("schedule_index")
        try:
            seqs = f"{int(seq):04d}"
        except (TypeError, ValueError):
            seqs = str(seq) if seq is not None else "?"
        run.label = f"{seqs} {p.get('role', '?')}"
        run.filename = build_filename(p)
        run.folder = os.path.join(outroot, "E" + sanitise(p.get("experiment", "unknown")))
        try:
            os.makedirs(run.folder, exist_ok=True)
            own(run.folder)
            ensure_results_file(run.folder)
        except OSError as e:
            loud(f"cannot create the output folder {run.folder}: {e}")
            run.folder = None
        if run.folder:
            open_trace(run)

        # health check: a test reading so a dead meter is noticed before the run, not after it
        d = None
        for attempt in range(3):
            t_b = time.monotonic()
            d = meter_read()
            t_e = time.monotonic()
            wall_e = datetime.now()
            if d:
                break
            time.sleep(0.2)
        exp = f"Exp{p.get('experiment', '?')}{p.get('subexp') or ''}"
        head = f"{hhmmss()} GETREADY  {exp} {p.get('platform', '?')} {run.label} {p.get('algorithm', '?')} n{p.get('n_target', '?')}"
        if d:
            say(f"{head}   meter ok (test reading {d['Power']:.2f} W)")
            fail_streak = 0
            run.last_read_end = t_e
            record_reading(run, d, t_b, t_e, wall_e)
        else:
            say(head)
            loud(f"meter read failed at GETREADY {run.label} (3 tries)")
            run.add_flag("meter_check_failed")
            run.last_read_end = time.monotonic()
        runs[run_id] = run
        sampling_run = run
    wake.set()


def on_start(run_id, t_recv, wall_recv):
    with state_lock:
        run = runs.get(run_id)
        if run is None or not run.have_getready:
            loud(f"START without GETREADY ({run_id}): ignored")
            return
        if run.t_recv_start is not None:
            loud(f"duplicate START {run.label}: ignored")
            return
        if run.t_recv_stop is not None:
            loud(f"START after STOP {run.label}: ignored")
            return
        run.t_recv_start = t_recv
        say(f"{hhmmss()} START     {run.label}")
    wake.set()


def on_stop(p, t_recv, wall_recv):
    run_id = str(p.get("run_id", "")).strip()
    with state_lock:
        run = runs.get(run_id)
        if run is None:
            if run_id in done_ids:
                loud(f"duplicate STOP {run_id}: ignored")
                return
            # no GETREADY ever seen: keep what the DUT tells us, no energy
            run = Run(run_id)
            run.wall_any = wall_recv
            run.add_flag("no_getready")
            run.no_edges = True
            runs[run_id] = run
        elif run.t_recv_stop is not None:
            loud(f"duplicate STOP {run.label}: ignored")
            return
        run.stop_data = p
        run.t_recv_stop = t_recv
        if run.have_getready and run.t_recv_start is None:
            run.add_flag("no_start")
            run.no_edges = True
        n = p.get("n_completed")
        dur = num(p.get("dut_time_to_run_s"))
        _wipe_live()
        say(f"{hhmmss()} STOP      {run.label}  n={'-' if n is None else n}  {'-' if dur is None else f'{dur:.3f}'} s")
        say(f"{hhmmss()} waiting on SUMMARY ({summary_timeout:g} s) ...")
        if run.no_edges:
            stop_sampling(run)
        maybe_finalize(run)
    wake.set()


def _wipe_live():
    with out_lock:
        _wipe()


def on_summary(p, t_recv, wall_recv):
    run_id = str(p.get("run_id", "")).strip()
    with state_lock:
        run = runs.get(run_id)
        if run is None:
            if run_id in done_ids:
                loud(f"late SUMMARY for {run_id}: ignored (row already written)")
            else:
                loud(f"SUMMARY without a matching run ({run_id}): ignored")
            return
        if run.summary is not None:
            loud(f"duplicate SUMMARY {run.label}: ignored")
            return
        run.summary = p
        if run.t_recv_stop is None:      # the STOP never arrived
            run.add_flag("no_stop")
            run.no_edges = True
            stop_sampling(run)
            loud(f"SUMMARY for {run.label} but no STOP was received: row written with no_stop, no energy")
        maybe_finalize(run)


def handle_message(text, t_recv, wall_recv):
    kw, _, rest = text.strip().partition(" ")
    kw = kw.upper()
    if kw == "START":
        on_start(rest.strip(), t_recv, wall_recv)
        return
    if kw in ("GETREADY", "STOP", "SUMMARY"):
        try:
            p = json.loads(rest)
            if not isinstance(p, dict):
                raise ValueError("not a JSON object")
        except ValueError as e:
            loud(f"{kw} with unreadable JSON ({e}): ignored")
            return
        {"GETREADY": on_getready, "STOP": on_stop, "SUMMARY": on_summary}[kw](p, t_recv, wall_recv)
        return
    loud(f"unknown message ignored: {text.strip()[:80]!r}")


# --- Network listener thread ----------------------------------------------------------------------------------
def network_listener(sock):
    """UDP only. The arrival time is stamped before anything else happens to the datagram."""
    sock.settimeout(1.0)
    while not stop_event.is_set():
        try:
            data, addr = sock.recvfrom(65535)
            t_recv = time.monotonic()
            wall_recv = datetime.now()
            handle_message(data.decode("utf-8", errors="replace"), t_recv, wall_recv)
        except socket.timeout:
            pass
        except Exception as e:
            loud(f"error handling a message: {e!r}")
        try:
            check_timeouts()
        except Exception as e:
            loud(f"error in the timeout check: {e!r}")


# --- Shutdown -------------------------------------------------------------------------------------------------
def print_session_summary():
    """The core figures of every row written in this run of the Collector, grouped by session, with the LIVE - NULL
    delta under each NULL row. The NULL run has the same key count and duration as its LIVE run by construction, so the
    delta needs no scaling: delta J = LIVE J - NULL J, per key = delta J / keys, watts = delta J / seconds."""
    rows = list(session_rows)
    if not rows:
        return
    by_pair = {}
    for r in rows:
        if r["pair"] is not None:
            by_pair.setdefault((r["session"], r["pair"]), {})[r["role"]] = r

    def cells(r):
        try:
            slot = str(int(r["slot"]))
        except (TypeError, ValueError):
            slot = "?"
        return [r["exp"], slot, r["platform"], r["algorithm"], r["role"],
                "-" if r["keys"] is None else str(int(round(r["keys"]))),
                "-" if r["joules"] is None else f"{r['joules']:.1f}",
                "-" if r["seconds"] is None else f"{r['seconds']:.1f}", r["status"], r["flags"], r["file"]]
    head = ["Exp", "#", "Platform", "Algorithm", "Role", "Keys", "Joules", "Seconds", "Status", "Flags", "File"]
    right = {1, 5, 6, 7}                      # right-aligned columns
    table = [cells(r) for r in rows]
    widths = [max(len(head[i]), *(len(t[i]) for t in table)) for i in range(len(head))]

    def line(vals):
        out = []
        for i, v in enumerate(vals):
            out.append(v.rjust(widths[i]) if i in right else v.ljust(widths[i]))
        return "  ".join(out).rstrip()
    pad = " " * (sum(widths[:4]) + 4 * 2)    # delta line starts under the Role column
    say("")
    shown = None
    for r, t in zip(rows, table):
        if r["session"] != shown:
            shown = r["session"]
            say(f"Session {shown}")
            say(line(head))
        say(line(t))
        mates = by_pair.get((r["session"], r["pair"]), {}) if r["pair"] is not None else {}
        if r["role"] == "LIVE" and "NULL" not in mates:
            say(f"{pad}no NULL row for this LIVE row: pair incomplete, no delta")
        elif r["role"] == "NULL":
            lv = mates.get("LIVE")
            if lv is None:
                say(f"{pad}no LIVE row for this NULL row: no delta")
            elif lv["joules"] is None or r["joules"] is None or not r["keys"]:
                say(f"{pad}no delta (energy missing from one of the pair)")
            else:
                dj = lv["joules"] - r["joules"]
                w = f"   {dj / r['seconds']:.2f} W" if r["seconds"] else ""
                say(f"{pad}delta {dj:.1f} J   {dj / r['keys']:.4g} J/{r['unit']}{w}")


def shutdown():
    with state_lock:
        for run in list(runs.values()):
            if run.t_recv_stop is None:
                run.add_flag("aborted")
            elif run.summary is None:
                run.add_flag("no_summary")
            finalize(run)
        stop_sampling_all = sampling_run
        if stop_sampling_all is not None:
            close_trace(stop_sampling_all)
    try:
        with meter_lock:
            meter._SIF.close()
    except Exception:
        pass
    say(f"\nCollector stopped: {rows_written} rows written this session, {rows_flagged} flagged")
    try:
        print_session_summary()
    except Exception as e:           # never let the summary hide a normal shutdown
        loud(f"could not print the session summary: {e!r}")


def _term(signum, frame):
    raise KeyboardInterrupt


# --- Main function, start here ---
def main():
    global PORT, outroot, summary_timeout, device_path, meter

    parser = argparse.ArgumentParser(description="Linux data acquisition server (collector).")
    parser.add_argument("--device", dest="device_path", action="store", type=str,
                        help=f"Serial device path for the TC66C meter (default={device_path})")
    parser.add_argument("--port", dest="port", action="store", type=int, default=PORT,
                        help=f"UDP port to listen on (default={PORT})")
    parser.add_argument("--outroot", dest="outroot", action="store", type=str, default=outroot,
                        help="Folder under which each experiment's folder E<Experiment> is created (default=~)")
    parser.add_argument("--summary-timeout", dest="summary_timeout", action="store", type=float, default=summary_timeout,
                        help=f"Seconds after STOP before a row is written without a SUMMARY (default={summary_timeout:g})")
    parser.add_argument("--nonet-delaystart", dest="nonet_delaystart", action="store", type=float, default=None,
                        help="Self-test without a DUT: seconds between the simulated GETREADY and START")
    parser.add_argument("--nonet-duration", dest="nonet_duration", action="store", type=float, default=None,
                        help="Self-test without a DUT: seconds between the simulated START and STOP")
    args = parser.parse_args()

    if args.device_path:
        device_path = args.device_path
    PORT = args.port
    outroot = os.path.abspath(os.path.expanduser(args.outroot))
    summary_timeout = args.summary_timeout
    nonet = args.nonet_delaystart is not None or args.nonet_duration is not None

    print(f"\nCollector {COLLECTOR_VERSION}")
    if not os.path.exists(device_path):
        print(f"WARNING: {device_path} does not exist yet. Check the meter is plugged in, "
              f"or override with --device if it enumerates differently.")
    meter = TC66C(device_path)            # exits with a message if it cannot be opened
    first = meter_read()
    if first is None:
        first = meter_read()
    if first is None:
        print(f"meter: opened {device_path} but it is not answering. Are its Power and PD switches OFF? Exiting.")
        sys.exit(1)
    print(f"meter: TC66C on {device_path}  ok ({first['Power']:.2f} W)")

    sock = None
    if not nonet:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((HOST, PORT))
        except OSError as e:
            print(f"cannot listen on UDP {HOST}:{PORT}: {e}")
            sys.exit(1)
        print(f"listening: UDP {HOST}:{PORT}")
    print(f"output: {os.path.join(outroot, 'E<n>')}  (created on the first GETREADY for each experiment)")
    print(f"summary timeout {summary_timeout:g} s")

    signal.signal(signal.SIGTERM, _term)
    threading.Thread(target=sampler_loop, daemon=True).start()

    if nonet:
        simulate_run(args)
        shutdown()
        return

    net = threading.Thread(target=network_listener, args=(sock,), daemon=True)
    net.start()
    print("Waiting for GETREADY (Ctrl+C to exit)...")
    try:
        while net.is_alive():
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    stop_event.set()
    wake.set()
    net.join(timeout=3)
    shutdown()


def simulate_run(args):
    """Self-test without a DUT (replaces v9.23's --nonet mode): one fabricated LIVE run through the real handlers."""
    delay = args.nonet_delaystart if args.nonet_delaystart is not None else 0.0
    dur = args.nonet_duration if args.nonet_duration is not None else 30.0
    print(f"\nRunning in no-network mode - start delay {delay:.2f}s, duration {dur:.2f}s\n")
    rid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-nonet-0001-LIVE"
    p = {"run_id": rid, "session_id": rid.rsplit("-0001", 1)[0], "schedule_index": 1, "role": "LIVE",
         "pair_id": "nonet-pair-1", "algorithm": "test_algo", "n_target": 1000, "sample_period": 1.0,
         "platform": "manual", "experiment": "0", "subexp": ""}
    handle_message("GETREADY " + json.dumps(p), time.monotonic(), datetime.now())
    time.sleep(delay + 3.0)
    handle_message("START " + rid, time.monotonic(), datetime.now())
    time.sleep(dur)
    stop = {"run_id": rid, "n_completed": 1000, "dut_time_to_run_s": dur, "status": "ok"}
    handle_message("STOP " + json.dumps(stop), time.monotonic(), datetime.now())
    time.sleep(2.0)
    summ = {"run_id": rid, "t_start_c": 25.0, "t_stop_c": 26.0, "throttled": False, "unit": "key"}
    handle_message("SUMMARY " + json.dumps(summ), time.monotonic(), datetime.now())
    t_end = time.monotonic() + summary_timeout + 2
    while runs and time.monotonic() < t_end:
        check_timeouts()
        time.sleep(0.2)
    print("No-network run finished.")


if __name__ == "__main__":
    main()


# END CODE
