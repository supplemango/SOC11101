#!/usr/bin/env python3
# VERSION v20261020.2345   (vYYYYMMDD.HHMM, UK local time of the last edit; bump on every change)
"""
experimenter_e2.py -- E2: energy of key generation, one program for every variant.

Run it on the DUT:      sudo python3 ~/pqc/E2/experimenter_e2.py --method shell|library [options]
and have collector.py running on the Collector that has THIS board's TC66 meter (192.168.55.200, or --collector IP).
The root rule applies (first statement). --method is required; there is no default.

--method (how ONE key is made)
  shell    starts the program `openssl genpkey <args> -out /dev/null` for every key (the original E2). Needs
           calibration_<hostname>.csv (run calibrate_e2.py once per platform). Per-key time includes starting the program.
  library  loads OpenSSL's libcrypto into Python (ctypes) and makes the keys in a loop in one process (E2b): new context,
           init, options, generate, free, for every key; nothing is written out. The keys per second are measured at the start
           of the session (--calibrate-seconds per condition, default 10). Needs --libcrypto only if the build is not in one of
           the usual places under /usr/local/ssl.

--freqs F1,F2,...   (optional) a comma list of CPU clocks in MHz, explicit, no ranges: every condition is run at every listed
           clock (E2c with --method library, E2d with --method shell). Each value is snapped to the nearest step the platform
           offers if that is within 1 % (1228 -> 1228.8), else the run refuses to start. Between pairs the clock is pinned
           (scaling_min = scaling_max on every cpufreq policy, read back) and the starting limits are restored at the end.
           Keys per run scale with the clock: N(f) = N(default clock) x f / f_default, so every run lasts about the target.
           Not available on the Mac (no clock control). The clock is sent to the Collector (TargetMHz, MeasuredMHz).
           Without --freqs the board stays at its session clock (--cpu-clock, default: unchanged; DUT-PC 2600 MHz).

Conditions: --conditions FILE (label, openssl_args, unit, execute_order). Default: conditions.csv (shell), conditions_e2b.csv
(library), conditions_e2c.csv (either method with --freqs). Library keygen understands only -algorithm and -pkeyopt.

Pairs. Every schedule entry is a PAIR: a LIVE run (N keys) then a NULL run of the same duration in which the board only waits
(shell: a per-key timetable; library: one plain sleep). The Collector measures the energy of both; the analysis subtracts.
Each run: sync_clock() -> GETREADY -> ready-wait -> thermal gate -> START -> loop -> STOP -> (after --summary-delay) SUMMARY.
Nothing is printed, written or sampled between START and STOP.
Order: [`first` conditions x R passes] [R blocks, each holding every `shuffled` (condition, clock) combination once in random
order] [`last` x R]. --unshuffled keeps the file order. --only/--skip GROUP select execute_order groups.

Sub-experiment letter sent to the Collector and session-id suffix:
  shell: "" (none) / E2D with --freqs        library: b / -E2B, or c / -E2C with --freqs      (--subexp overrides the letter)

Operator checks at the start (once): testbed cables, TC66 meter in THIS board's power path (--meter-confirmed), external fan
(--fan-confirmed). The screen shows results, warnings and errors; routine lines go to session.log (--verbose shows all).
The status line shows "running: N keys, about X, ends about hh:mm / HH:MM" (this run / whole session).

Outputs (outside git), in <invoking user's home>/E2/<session_id>/ :
  session.log, session.json, schedule.csv (done flags, clock column; --resume), results.csv (one row per run),
  run_<slot>-<LIVE|NULL>.json (per-key or per-batch times, gate, sync, throttle), and <home>/E2/STATUS.
The energy results are written by the Collector (AllResults.csv).
"""
import argparse
import array
import csv
import ctypes
import glob
import json
import math
import os
import platform
import random
import shlex
import socket
import subprocess
import sys
import time

sys.dont_write_bytecode = True           # running as root: never leave root-owned __pycache__ in the git tree
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
import platform_env                       # noqa: E402
import harness                            # noqa: E402
import e2_common                          # noqa: E402

E2_VERSION = "v20261020.2345"
platform_env.require_root(os.path.basename(sys.argv[0]))      # FIRST statement: nothing has been touched yet

OPENSSL_EXPECTED = e2_common.OPENSSL_EXPECTED
LIBCRYPTO_CANDIDATES = ("/usr/local/ssl/lib/libcrypto.so.3", "/usr/local/ssl/lib64/libcrypto.so.3", "/usr/local/ssl/lib/libcrypto.3.dylib")
MIN_KEYS = 5                  # fewest keys in a run (smoke: 1)
BATCH_MAX = 100               # library: keys per timing batch (fewer when a key is slow, so a batch is about a second at most)
SAMPLE_PERIOD = 1.0           # the Collector's meter polling period (s), sent in GETREADY
OVERHEAD_S = 40               # sync + ready-wait + gate + summary delay + statistics, per run, for the estimates only (measured 15-60 s)
KEY_CAP_MIN_S = 900.0         # shell: a single key is killed after max(this, 10 x its expected time)
KEY_CAP_FACTOR = 10.0
FAILED_PAIRS_ABORT = 3        # failed pairs in a row
GATE_MISSES_ABORT = 3         # runs in a row with the thermal gate not met
THROTTLED_ABORT = 3           # runs in a row throttled
FREQ_OFF_PCT = 3.0            # measured clock (gate burn) further than this from the target: flag freq_off
LENGTH_OFF_PCT = 25.0         # LIVE run longer/shorter than its expected length by more than this: flag length_off
SNAP_PCT = 1.0                # --freqs: a typed value is snapped to a platform step only within this
# the clock the calibrations are made at (the board's session clock): Pi 5 / CM5 governor performance = 2400 MHz, RISC-V 1600, PC fixed 2600
REF_MHZ = {"pi5": 2400.0, "cm5": 2400.0, "riscv": 1600.0, "pc": 2600.0}
SMOKE = {"repeats": 1, "target_seconds": 10.0, "min_keys": 1, "calibrate_seconds": 2.0}
SCHEDULE_COLS = ["slot", "block", "label", "freq_mhz", "n_target", "done"]
OLD_SCHEDULE_COLS = ["slot", "block", "label", "n_target", "done"]
RESULT_COLS = ["run_id", "slot", "block", "role", "label", "freq_mhz", "n_target", "n_completed", "status", "dut_time_to_run_s",
               "throttled", "gate_met", "sync_ok", "sync_error_ms", "flags"]
EXP_LETTERS = {("shell", False): ("", ""), ("shell", True): ("d", "-E2D"), ("library", False): ("b", "-E2B"), ("library", True): ("c", "-E2C")}


# ---------------------------------------------------------------------------------------------- small helpers
def git_info():
    """(short commit hash, dirty flag) of the code directory, or ('unknown', None)."""
    base = ["git", "-c", "safe.directory=*", "-C", HERE]
    try:
        h = subprocess.run(base + ["rev-parse", "--short", "HEAD"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
        if h.returncode != 0:
            return "unknown", None
        s = subprocess.run(base + ["status", "--porcelain", "--untracked-files=no"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
        return h.stdout.decode().strip(), bool(s.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return "unknown", None


def cpufreq_snapshot():
    """governor, min and max of every cpufreq policy (Linux): compared before and after a run."""
    snap = {}
    for pol in sorted(glob.glob("/sys/devices/system/cpu/cpufreq/policy*")):
        d = {}
        for k in ("scaling_governor", "scaling_min_freq", "scaling_max_freq"):
            try:
                d[k] = open(os.path.join(pol, k)).read().strip()
            except OSError:
                d[k] = None
        snap[os.path.basename(pol)] = d
    return snap


def percentile(sorted_vals, p):
    n = len(sorted_vals)
    return sorted_vals[min(n - 1, max(0, int(math.ceil(p / 100.0 * n)) - 1))]


def iter_stats(times):
    """Spread of the time per key. times: seconds per key (shell: one value per key; library: one per batch of keys)."""
    n = len(times)
    if n == 0:
        return {"n": 0}
    s = sorted(times)
    mean = sum(s) / n
    cv = None
    if n > 1 and mean > 0:
        sd = math.sqrt(sum((x - mean) ** 2 for x in s) / (n - 1))
        cv = 100.0 * sd / mean
    return {"n": n, "mean_s": mean, "min_s": s[0], "p50_s": percentile(s, 50), "p95_s": percentile(s, 95), "p99_s": percentile(s, 99),
            "max_s": s[-1], "cv_pct": cv}


def r6(x):
    return None if x is None else round(x, 9)


def fmt_dur(secs):
    secs = int(round(secs))
    if secs >= 3600:
        return "%d h %02d min" % (secs // 3600, secs % 3600 // 60)
    if secs >= 120:
        return "%d min" % round(secs / 60.0)
    return "%d s" % secs


def clock_hhmm(secs_from_now):
    return time.strftime("%H:%M", time.localtime(time.time() + secs_from_now))


def fmt_f(f):
    """A clock in MHz for files and screen: 2400 -> '2400', 1228.8 -> '1228.8', None -> ''."""
    return "" if f is None else ("%g" % f)


def cond_text(label, f):
    return label if f is None else "%s@%s" % (label, fmt_f(f))


class Sender(object):
    """UDP, fire and forget, no replies. A failed send is logged once and never stops the run."""

    def __init__(self, host, port, log):
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.log = log
        self.warned = False

    def send(self, keyword, payload=None):
        text = keyword if payload is None else keyword + " " + (payload if isinstance(payload, str) else json.dumps(payload, default=str))
        try:
            self.sock.sendto(text.encode("utf-8"), self.addr)
        except OSError as e:
            if not self.warned:
                self.warned = True
                self.log("WARNING: cannot send to the Collector %s:%s (%s); the run carries on" % (self.addr[0], self.addr[1], e))


def screen_hides(msg):
    """Log lines that go to session.log only (the screen shows results, warnings and errors; --verbose shows everything)."""
    if msg.startswith(("session ", "command line:", "groups: ", "host ", "openssl:", "libcrypto:", "resumed from", "cpu clock:", "governor:",
                       "quiesce:", "fan:", "external fan:", "TC66 meter:", "WAITING FOR OPERATOR", "gate met:", "T_ref=",
                       "SESSION COMPLETE", "(prompts off)", "calibrated ", "clock pinned:")):
        return True
    return msg.startswith("sync_clock:") and "ok=True" in msg


def make_hide(log, live_on):
    """The SessionLog.hide hook. A routine line never stays on the screen: it goes to session.log, and once the runs have started
    (live_on["on"]) it is also shown briefly on the one self-overwriting status line (terminal only)."""
    def hide(msg):
        if not screen_hides(msg):
            return False
        if live_on["on"] and not msg.startswith("WAITING FOR OPERATOR"):
            log.progress(time.strftime("%H:%M:%S ") + msg)
        return True
    return hide


def confirm_meter(a, host, log):
    """The TC66 meter must be in the power path of THIS board. Once per session; y/yes continues, anything else aborts."""
    if a.no_prompts:
        log("TC66 meter: not asked (--no-prompts)")
        return "skipped"
    if a.meter_confirmed:
        log("TC66 meter: confirmed by flag (in the power path of %s)" % host)
        return "flag"
    if not sys.stdin.isatty():
        raise platform_env.EnvError("TC66 meter not confirmed; pass --meter-confirmed for unattended runs")
    if input("Collector running and Monitored TC66 meter is in the power path of THIS board (%s)? [y/n]: " % host).strip().lower() not in ("y", "yes"):
        raise platform_env.EnvError("TC66 meter not confirmed as being in the power path of %s" % host)
    log("TC66 meter: confirmed by the operator (in the power path of %s)" % host)
    return "operator"


# ---------------------------------------------------------------------------------------------- clocks (--freqs)
def snap_freqs(text, avail):
    """'1000,1228' -> ([1000.0, 1228.8], ['1000 -> 1000', '1228 -> 1228.8']). Explicit comma list; each value must lie within
    SNAP_PCT of a platform step. Raises ValueError with the message to show."""
    if not avail:
        raise ValueError("this platform has no clock control or its clock steps cannot be listed (--freqs not possible here)")
    out, notes = [], []
    for tok in [t.strip() for t in text.split(",")]:
        if not tok:
            continue
        try:
            v = float(tok)
        except ValueError:
            raise ValueError("--freqs: %r is not a number (a comma list of MHz, e.g. 1000,1400,1800)" % tok)
        if v <= 0:
            raise ValueError("--freqs: %r must be positive" % tok)
        best = min(avail, key=lambda x: abs(x - v))
        if abs(best - v) / v * 100.0 > SNAP_PCT:
            raise ValueError("--freqs: %s MHz is not a clock step of this board (nearest %s MHz). Steps: %s" %
                             (tok, fmt_f(best), ", ".join(fmt_f(x) for x in avail)))
        if best in out:
            raise ValueError("--freqs: %s MHz is the same step (%s MHz) as an earlier value" % (tok, fmt_f(best)))
        out.append(best)
        notes.append("%s -> %s" % (tok, fmt_f(best)))
    if not out:
        raise ValueError("--freqs: no values given")
    return out, notes


# ---------------------------------------------------------------------------------------------- the in-process keygen (library method)
def find_libcrypto(given):
    if given:
        return given
    for p in LIBCRYPTO_CANDIDATES:
        if os.path.exists(p):
            return p
    return LIBCRYPTO_CANDIDATES[0]


def load_libcrypto(path):
    """Load libcrypto through ctypes and declare the few functions used. Returns (lib, version string)."""
    lib = ctypes.CDLL(path)
    vp = ctypes.c_void_p
    lib.OpenSSL_version.restype = ctypes.c_char_p
    lib.OpenSSL_version.argtypes = [ctypes.c_int]
    lib.EVP_PKEY_CTX_new_from_name.restype = vp
    lib.EVP_PKEY_CTX_new_from_name.argtypes = [vp, ctypes.c_char_p, vp]
    lib.EVP_PKEY_keygen_init.argtypes = [vp]
    lib.EVP_PKEY_CTX_ctrl_str.argtypes = [vp, ctypes.c_char_p, ctypes.c_char_p]
    lib.EVP_PKEY_generate.argtypes = [vp, ctypes.POINTER(vp)]
    lib.EVP_PKEY_free.argtypes = [vp]
    lib.EVP_PKEY_CTX_free.argtypes = [vp]
    return lib, lib.OpenSSL_version(0).decode()


def parse_openssl_args(text):
    """'-algorithm RSA -pkeyopt rsa_keygen_bits:1024' -> ('RSA', [('rsa_keygen_bits', '1024')]). Raises ValueError otherwise."""
    toks = shlex.split(text)
    algo, opts, i = None, [], 0
    while i < len(toks):
        t = toks[i]
        if t == "-algorithm" and i + 1 < len(toks):
            algo = toks[i + 1]
            i += 2
        elif t == "-pkeyopt" and i + 1 < len(toks) and ":" in toks[i + 1]:
            k, v = toks[i + 1].split(":", 1)
            opts.append((k, v))
            i += 2
        else:
            raise ValueError("openssl_args %r: only -algorithm NAME and -pkeyopt name:value are understood (stopped at %r)" % (text, t))
    if not algo:
        raise ValueError("openssl_args %r: no -algorithm" % text)
    return algo, opts


def make_keygen(lib, algo, opts):
    """Return one() that generates one key: new context, init, options, generate, free key, free context.
    Raises RuntimeError if OpenSSL refuses a step."""
    vp = ctypes.c_void_p
    byref = ctypes.byref
    b = algo.encode()
    ob = [(k.encode(), v.encode()) for k, v in opts]
    new, init, ctrl, gen = lib.EVP_PKEY_CTX_new_from_name, lib.EVP_PKEY_keygen_init, lib.EVP_PKEY_CTX_ctrl_str, lib.EVP_PKEY_generate
    pfree, cfree = lib.EVP_PKEY_free, lib.EVP_PKEY_CTX_free

    def one():
        ctx = new(None, b, None)
        if not ctx:
            raise RuntimeError("OpenSSL does not know the algorithm %s" % algo)
        try:
            if init(ctx) != 1:
                raise RuntimeError("keygen_init failed for %s" % algo)
            for k, v in ob:
                if ctrl(ctx, k, v) <= 0:
                    raise RuntimeError("option %s:%s refused for %s" % (k.decode(), v.decode(), algo))
            pk = vp()
            if gen(ctx, byref(pk)) != 1:
                raise RuntimeError("generate failed for %s" % algo)
            pfree(pk)
        finally:
            cfree(ctx)
    return one


def calibrate_inprocess(conds, keygens, cal_s, target_seconds, min_keys, log):
    """In-process rate of every condition at the session clock: {label: (seconds_per_key, keys_measured, n_for_the_run)}."""
    out = {}
    mono = time.monotonic
    for c in conds:
        kg = keygens[c["label"]]
        kg()                                    # one key first: loads the algorithm, so it is not in the rate
        n, t0 = 0, mono()
        end = t0 + cal_s
        while mono() < end or n < 1:
            kg()
            n += 1
        dt = mono() - t0
        spk = dt / n
        out[c["label"]] = (spk, n, max(min_keys, int(round(target_seconds / spk))))
        log("calibrated %s: %.3f us/key from %d keys; %d keys for a %g s run" % (c["label"], spk * 1e6, n, out[c["label"]][2], target_seconds))
    return out


def batch_of(spk):
    """Keys per timing batch: up to BATCH_MAX, but a batch of slow keys stays about a second."""
    return max(1, min(BATCH_MAX, int(1.0 / spk)))


# ---------------------------------------------------------------------------------------------- schedule
def n_at(n_ref, f, f_ref, min_keys):
    """Keys for a run at clock f when n_ref keys fill the target at the reference clock (time per key scales as 1/f)."""
    return max(min_keys, int(round(n_ref * (f / f_ref if f else 1.0))))


def build_schedule(conds, freqs, n_ref, f_ref, repeats, shuffle, rng, min_keys):
    """List of dicts slot, block, label, freq, n_target, done. See the module docstring for the order."""
    fl = list(freqs) if freqs else [None]
    first = [(c, f) for c in conds if c["execute_order"] == "first" for f in fl]
    mid = [(c, f) for c in conds if c["execute_order"] == "shuffled" for f in fl]
    last = [(c, f) for c in conds if c["execute_order"] == "last" for f in fl]
    seq = []

    def group(cs, rep):
        cs = list(cs)
        if shuffle:
            rng.shuffle(cs)
        for c in cs:
            seq.append((rep, c))
    for rep in range(1, repeats + 1):
        group(first, rep)
    for rep in range(1, repeats + 1):
        group(mid, rep)
    for rep in range(1, repeats + 1):
        group(last, rep)
    return [{"slot": i, "block": rep, "label": c["label"], "freq": f, "n_target": n_at(n_ref[c["label"]], f, f_ref, min_keys), "done": 0}
            for i, (rep, (c, f)) in enumerate(seq, 1)]


def write_schedule(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(SCHEDULE_COLS)
        for r in rows:
            w.writerow([r["slot"], r["block"], r["label"], fmt_f(r["freq"]), r["n_target"], r["done"]])
    os.replace(tmp, path)


def read_schedule(path):
    rows = []
    with open(path, newline="") as f:
        rd = csv.DictReader(f)
        cols = [c.strip() for c in rd.fieldnames] if rd.fieldnames else None
        if cols not in (SCHEDULE_COLS, OLD_SCHEDULE_COLS):
            raise ValueError("%s: header must be %s" % (path, ",".join(SCHEDULE_COLS)))
        for r in rd:
            fr = (r.get("freq_mhz") or "").strip()
            rows.append({"slot": int(r["slot"]), "block": int(r["block"]), "label": r["label"].strip(),
                         "freq": float(fr) if fr else None, "n_target": int(r["n_target"]), "done": int(r["done"])})
    return rows


# ---------------------------------------------------------------------------------------------- one run
def run_one(role, ent, n_target, null_duration_s, ctx):
    """One LIVE or NULL run, in the agreed order. Returns a dict (the run record). If the run was interrupted the record
    has "exc" (the exception to re-raise once the caller has written the row)."""
    a, env, log, snd = ctx["a"], ctx["env"], ctx["log"], ctx["sender"]
    slot, label, mhz = ent["slot"], ent["label"], ent["freq"]
    fkey = (label, mhz)
    lt = cond_text(label, mhz)
    method = a.method
    session_id = ctx["session_id"]
    run_id = "%s-%04d-%s" % (session_id, slot, role)
    pair_id = "%s-%04d" % (session_id, slot)
    rec = {"run_id": run_id, "pair_id": pair_id, "slot": slot, "block": ent["block"], "role": role, "label": label, "freq_mhz": mhz,
           "n_target": n_target, "flags": list(ctx["base_flags"]), "exc": None}
    spk = ctx["spk"][fkey]
    tag = "%s %s" % (ctx["pair_text"], role)

    def live(msg):
        log.progress("%s  %-16s %s   ETA %s" % (tag, lt, msg, ctx["eta"]()))

    # a. clock sync (before GETREADY, so its activity cannot disturb the gate)
    live("syncing the clock")
    if a.no_sync:
        rec["sync"] = None
        rec["flags"].append("sync_skipped")
    else:
        rec["sync"] = env.sync_clock()
        if not rec["sync"].get("ok"):
            rec["flags"].append("clocksync_failed")
    # b. GETREADY (no reply)
    snd.send("GETREADY", {"run_id": run_id, "session_id": session_id, "schedule_index": slot, "role": role, "pair_id": pair_id,
                          "algorithm": label, "n_target": n_target, "sample_period": SAMPLE_PERIOD, "platform": ctx["host"],
                          "experiment": a.experiment, "subexp": ctx["subexp"], "unit": ctx["unit"][label],
                          "cpu_clock": fmt_f(mhz) if mhz else a.cpu_clock, "method": method, "block": ent["block"],
                          **({"freq_mhz": mhz} if mhz else {})})
    # c. ready-wait: the meter opens meanwhile
    live("waiting for the Collector (%g s)" % a.ready_wait)
    time.sleep(a.ready_wait)
    # d. thermal gate
    if a.no_gate or env.t_target is None:
        rec["gate"] = {"met": None, "note": "skipped" if a.no_gate else "no temperature sensor"}
        rec["flags"].append("gate_skipped")
    else:
        live("temperature gate")
        rec["gate"] = env.prepare_iteration()
        if not rec["gate"].get("met"):
            rec["flags"].append("gate_not_met")
        if rec["gate"].get("plateau_below_target"):
            rec["flags"].append("gate_plateau")      # the burn stopped rising below the target: the run starts at the plateau temperature
    if env.t_ref_converged is False:
        rec["flags"].append("t_ref_unconverged")
    # e. baselines for the throttle and clock-setting checks
    thr0 = env.throttle_state()
    freq0 = cpufreq_snapshot()
    t_start_c = rec["gate"].get("end_temp") if rec["gate"].get("end_temp") is not None else env.read_temp()
    expect_live = n_target * spk
    expect_s = expect_live if role == "LIVE" else null_duration_s
    # the status line stays on screen for the whole run (written BEFORE START; nothing is printed between START and STOP)
    log.progress("%s %-16s %-4s running: %d keys, about %s, ends about %s / %s" %
                 (ctx["pair_text"], lt, role, n_target, fmt_dur(expect_s), clock_hhmm(expect_s), ctx["eta"](expect_s)))
    if method == "library":
        keygen = ctx["keygens"][label]
        batch = ctx["batches"][fkey]
        if role == "LIVE":
            keygen()                    # one key before START, so the first timed key is not the first of the run
    else:
        cmd = ctx["cmds"][label]
        key_cap = max(KEY_CAP_MIN_S, KEY_CAP_FACTOR * spk)
    # f. START; t_mono0 immediately after.   From here to STOP: silent.
    snd.send("START", run_id)
    mono = time.monotonic
    t_mono0 = mono()
    wall0 = time.time_ns()
    status = "ok"
    exc = None
    times = array.array("d")
    slips = array.array("d")
    n_done = 0
    # g. the loop
    try:
        if role == "LIVE":
            guard_s = a.max_duration_factor * expect_live
            if method == "library":
                t_prev = t_mono0
                while n_done < n_target:
                    m = min(batch, n_target - n_done)
                    try:
                        for _ in range(m):
                            keygen()
                    except RuntimeError as e:
                        status = "keygen_failed"
                        rec["error"] = str(e)
                        break
                    now = mono()
                    times.append((now - t_prev) / m)
                    t_prev = now
                    n_done += m
                    if now - t_mono0 > guard_s:
                        status = "max_duration" if n_done < n_target else "ok"
                        break
            else:
                timer = e2_common.time_one_key
                oenv = ctx["oenv"]
                while n_done < n_target:
                    if mono() - t_mono0 > guard_s:
                        status = "max_duration"
                        break
                    dt, rc, err = timer(cmd, oenv, key_cap)
                    if dt is None:
                        status = "key_timeout"
                        rec["error"] = "a key ran longer than %g s" % key_cap
                        break
                    if rc != 0:
                        status = "openssl_rc=%s" % rc
                        rec["error"] = err
                        break
                    times.append(dt)
                    n_done += 1
        else:
            if method == "library":
                deadline = t_mono0 + null_duration_s
                while True:
                    left = deadline - mono()
                    if left <= 0:
                        break
                    time.sleep(min(left, 30.0))
                n_done = n_target                   # count-matched by definition: nothing was generated
            else:
                period = null_duration_s / float(n_target)
                sleep = time.sleep
                prev = t_mono0
                for k in range(1, n_target + 1):
                    target = t_mono0 + k * period
                    delay = target - mono()
                    if delay > 0:
                        sleep(delay)
                    now = mono()
                    if now > target:
                        slips.append(now - target)
                    times.append(now - prev)
                    prev = now
                    n_done += 1
    except (KeyboardInterrupt, SystemExit) as e:
        status = "interrupted"
        exc = e
    # h, i. STOP: nothing between the loop and the message
    t_mono1 = mono()
    wall1 = time.time_ns()
    dur = t_mono1 - t_mono0
    snd.send("STOP", {"run_id": run_id, "n_completed": n_done, "dut_time_to_run_s": dur, "status": status,
                      "t_mono0": t_mono0, "t_mono1": t_mono1, "wall_start_ns": wall0, "wall_end_ns": wall1})
    # j. everything else, after STOP
    rec.update(n_completed=n_done, status=status, dut_time_to_run_s=dur, t_mono0=t_mono0, t_mono1=t_mono1)
    if exc is not None:
        rec["exc"] = exc
        rec["flags"].append("interrupted")
        return rec
    t_stop_c = env.read_temp()
    thr1 = env.throttle_state(since=thr0)
    rec["throttle"] = thr1
    if thr1.get("throttled"):
        rec["flags"].append("throttled")
    if cpufreq_snapshot() != freq0:
        rec["flags"].append("cpufreq_changed")
    if status == "max_duration":
        rec["flags"].append("max_duration")
    elif status != "ok":
        rec["flags"].append(status.split("=")[0])
    if abs((wall1 - wall0) / 1e9 - dur) > 0.5:
        rec["flags"].append("clock_step")
    if role == "LIVE" and status == "ok" and expect_live > 0 and abs(dur - expect_live) / expect_live * 100.0 > LENGTH_OFF_PCT:
        rec["flags"].append("length_off")
    st = iter_stats(times)
    null_slip = max(slips) if len(slips) else 0.0
    cc = (ctx["env_controls"].get("cpu_clock") or {})
    if mhz:
        target_f = float(mhz)
    elif cc.get("mode") == "fixed" and cc.get("target_khz"):
        target_f = cc["target_khz"] / 1000.0
    else:
        target_f = None
    target_mhz = int(round(target_f)) if target_f else None
    bf = rec["gate"].get("burn_freq_khz")
    measured_mhz = int(round(bf["max"] / 1000.0)) if bf and bf.get("max") else None
    if mhz and measured_mhz and abs(bf["max"] / 1000.0 - target_f) / target_f * 100.0 > FREQ_OFF_PCT:
        rec["flags"].append("freq_off")
    rec.update(stats=st, t_start_c=t_start_c, t_stop_c=t_stop_c, target_mhz=target_mhz, measured_mhz=measured_mhz,
               null_slip_s=null_slip if (role == "NULL" and method == "shell") else None,
               null_slipped_turns=sum(1 for x in slips if x > 0.001) if (role == "NULL" and method == "shell") else None)
    summary = {"run_id": run_id, "t_start_c": t_start_c, "t_stop_c": t_stop_c, "throttled": thr1.get("throttled"),
               "target_mhz": target_mhz, "measured_mhz": measured_mhz, "freq_khz": int(round(mhz * 1000)) if mhz else None,
               "iter_p50_s": r6(st.get("p50_s")), "iter_p95_s": r6(st.get("p95_s")), "iter_max_s": r6(st.get("max_s")),
               "iter_min_s": r6(st.get("min_s")), "iter_cv_pct": None if st.get("cv_pct") is None else round(st["cv_pct"], 2),
               "null_slip_s": r6(null_slip) if (role == "NULL" and method == "shell") else None,
               "flags": rec["flags"], "unit": ctx["unit"][label],
               "gate": rec["gate"], "sync": rec["sync"], "throttle_detail": thr1.get("detail"),
               "n_target": n_target, "pair_id": pair_id, "e2_version": E2_VERSION, "git_commit": ctx["git"][0],
               "git_dirty": ctx["git"][1], "openssl": ctx["openssl_version"], "method": method,
               "inprocess": method == "library", "internet_always": a.internet_always, "session_filter": ctx["session_filter"],
               "collector": a.collector, "meter_confirmed": ctx["meter_conf"]}
    if method == "library":
        summary.update(libcrypto=ctx["libcrypto_path"], batch_keys=batch)
    # the run files are written inside the summary delay
    extra = ({"batch_seconds_per_key": [round(x, 9) for x in times]} if method == "library"
             else {"key_seconds": [round(x, 6) for x in times], "null_slip_seconds": [round(x, 6) for x in slips]})
    with open(os.path.join(ctx["sdir"], "run_%04d-%s.json" % (slot, role)), "w") as f:
        json.dump(dict(rec, summary=summary, **extra), f, default=str)
    row = {"run_id": run_id, "slot": slot, "block": ent["block"], "role": role, "label": label, "freq_mhz": fmt_f(mhz),
           "n_target": n_target, "n_completed": n_done, "status": status, "dut_time_to_run_s": round(dur, 3), "throttled": thr1.get("throttled"),
           "gate_met": rec["gate"].get("met"), "sync_ok": None if rec["sync"] is None else rec["sync"].get("ok"),
           "sync_error_ms": None if rec["sync"] is None else rec["sync"].get("dut_clock_error_ms"), "flags": ";".join(rec["flags"])}
    ctx["results_writer"].writerow(row)
    ctx["results_file"].flush()
    # k. SUMMARY, at least --summary-delay after STOP
    wait = a.summary_delay - (mono() - t_mono1)
    if wait > 0:
        time.sleep(wait)
    snd.send("SUMMARY", summary)
    return rec


# ---------------------------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="store_true")
    ap.add_argument("--method", choices=("shell", "library"), default=None, help="REQUIRED. shell = openssl genpkey per key (E2); library = in-process libcrypto (E2b)")
    ap.add_argument("--freqs", default=None, metavar="MHZ,MHZ,...", help="explicit comma list of CPU clocks in MHz (E2c library / E2d shell); snapped to platform steps within 1 %%")
    ap.add_argument("--target-seconds", type=float, default=None, help="target length of a LIVE run (default 600; smoke 10)")
    ap.add_argument("--calibrate-seconds", type=float, default=None, help="library: seconds per condition for the in-process rate at the start (default 10; smoke 2)")
    ap.add_argument("--repeats", type=int, default=None, help="passes over the conditions (default 3, the minimum; smoke 1)")
    ap.add_argument("--unshuffled", action="store_true", help="keep the order of the conditions file in every group (no shuffling)")
    ap.add_argument("--resume", default=None, metavar="SCHEDULE.CSV", help="start a NEW session at the first undone pair of an earlier schedule.csv")
    ap.add_argument("--experiment", default="2", help="experiment number sent to the Collector (default 2)")
    ap.add_argument("--subexp", default=None, help="sub-experiment letter sent to the Collector (default: by method and --freqs, see above)")
    ap.add_argument("--collector", default="192.168.55.200", help="Collector address (default 192.168.55.200)")
    ap.add_argument("--port", type=int, default=65432, help="Collector UDP port (default 65432)")
    ap.add_argument("--summary-delay", type=float, default=2.0, help="seconds between STOP and SUMMARY (default 2.0)")
    ap.add_argument("--ready-wait", type=float, default=3.0, help="seconds between GETREADY and the thermal gate (default 3.0)")
    ap.add_argument("--max-duration-factor", type=float, default=3.0, help="a LIVE run is stopped (flagged max_duration) after this many times its expected length (default 3.0)")
    ap.add_argument("--only", default=None, metavar="GROUPS", help="run only these execute_order groups (first | shuffled | last, comma list)")
    ap.add_argument("--skip", default=None, metavar="GROUPS", help="leave out these execute_order groups (first | shuffled | last, comma list)")
    ap.add_argument("--plan", action="store_true", help="print the schedule and the time estimates, then exit (touches nothing)")
    ap.add_argument("--smoke", action="store_true", help="bare-minimum counts (1 pass, 10 s runs) to prove the whole flow; session named ...-SMOKE")
    ap.add_argument("--internet-always", action="store_true", help="this board has no separate internet cable (DUT-Mac on Wi-Fi): skip the internet-absent check; the Collector must still answer")
    ap.add_argument("--openssl", default=None, help="shell: path to openssl (default /usr/local/ssl/bin/openssl, must be %s)" % OPENSSL_EXPECTED)
    ap.add_argument("--libcrypto", default=None, help="library: path to the benchmark build's libcrypto (default: first of %s; must be OpenSSL %s)" % (", ".join(LIBCRYPTO_CANDIDATES), OPENSSL_EXPECTED))
    ap.add_argument("--conditions", default=None, help="conditions file (default: conditions.csv shell, conditions_e2b.csv library, conditions_e2c.csv with --freqs)")
    ap.add_argument("--out", default=None, help="data folder (default: <invoking user's home>/E2)")
    ap.add_argument("--meter-confirmed", action="store_true", help="skip the question whether the TC66 meter is in this board's power path (unattended runs)")
    ap.add_argument("--fan-confirmed", action="store_true", help="library option: skip the external-fan question")
    ap.add_argument("--cpu-clock", default="default", help="library option: default | turbo | noturbo | <MHz> (not with --freqs)")
    ap.add_argument("--ntp-server", default="192.168.55.100", help="library option: the clock-sync server (JUMP)")
    ap.add_argument("--no-sync", action="store_true", help="debug only: skip sync_clock() (every run is then flagged)")
    ap.add_argument("--no-gate", action="store_true", help="debug only: skip the thermal gate")
    ap.add_argument("--no-prompts", action="store_true", help="debug only: no operator pauses and no network checks")
    ap.add_argument("--any-lib-version", action="store_true", help="debug only: accept a libcrypto that is not %s" % OPENSSL_EXPECTED)
    ap.add_argument("--verbose", action="store_true", help="show the per-second thermal gate lines on screen")
    a = ap.parse_args()
    if a.version:
        print("experimenter_e2.py", E2_VERSION, "| e2_common", e2_common.E2_COMMON_VERSION, "| platform_env",
              platform_env.PLATFORM_ENV_VERSION, "| harness", harness.HARNESS_VERSION)
        return 0
    if a.method is None:
        ap.error("--method shell|library is required (shell = openssl genpkey per key; library = in-process libcrypto)")
    method = a.method
    if a.freqs is not None and a.resume:
        ap.error("--freqs cannot be combined with --resume (the schedule file already holds the clocks)")
    if a.freqs is not None and a.cpu_clock != "default":
        ap.error("--freqs sets the clock itself; do not combine it with --cpu-clock")
    dflt = SMOKE if a.smoke else {"repeats": 3, "target_seconds": 600.0, "min_keys": MIN_KEYS, "calibrate_seconds": 10.0}
    if a.repeats is None:
        a.repeats = dflt["repeats"]
    if a.target_seconds is None:
        a.target_seconds = dflt["target_seconds"]
    if a.calibrate_seconds is None:
        a.calibrate_seconds = dflt["calibrate_seconds"]
    min_keys = dflt["min_keys"]
    if a.repeats < 1 or a.target_seconds <= 0 or a.calibrate_seconds <= 0 or a.max_duration_factor < 1:
        ap.error("--repeats must be >= 1, --target-seconds and --calibrate-seconds > 0, --max-duration-factor >= 1")

    host = e2_common.platform_name()
    env0 = platform_env.PlatformEnvironment(log=lambda m: None)         # detection only: nothing is changed
    kind = env0.kind
    f_ref = REF_MHZ.get(kind)
    freqs = None
    freq_notes = []
    if a.freqs is not None:
        if kind == "mac":
            print("ERROR: --freqs is not possible on the Mac (no clock control).")
            return 1
        if f_ref is None:
            print("ERROR: --freqs: no reference clock is defined for platform kind %r." % kind)
            return 1
        try:
            freqs, freq_notes = snap_freqs(a.freqs, env0.available_clocks_mhz())
        except ValueError as e:
            print("ERROR:", e)
            return 1
    letter, suffix = EXP_LETTERS[(method, freqs is not None)]
    print("E2 key generation (%s)  %s%s   experimenter_e2 %s | e2_common %s | platform_env %s | harness %s" %
          (method, host, "  SMOKE" if a.smoke else "", E2_VERSION, e2_common.E2_COMMON_VERSION, platform_env.PLATFORM_ENV_VERSION,
           harness.HARNESS_VERSION), flush=True)

    # ---- conditions (and, for shell, the calibration): nothing is touched until they are in order
    cond_path = a.conditions or os.path.join(HERE, "conditions_e2c.csv" if a.freqs is not None else ("conditions_e2b.csv" if method == "library" else "conditions.csv"))
    try:
        conds = e2_common.load_conditions(cond_path)
        parsed = dict((c["label"], parse_openssl_args(c["openssl_args"])) for c in conds) if method == "library" else None
        cal = e2_common.load_calibration(e2_common.calibration_path(HERE, host)) if method == "shell" else None
    except (ValueError, OSError) as e:
        print("ERROR:", e)
        return 1
    groups = ("first", "shuffled", "last")
    sel = {}
    for opt in ("only", "skip"):
        val = getattr(a, opt)
        if val is None:
            continue
        gl = [g.strip() for g in val.split(",") if g.strip()]
        bad = [g for g in gl if g not in groups]
        if bad or not gl:
            ap.error("--%s: groups are %s (got %r)" % (opt, ", ".join(groups), val))
        sel[opt] = gl
    if sel and a.resume:
        ap.error("--only / --skip cannot be combined with --resume (the schedule file already says what is left)")
    if "only" in sel and "skip" in sel:
        ap.error("use --only or --skip, not both")
    session_filter = ("only:" + ",".join(sel["only"])) if "only" in sel else (("skip:" + ",".join(sel["skip"])) if "skip" in sel else "all")
    if "only" in sel:
        conds = [c for c in conds if c["execute_order"] in sel["only"]]
    elif "skip" in sel:
        conds = [c for c in conds if c["execute_order"] not in sel["skip"]]
    if not conds:
        print("ERROR: --%s %s leaves no conditions in %s" % ("only" if "only" in sel else "skip", ",".join(sel.get("only") or sel.get("skip")), cond_path))
        return 1
    by_label = dict((c["label"], c) for c in conds)
    resumed_from = None
    sched = None
    if a.resume:
        try:
            sched = [r for r in read_schedule(a.resume) if not r["done"]]
        except (OSError, ValueError) as e:
            print("ERROR: cannot use --resume %s: %s" % (a.resume, e))
            return 1
        unknown = sorted(set(r["label"] for r in sched) - set(by_label))
        if unknown:
            print("ERROR: --resume: conditions not in %s: %s" % (cond_path, ", ".join(unknown)))
            return 1
        if not sched:
            print("Nothing to resume: every pair in %s is done." % a.resume)
            return 0
        resumed_from = os.path.abspath(a.resume)
        conds = [c for c in conds if c["label"] in set(r["label"] for r in sched)]
        rf_set = sorted(set(r["freq"] for r in sched if r["freq"] is not None))
        if rf_set:
            freqs = rf_set
            letter, suffix = EXP_LETTERS[(method, True)]
        if any(r["freq"] is not None for r in sched) and f_ref is None and method == "shell":
            print("ERROR: --resume: no reference clock for platform kind %r" % kind)
            return 1
    if a.subexp is not None:
        letter = a.subexp
    seed = int(time.time() * 1000) & 0xFFFFFFFF

    # keys per run at the session clock (shell: from the calibration file; library: measured at the start of the session)
    spk_ref = {}
    n_ref = {}
    if method == "shell":
        need = conds
        missing = e2_common.missing_calibration(need, cal)
        if missing:
            print(e2_common.calibration_error_text(missing))
            return 1
        for c in conds:
            spk_ref[c["label"]] = cal[c["label"]][0]
            n_ref[c["label"]] = a.target_seconds / spk_ref[c["label"]]
        if sched is None:
            sched = build_schedule(conds, freqs, n_ref, f_ref, a.repeats, not a.unshuffled, random.Random(seed), min_keys)

    def spk_dict(sched_rows):
        d = {}
        for r in sched_rows:
            k = (r["label"], r["freq"])
            if method == "shell" or not a.resume:
                d[k] = spk_ref[r["label"]] * (f_ref / r["freq"] if r["freq"] else 1.0)
            else:
                d[k] = a.target_seconds / float(r["n_target"])      # resumed library run: the schedule's counts fill the target
        return d

    # ---- the tool of the method
    if method == "shell":
        exe = e2_common.openssl_path(a.openssl)
        if not exe or not os.path.exists(exe):
            print("ERROR: openssl not found (%s); give --openssl PATH" % (exe or "none"))
            return 1
        oenv = harness.openssl_runtime_env(exe)
        ver = e2_common.openssl_version(exe, oenv)
        if not ver or OPENSSL_EXPECTED not in ver:
            print("ERROR: %s reports %r; the benchmark build is OpenSSL %s. Give --openssl PATH of the %s build." % (exe, ver, OPENSSL_EXPECTED, OPENSSL_EXPECTED))
            return 1
        cmds = dict((c["label"], e2_common.keygen_cmd(exe, c["openssl_args"])) for c in conds)
        tool_text, tool_path = "openssl: %s (%s)" % (ver, exe), exe
        keygens, libcrypto_path = None, None
    else:
        oenv, cmds = None, None
        libcrypto_path = find_libcrypto(a.libcrypto)

    unit = dict((c["label"], c["unit"]) for c in conds)

    # ---- the plan
    def est_pair(expect):
        return 2 * (expect + OVERHEAD_S)

    if sched is not None:
        spk = spk_dict(sched)
        expect_s = dict((k, next(r["n_target"] for r in sched if (r["label"], r["freq"]) == k) * v) for k, v in spk.items())
        total_est = sum(est_pair(expect_s[(r["label"], r["freq"])]) for r in sched)
        n_pairs_plan = len(sched)
    else:
        n_pairs_plan = a.repeats * len(conds) * (len(freqs) if freqs else 1)
        total_est = n_pairs_plan * est_pair(a.target_seconds) + a.calibrate_seconds * len(conds) + 30
    if a.plan:
        print("method %s; conditions %s (%d)%s" % (method, cond_path, len(conds), "; clocks (MHz): " + ", ".join(fmt_f(f) for f in freqs) if freqs else ""))
        if freq_notes:
            print("clocks as typed -> used: " + ", ".join(freq_notes))
        print("seed %d; %d pairs (%d runs) of about %g s; %s%s; groups: %s" % (seed, n_pairs_plan, 2 * n_pairs_plan, a.target_seconds,
              "unshuffled" if a.unshuffled else "shuffled in blocks", "; resuming" if a.resume else "", session_filter))
        if sched is not None:
            print("%-12s %8s %12s %8s %10s" % ("condition", "MHz", "s/key", "keys", "run"))
            shown = set()
            for r in sched:
                k = (r["label"], r["freq"])
                if k in shown:
                    continue
                shown.add(k)
                print("%-12s %8s %12.6g %8d %10s" % (r["label"], fmt_f(r["freq"]) or "-", spk[k], r["n_target"], fmt_dur(r["n_target"] * spk[k])))
            print("order: " + ", ".join("%d:%s" % (r["slot"], cond_text(r["label"], r["freq"])) for r in sched[:20]) + (" ..." if len(sched) > 20 else ""))
        else:
            print("The number of keys per run is set at the start of a real session by a %g s in-process rate measurement per condition"
                  "%s." % (a.calibrate_seconds, " (scaled by f / %g MHz for each clock)" % f_ref if freqs else ""))
        print("estimated total %s (about %s per pair), would end about %s" % (fmt_dur(total_est), fmt_dur(total_est / float(n_pairs_plan)), clock_hhmm(total_est)))
        return 0

    # ---- the library (loaded and tried on every condition before anything is set up)
    if method == "library":
        try:
            lib, ver = load_libcrypto(libcrypto_path)
        except (OSError, AttributeError) as e:
            print("ERROR: cannot load %s (%s); give --libcrypto PATH" % (libcrypto_path, e))
            return 1
        if OPENSSL_EXPECTED not in ver and not a.any_lib_version:
            print("ERROR: %s reports %r; the benchmark build is OpenSSL %s. Give --libcrypto PATH of the %s build." %
                  (libcrypto_path, ver, OPENSSL_EXPECTED, OPENSSL_EXPECTED))
            return 1
        keygens = dict((l, make_keygen(lib, algo, opts)) for l, (algo, opts) in parsed.items() if l in by_label)
        try:
            for c in conds:
                keygens[c["label"]]()
        except RuntimeError as e:
            print("ERROR: %s" % e)
            return 1
        tool_text, tool_path = "libcrypto: %s (%s)" % (ver, libcrypto_path), libcrypto_path

    # ---- session folder and log
    harness.ignore_sighup()
    out_root = a.out or os.path.join(harness.invoking_user_home(), "E2")
    session_id = time.strftime("%Y%m%d-%H%M%S") + "-" + host + suffix + ("-SMOKE" if a.smoke else "")
    sdir = os.path.join(out_root, session_id)
    os.makedirs(sdir, exist_ok=True)
    log = harness.SessionLog(os.path.join(sdir, "session.log"), verbose=a.verbose)
    live_on = {"on": False}
    log.hide = make_hide(log, live_on)
    sched_path = os.path.join(sdir, "schedule.csv")
    git = git_info()
    n_pairs = n_pairs_plan
    status = harness.StatusFile(os.path.join(out_root, "STATUS"), n_pairs, log.path)
    status.running(0, 0)
    log.screen("Method       : %s%s" % ("shell (openssl genpkey per key)" if method == "shell" else "library (in-process libcrypto)",
                                      "; clocks MHz: " + ", ".join(fmt_f(f) for f in freqs) if freqs else ""))
    if freq_notes:
        log.screen("Clocks       : typed -> used: " + ", ".join(freq_notes))
    log.screen("Plan         : %d pairs (%d runs), about %s, ETA %s%s" %
               (n_pairs, 2 * n_pairs, fmt_dur(total_est), clock_hhmm(total_est + 180), "   (resuming %s)" % resumed_from if resumed_from else ""))
    log.log("session %s; experimenter_e2 %s; e2_common %s; platform_env %s; harness %s" %
            (session_id, E2_VERSION, e2_common.E2_COMMON_VERSION, platform_env.PLATFORM_ENV_VERSION, harness.HARNESS_VERSION))
    log.log("command line: " + " ".join(sys.argv))
    log.log("groups: %s; method %s; conditions %s; Collector %s:%s; sub-experiment %r" % (session_filter, method, cond_path, a.collector, a.port, letter))
    log.log("host %s; kernel %s; python %s; git commit %s%s; seed %d" %
            (host, platform.release(), platform.python_version(), git[0], " (DIRTY)" if git[1] else "", seed))
    log.log(tool_text)
    if freqs:
        log.log("clocks (MHz): %s%s" % (", ".join(fmt_f(f) for f in freqs), "; as typed -> used: " + ", ".join(freq_notes) if freq_notes else ""))
    if resumed_from:
        log.log("resumed from %s" % resumed_from)

    env = platform_env.PlatformEnvironment(log=log.log, fan_confirmed=a.fan_confirmed, cpu_clock=a.cpu_clock, ntp_server=a.ntp_server,
                                         measure_clock=True)
    sender = Sender(a.collector, a.port, log.log)
    rf = open(os.path.join(sdir, "results.csv"), "w", newline="")
    rw = csv.DictWriter(rf, fieldnames=RESULT_COLS, lineterminator="\n")
    rw.writeheader()
    rf.flush()
    state = {"done": 0, "t0": time.time(), "est_done": 0.0, "act_done": 0.0, "t_pair0": None}
    abort_reason = None
    env_controls = {}
    base_flags = []
    done_count, failed_row, gate_row, thr_row = 0, 0, 0, 0
    override = False
    cal_lib = {}

    def eta(*_):
        """Clock time the session should end. The ratio of actual to estimated time so far may go below 1 (the estimates are
        generous); the part of the current pair already elapsed is taken off."""
        ratio = max(0.4, min(10.0, state["act_done"] / state["est_done"])) if state["est_done"] > 0 else 1.0
        left = (total_est - state["est_done"]) * ratio
        if state["t_pair0"] is not None:
            left -= time.time() - state["t_pair0"]
        return clock_hhmm(max(left, 0))

    def wait_for(msg, check, ok_msg=None):
        if a.no_prompts:
            log.log("(prompts off) " + msg)
            return None
        log.clear_inline()
        r = harness.operator_wait(msg, out_root, log.log, on_wait=lambda m: status.waiting(done_count, len(log.errors), m), check=check,
                                  short=True, ok_msg=ok_msg, say=lambda t: log.screen(t, file=True), override_after=3)
        status.running(done_count, len(log.errors))
        return r

    def check_network():
        if not harness.host_reachable(a.collector):
            return False, "no reply from the Collector at %s (is the testbed cable connected and the Collector box up?)" % a.collector
        if not a.internet_always and harness.internet_reachable():
            return False, "the internet IS reachable; E2 must run with the internet cable disconnected"
        return True, ""

    try:
        # network: BEFORE setup(), so nothing has been changed yet
        if a.internet_always:
            ok, why = check_network()
            if not ok and not a.no_prompts:
                wait_for("Testbed network problem: " + why + ". Fix it", check_network, ok_msg="  check: Collector reachable")
        else:
            override = bool(wait_for("Testbed cable connected and INTERNET cable / USB adapter DISCONNECTED?", check_network,
                                     ok_msg="  check: Collector reachable, internet not reachable"))
        if override:
            base_flags.append("internet_left_connected")
        meter_conf = confirm_meter(a, host, log.log)
        env.setup()
        env_controls = env.describe()
        cc = env_controls.get("cpu_clock") or {}
        if freqs:
            clock_txt = "clock pinned per pair (%s MHz), restored at the end" % ", ".join(fmt_f(f) for f in freqs)
        else:
            clock_txt = ("clock fixed at %.0f MHz" % (cc["target_khz"] / 1000.0) if cc.get("mode") == "fixed" and cc.get("target_khz")
                         else "clock left at the board's default")
        log.screen("Environment  : %s" % clock_txt)
        tr, tt = env_controls.get("t_ref_c"), getattr(env, "t_target", None)
        log.screen("Reference    : " + ("T_ref %.1f C" % tr if tr is not None else "T_ref n/a") +
                   (", thermal gate target %.1f C" % tt if tt is not None else ", no thermal gate") +
                   ("" if env_controls.get("t_ref_converged", True) else "  (T_REF DID NOT CONVERGE)"))
        # library: counts from an in-process rate of every condition, with the board set up as for the runs (at its session clock)
        if method == "library":
            if sched is None:
                log.screen("Calibrating  : %g s per condition, in-process" % a.calibrate_seconds)
                cal_lib = calibrate_inprocess(conds, keygens, a.calibrate_seconds, a.target_seconds, min_keys, log.log)
                for l, v in cal_lib.items():
                    spk_ref[l] = v[0]
                    n_ref[l] = v[2]
                sched = build_schedule(conds, freqs, n_ref, f_ref, a.repeats, not a.unshuffled, random.Random(seed), min_keys)
            spk = spk_dict(sched)
            expect_s = dict((k, next(r["n_target"] for r in sched if (r["label"], r["freq"]) == k) * v) for k, v in spk.items())
            n_pairs = len(sched)
            total_est = sum(est_pair(expect_s[(r["label"], r["freq"])]) for r in sched)
            status.total = n_pairs
        batches = dict((k, batch_of(v)) for k, v in spk.items()) if method == "library" else None
        write_schedule(sched_path, sched)
        with open(os.path.join(sdir, "session.json"), "w") as f:
            json.dump({"session_id": session_id, "e2_version": E2_VERSION, "e2_common_version": e2_common.E2_COMMON_VERSION,
                       "lib_version": platform_env.PLATFORM_ENV_VERSION, "harness_version": harness.HARNESS_VERSION,
                       "method": method, "subexp": letter, "clocks_mhz": freqs, "clocks_as_typed": freq_notes, "reference_mhz": f_ref,
                       "git_commit": git[0], "git_dirty": git[1], "command_line": sys.argv, "seed": seed, "conditions_file": cond_path,
                       "session_filter": session_filter, "collector": a.collector, "collector_port": a.port, "meter_confirmed": meter_conf,
                       "resumed_from": resumed_from, "openssl": ver, "tool_path": tool_path, "hostname": host, "kernel": platform.release(),
                       "python": platform.python_version(), "env_controls": env_controls,
                       "n_per_condition": dict(("%s@%s" % (l, fmt_f(fq)) if fq else l, next(r["n_target"] for r in sched if (r["label"], r["freq"]) == (l, fq)))
                                               for (l, fq) in spk),
                       "seconds_per_key": dict(("%s@%s" % (l, fmt_f(fq)) if fq else l, v) for (l, fq), v in spk.items()),
                       "args": vars(a)}, f, indent=1, default=str)
        ctx = {"a": a, "env": env, "log": log, "sender": sender, "session_id": session_id, "host": host, "keygens": keygens, "batches": batches,
               "spk": spk, "unit": unit, "sdir": sdir, "results_writer": rw, "results_file": rf, "env_controls": env_controls, "cmds": cmds, "oenv": oenv,
               "git": git, "openssl_version": ver, "libcrypto_path": libcrypto_path, "base_flags": base_flags, "eta": eta, "pair_text": "",
               "session_filter": session_filter, "meter_conf": meter_conf, "subexp": letter}
        live_on["on"] = True                       # from here routine lines flash on the status line
        current_mhz = None
        for pi, ent in enumerate(sched, 1):
            ctx["pair_text"] = "[pair %d/%d]" % (pi, n_pairs)
            t_pair0 = time.time()
            state["t_pair0"] = t_pair0
            if ent["freq"] is not None and ent["freq"] != current_mhz:
                res = env.set_clock_mhz(ent["freq"])        # raises EnvError if it does not read back: the session stops
                current_mhz = ent["freq"]
                log.log("clock pinned: %s MHz (limits now %s)" % (fmt_f(current_mhz), res["limits"]))
            live = run_one("LIVE", ent, ent["n_target"], None, ctx)
            runs = [live]
            if live["exc"] is None and live["status"] in ("ok", "max_duration") and live["n_completed"] >= 1:
                nul = run_one("NULL", ent, live["n_completed"], live["dut_time_to_run_s"], ctx)
                runs.append(nul)
            for r in runs:
                fl = [f for f in r["flags"] if f not in base_flags]
                log.screen("[pair %d/%d] %-16s %-4s %9d keys %10.3f s  %s  %s%s" %
                           (pi, n_pairs, cond_text(r["label"], r["freq_mhz"]), r["role"], r["n_completed"], r["dut_time_to_run_s"],
                            r["status"] if r["status"] != "ok" else "ok", time.strftime("%H:%M:%S"),
                            ("   " + "; ".join(f.upper() if f in ("throttled", "gate_not_met", "clocksync_failed", "freq_off") else f for f in fl)) if fl else ""),
                           file=True)
                if r["exc"] is not None:
                    raise r["exc"]
            pair_ok = len(runs) == 2 and all(r["status"] in ("ok", "max_duration") for r in runs)
            if not pair_ok:
                bad = [r for r in runs if r["status"] not in ("ok", "max_duration")]
                for r in bad:
                    log.error("%s: %s%s" % (r["run_id"], r["status"], " (%s)" % r["error"] if r.get("error") else ""))
                if len(runs) == 1:
                    log.log("pair %d (%s) incomplete: NULL skipped because the LIVE run failed" % (ent["slot"], cond_text(ent["label"], ent["freq"])))
            failed_row = 0 if pair_ok else failed_row + 1
            for r in runs:
                gate_row = gate_row + 1 if "gate_not_met" in r["flags"] else 0
                thr_row = thr_row + 1 if "throttled" in r["flags"] else 0
                if gate_row >= GATE_MISSES_ABORT or thr_row >= THROTTLED_ABORT:
                    break
            ent["done"] = 1 if pair_ok else 0         # a failed pair stays undone: --resume tries it again
            write_schedule(sched_path, sched)
            done_count = pi
            state["est_done"] += est_pair(expect_s[(ent["label"], ent["freq"])])
            state["act_done"] += time.time() - t_pair0
            state["t_pair0"] = None
            status.running(done_count, len(log.errors))
            if failed_row >= FAILED_PAIRS_ABORT:
                abort_reason = "%d failed pairs in a row" % failed_row
                break
            if gate_row >= GATE_MISSES_ABORT:
                abort_reason = ("thermal gate not met in %d runs in a row: the board did not reach the gate target in time. A run of misses "
                                "points at the setup, not at noise: fan position or strength changed since T_ref was taken, room "
                                "temperature, or T_ref taken while the board was still settling. Check the fan and the gate lines "
                                "in session.log, let it idle for 10-15 minutes and start again with --resume" % gate_row)
                break
            if thr_row >= THROTTLED_ABORT:
                abort_reason = "throttling seen in %d runs in a row: improve the cooling, then start again with --resume" % thr_row
                break
    except platform_env.EnvError as e:
        abort_reason = "environment error: %s" % e
        log.error(abort_reason)
    except KeyboardInterrupt:
        abort_reason = "interrupted"
    except SystemExit:
        abort_reason = "terminated"
        raise
    finally:
        try:
            env.teardown()
        finally:
            log.clear_inline()
            log.summary()
            if abort_reason:
                log.log("SESSION ABORTED: " + abort_reason)
                status.aborted(done_count, abort_reason)
                log.screen("SESSION ABORTED after %d/%d pairs: %s" % (done_count, n_pairs, abort_reason))
                log.screen("Resume with:  sudo python3 ~/pqc/E2/experimenter_e2.py --method %s --resume %s" % (method, sched_path))
            else:
                log.log("SESSION COMPLETE: %d/%d pairs, %d error(s)" % (done_count, n_pairs, len(log.errors)))
                status.completed(done_count, len(log.errors))
                log.screen("SESSION COMPLETE  %d/%d pairs  %d error(s)  %s   log: %s" %
                           (done_count, n_pairs, len(log.errors), fmt_dur(time.time() - state["t0"]), log.path))
            try:
                with open(os.path.join(sdir, "session.json")) as f:
                    sj = json.load(f)
                sj.update(aborted=abort_reason, pairs_done=done_count, errors=log.errors)
                with open(os.path.join(sdir, "session.json"), "w") as f:
                    json.dump(sj, f, indent=1, default=str)
            except (OSError, ValueError):
                pass
            rf.close()
            log.close()
            harness.chown_to_user(out_root)
    return 1 if abort_reason else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except platform_env.EnvError as e:
        if "must be run as root" not in str(e):
            print("ERROR:", e)
        sys.exit(1)
