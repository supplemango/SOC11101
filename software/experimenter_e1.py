#!/usr/bin/env python3
# VERSION v20261021.2345   (vYYYYMMDD.HHMM, UK local time of the last edit; bump on every change)
"""
experimenter_e1.py -- E1 platform-capability baseline wrapper (a feel for the grunt each DUT has).

Run it like every other experimenter:   sudo python3 ~/pqc/E1/experimenter_e1.py [options]
(the root rule applies to ALL programs on the library; the very first statement checks it).

E1 takes NO energy readings, so there is no Collector, no GETREADY/START/STOP and no meter. What it shares with the
other experiments is the environment: platform_env.PlatformEnvironment.setup() (governor and CPU clock fixed, external
fan prompt, background update/housekeeping jobs paused, T_ref measured), then BEFORE EVERY RUN: sync_clock() and the
thermal gate (burn, then cool to T_ref + 5 C, approached from above). Every run is then measured with the monotonic
clock while a side thread samples temperature and the CPU clock once a second.

Tools in this version (more are added by name in TOOLS):
  stress-ng       `stress-ng --cpu N -t S --metrics-brief`, N = 1 and N = all cores; metric = bogo ops/s (real time)
  mandelbrot      mandelbrot_w.py N (Benchmarks Game Python Mandelbrot, workers 1 and all); output is hashed and
                  discarded; metric = elapsed time at fixed work (also megapixels/s); the md5 must be identical on
                  every board or the work was not identical
  openssl-speed   `openssl speed -seconds S <alg>` with the testbed's OpenSSL 3.5.8 (--openssl PATH; default
                  /usr/local/ssl/bin/openssl, run with the LD_LIBRARY_PATH that sudo would otherwise drop); two runs per
                  repeat, each with its own sync and gate: variant rsa2048 (metric RSA-2048 sign/s) and variant mlkem768
                  (metric complete ML-KEM-768 exchanges/s = 1/(1/keygen+1/encaps+1/decaps); the raw keygen/encaps/decaps
                  rates stay in the run JSON), and variants aes256gcm and aes256cbc (`-bytes 16384 -evp <alg>`, one thread,
                  60 s each (10 s in smoke); metrics AES-256-GCM and AES-256-CBC MB/s). Sessions before v20261013.1631 had one run "1" doing RSA+ML-KEM.
  scimark2        NIST SciMark 2 (the binary Phoronix built); `-large` (as pts/scimark2) and the standard run; Mflops composite
  geekbench       `geekbench7 --cpu` (ONLINE tool: always uploads; scores comparable only within one Geekbench version)
  sbc-bench       `bash sbc-bench.sh -c` (works offline once its helpers are installed; ~8 min; runs after the other measured tools): 7-zip, memcpy/memset, AES (system OpenSSL) and its own
                  throttle/clock validation. Its 7-zip is part of this composite, so 7-zip is not run separately.

Order: repeat-major (repeat 1 of every tool/variant, then repeat 2, ...) so slow drift is spread across tools.
Failure policy as for E2: a failing tool run is recorded and flagged and the session carries on; 3 failed runs in a row
abort the session. Every caught error is printed again at the end. A one-line STATUS file is kept (see harness.py).

Outputs (outside the git tree), in <invoking user's home>/E1/<session_id>/ :
  session.log, results.jsonl (full record per run), results.csv (one summary row per run), run_NNN.json (with the 1 Hz
  trace), and <home>/E1/STATUS. Everything is handed back to the invoking user at the end.
"""
import argparse
import csv
import hashlib
import json
import os
import platform
import re
import resource
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.dont_write_bytecode = True           # running as root: never leave root-owned __pycache__ in the git tree
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
import platform_env                       # noqa: E402
import harness                            # noqa: E402

E1_VERSION = "v20261021.2345"
platform_env.require_root(os.path.basename(sys.argv[0]))      # FIRST statement: nothing has been touched yet

TOOLS_BUILT = ("stress-ng", "mandelbrot", "openssl-speed", "scimark2", "geekbench", "sbc-bench")
TOOLS_PLANNED = ()
# Tools that need the internet (they upload results / install helpers) run in the ONLINE phase, after every
# measured tool; everything else is OFFLINE: testbed network only (JUMP reachable for the clock sync), no internet.
ONLINE_TOOLS = ("geekbench",)      # needs the internet (the preview build always uploads); runs last. sbc-bench works offline (tested 2026-10-11)
SMOKE = {"geekbench_repeats": 1, "sbcbench_repeats": 1, "repeats": 1, "stressng_secs": 20, "mandelbrot_n": 500, "speed_secs": 5}      # bare minimum counts
AES_SECS, AES_SECS_SMOKE = 60, 10        # AES-256 GCM/CBC `openssl speed`: one timed phase per run, so a long run is cheap; fixed, not an option
SWAP_GROWTH_FLAG_MB = 16        # swap use growing by more than this during one run flags it (a CPU-bound tool should not touch swap)
STANDARD = {"geekbench_repeats": 3, "sbcbench_repeats": 3, "repeats": 5, "stressng_secs": 60, "mandelbrot_n": 4000, "speed_secs": 10}


# ---------------------------------------------------------------------------------------------- helpers
def tail(text, n=4000):
    return text[-n:] if text else ""


def run_capture(cmd, timeout=None, env=None, cwd=None):   # env=None: inherit
    """Run a command, capture stdout+stderr as text. Returns (rc, text, rusage-delta cpu seconds). rc -9 on timeout."""
    r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, env=env, cwd=cwd)
        rc, out = p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        rc, out = -9, (e.stdout or b"").decode("utf-8", "replace") + "\n[timeout]"
    except OSError as e:
        rc, out = 127, str(e)
    r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (r1.ru_utime - r0.ru_utime) + (r1.ru_stime - r0.ru_stime)
    return rc, out, cpu


def cmd_output(cmd, timeout=15, env=None):
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, env=env).stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.TimeoutExpired):
        return None


def read_swap_mb():
    """(total_mb, used_mb) of swap, or None if it cannot be read. Linux: /proc/meminfo. macOS: `sysctl vm.swapusage`
    (macOS makes swap files on demand and cannot switch swap off, so only GROWTH during a run is ever flagged)."""
    try:
        if sys.platform == "darwin":
            m = re.search(r"total = ([\d.]+)([MG]).*?used = ([\d.]+)([MG])", cmd_output(["sysctl", "-n", "vm.swapusage"], 5) or "")
            if not m:
                return None
            k = {"M": 1.0, "G": 1024.0}
            return float(m.group(1)) * k[m.group(2)], float(m.group(3)) * k[m.group(4)]
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith(("SwapTotal:", "SwapFree:")):
                    info[line.split(":")[0]] = int(line.split()[1])
        return info["SwapTotal"] / 1024.0, (info["SwapTotal"] - info["SwapFree"]) / 1024.0
    except (OSError, KeyError, ValueError):
        return None


E1_SWAPFILE = "/swapfile-e1"


def swap_enable(mb, log):
    """Create and switch on a swap file of `mb` MB (root). Returns (ok, message). Used only around Geekbench on a board whose
    RAM is too small for it (DUT-RISCV). Our own file name, so a swap file of the operator's is never touched."""
    try:
        if os.path.exists(E1_SWAPFILE):                     # left over from an interrupted session
            swap_disable(log)
        free_mb = shutil.disk_usage("/").free / 1048576
        if free_mb < mb + 2048:
            return False, f"not enough free disk space for a {mb} MB swap file ({free_mb:.0f} MB free)"
        rc, out, _ = run_capture(["fallocate", "-l", f"{mb}M", E1_SWAPFILE])
        if rc != 0:                                         # filesystems without fallocate support
            rc, out, _ = run_capture(["dd", "if=/dev/zero", f"of={E1_SWAPFILE}", "bs=1M", f"count={mb}"])
        if rc != 0:
            raise OSError(f"cannot create {E1_SWAPFILE}: {tail(out, 200)}")
        os.chmod(E1_SWAPFILE, 0o600)
        for cmd in (["mkswap", E1_SWAPFILE], ["swapon", E1_SWAPFILE]):
            rc, out, _ = run_capture(cmd)
            if rc != 0:
                raise OSError(f"{' '.join(cmd)} failed: {tail(out, 200)}")
        sw = read_swap_mb()
        log(f"swap: {E1_SWAPFILE} of {mb} MB switched ON for Geekbench (swap total now {'?' if sw is None else f'{sw[0]:.0f}'} MB)")
        return True, ""
    except OSError as e:
        swap_disable(log)
        return False, str(e)


def swap_disable(log):
    """Switch our swap file off and delete it. Returns True when it is gone."""
    if not os.path.exists(E1_SWAPFILE):
        return True
    rc, out, _ = run_capture(["swapoff", E1_SWAPFILE])
    try:
        active = E1_SWAPFILE in open("/proc/swaps").read()
    except OSError:
        active = False
    if rc != 0 and active:
        log(f"swap: swapoff {E1_SWAPFILE} FAILED: {tail(out, 200)} (file kept; switch it off by hand)")
        return False
    try:
        os.remove(E1_SWAPFILE)
    except OSError as e:
        log(f"swap: cannot delete {E1_SWAPFILE}: {e}")
        return False
    sw = read_swap_mb()
    log(f"swap: {E1_SWAPFILE} switched OFF and deleted (swap total now {'?' if sw is None else f'{sw[0]:.0f}'} MB)")
    return True


class Sampler(threading.Thread):
    """1 Hz side sampler: temperature and the busy CPUs' clocks. Energy is not measured in E1, so the sampling cost is
    irrelevant to the result. The clock list is only meaningful while cores are busy (idle cores report stale values)."""

    def __init__(self, env, period=1.0, log=None, label="", beat_s=60.0, progress=None, live_s=10.0):
        super().__init__(daemon=True)
        self.env, self.period = env, period
        self.log, self.label, self.beat_s = log, label, beat_s       # progress line while a tool is quiet (all output is captured)
        self._next_beat = beat_s
        self.progress, self.live_s, self._next_live = progress, live_s, 0.0     # screen-only live line (not in the log); first update at 0 s
        self.stop_evt = threading.Event()
        self.trace = []
        self.swap_total_mb = None
        self.t0 = time.monotonic()

    def run(self):
        while not self.stop_evt.is_set():
            t = time.monotonic()
            try:
                temp = self.env.read_temp()
            except Exception:
                temp = None
            try:
                fr = self.env.read_cur_freqs()
            except Exception:
                fr = []
            sw = read_swap_mb()
            self.trace.append({"t": round(t - self.t0, 2), "temp_c": temp, "cpu_khz": fr,
                               "swap_used_mb": None if sw is None else round(sw[1], 1)})
            if sw is not None:
                self.swap_total_mb = round(sw[0], 1)
            if self.log and (t - self.t0) >= self._next_beat:
                self._next_beat += self.beat_s
                mhz = f"{max(fr) / 1000:.0f} MHz" if fr else "n/a"
                tc = f"{temp:.1f} C" if temp is not None else "n/a"
                try:
                    self.log(f"   ... {self.label} still running, {int(t - self.t0)} s in; {tc}, {mhz}")
                except Exception:
                    pass
            if self.progress and (t - self.t0) >= self._next_live:
                self._next_live += self.live_s
                try:
                    mhz = f"{max(fr) / 1000:.0f} MHz" if fr else "n/a"
                    tc = f"{temp:.1f} C" if temp is not None else "n/a"
                    el = int(t - self.t0)
                    self.progress(f"running {el // 60:02d}:{el % 60:02d}   {tc}" + ("" if mhz == "n/a" else f"   {mhz}"))
                except Exception:
                    pass
            self.stop_evt.wait(max(0.0, self.period - (time.monotonic() - t)))

    def finish(self):
        self.stop_evt.set()
        self.join(timeout=5)
        temps = [x["temp_c"] for x in self.trace if x["temp_c"] is not None]
        freqs = [f for x in self.trace for f in x["cpu_khz"]]
        sws = [x["swap_used_mb"] for x in self.trace if x.get("swap_used_mb") is not None]
        return {"samples": len(self.trace),
                "swap_total_mb": self.swap_total_mb,
                "swap_used_start_mb": sws[0] if sws else None, "swap_used_max_mb": max(sws) if sws else None,
                "swap_growth_mb": round(max(sws) - sws[0], 1) if sws else None,
                "temp_start_c": temps[0] if temps else None, "temp_end_c": temps[-1] if temps else None,
                "temp_max_c": max(temps) if temps else None,
                "cpu_khz_min": min(freqs) if freqs else None, "cpu_khz_max": max(freqs) if freqs else None,
                "cpu_khz_mean": round(sum(freqs) / len(freqs)) if freqs else None}


def openssl_path(a):
    """--openssl PATH, else the testbed's own build (/usr/local/ssl/bin/openssl) if present, else the first on PATH.
    (Not simply `openssl`: sudo resets PATH, and the Mac has three.)"""
    if a.openssl:
        return a.openssl
    if os.path.isfile("/usr/local/ssl/bin/openssl"):
        return "/usr/local/ssl/bin/openssl"
    return harness.find_tool("openssl")


# ---------------------------------------------------------------------------------------------- tool runners
# Each runner returns a dict: {rc, status, metric_name, metric_value, metric_unit, elapsed_s, cpu_time_s, flags[], detail{}}
def run_stress_ng(a, variant, secs):
    workers = os.cpu_count() if variant == "all" else 1
    exe = a.stress_ng or harness.find_tool("stress-ng")
    if not exe:
        return {"rc": 127, "status": "stress-ng not found", "flags": ["tool_missing"], "detail": {}}
    cmd = [exe, "--cpu", str(workers), "-t", f"{int(secs)}s", "--metrics-brief"]
    if a.stressng_method != "all":
        cmd += ["--cpu-method", a.stressng_method]
    t0 = time.monotonic()
    rc, out, cpu = run_capture(cmd, timeout=secs * 3 + 120)
    el = time.monotonic() - t0
    res = {"rc": rc, "status": "ok" if rc == 0 else f"rc={rc}", "elapsed_s": el, "cpu_time_s": cpu, "flags": [],
           "detail": {"cmd": cmd, "workers": workers, "output_tail": tail(out)}}
    m = None
    for m in re.finditer(r"\bcpu\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)", out):
        pass
    if m:
        ops, real, usr, sysm, bo_real, bo_cpu = int(m.group(1)), *[float(m.group(i)) for i in range(2, 7)]
        res.update(metric_name="bogo_ops_per_s_real", metric_value=bo_real, metric_unit="bogo ops/s")
        res["detail"].update(bogo_ops=ops, real_time_s=real, usr_time_s=usr, sys_time_s=sysm, bogo_ops_per_s_usr_sys=bo_cpu)
        # the 2026-09-27 lesson: a clock step made stress-ng report ~5 days for a 300 s run. Check it.
        if abs(real - secs) > 0.05 * secs + 2.0 or abs(real - el) > 0.05 * el + 2.0:
            res["flags"].append("time_suspect")
    elif rc == 0:
        res["status"], res["rc"] = "no metrics line found", 1
    if "scaling governors" in out and "powersave" in out:
        res["flags"].append("stress_ng_governor_warning")
    return res


def run_mandelbrot(a, variant, secs):
    workers = os.cpu_count() if variant == "all" else 1
    n = a.mandelbrot_n
    script = os.path.join(HERE, "mandelbrot_w.py")
    cmd = [sys.executable, script, str(n)]
    env = dict(os.environ, MANDEL_WORKERS=str(workers))
    expected = len(f"P4\n{n} {n}\n".encode()) + n * ((n + 7) // 8)
    md5, nbytes = hashlib.md5(), 0
    r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    errf = tempfile.TemporaryFile()
    t0 = time.monotonic()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, env=env)
    timed_out = []
    watchdog = threading.Timer(a.mandelbrot_timeout, lambda: (timed_out.append(1), p.kill()))
    watchdog.start()
    try:
        while True:
            chunk = p.stdout.read(1 << 20)
            if not chunk:
                break
            md5.update(chunk)
            nbytes += len(chunk)
        rc = p.wait()
    finally:
        watchdog.cancel()
    el = time.monotonic() - t0
    r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (r1.ru_utime - r0.ru_utime) + (r1.ru_stime - r0.ru_stime)
    errf.seek(0)
    err = errf.read().decode("utf-8", "replace")
    errf.close()
    flags = []
    if timed_out:
        rc = -9
        flags.append("timeout")
    if rc == 0 and nbytes != expected:
        flags.append("output_size_wrong")
    res = {"rc": rc, "status": "ok" if rc == 0 and not flags else ("rc=%s" % rc if rc else ",".join(flags)),
           "elapsed_s": el, "cpu_time_s": cpu, "flags": flags,
           "detail": {"cmd": cmd, "n": n, "workers": workers, "md5": md5.hexdigest(), "bytes": nbytes,
                      "bytes_expected": expected, "python": platform.python_version(), "stderr_tail": tail(err, 1000)}}
    if rc == 0:
        res.update(metric_name=f"mpix_per_s_n{n}", metric_value=round(n * n / el / 1e6, 4), metric_unit="Mpixel/s")
        res["detail"]["elapsed_s_is_the_headline_metric"] = True
        res["detail"]["cpu_utilisation_cores"] = round(cpu / el, 2) if el else None
    return res


SPEED_ALG = {"rsa2048": "rsa2048", "mlkem768": "ML-KEM-768", "aes256gcm": "aes-256-gcm", "aes256cbc": "aes-256-cbc",
             "chacha20poly1305": "chacha20-poly1305"}
BULK_VARIANTS = ("aes256gcm", "aes256cbc", "chacha20poly1305")      # bulk symmetric throughput: one thread, 16 KB blocks, 60 s
OFF_BY_DEFAULT = {("openssl-speed", "aes256cbc")}                    # dropped from the default plan 2026-10-21; run it with --variants aes256cbc


def run_openssl_speed(a, variant, secs):
    exe = openssl_path(a)
    if not exe:
        return {"rc": 127, "status": "openssl not found", "flags": ["tool_missing"], "detail": {}}
    algs = [SPEED_ALG[variant]]
    isaes = variant in BULK_VARIANTS
    secs_t = int(AES_SECS_SMOKE if a.smoke else AES_SECS) if isaes else int(a.speed_secs)
    cmd = [exe, "speed", "-seconds", str(secs_t)]
    if isaes:        # bulk symmetric throughput, one thread, 16 KB blocks (GCM = what TLS uses; CBC = comparable with sbc-bench)
        cmd += ["-bytes", "16384", "-evp", algs[0]]
    else:
        cmd += algs
    t0 = time.monotonic()
    rc, out, cpu = run_capture(cmd, timeout=secs_t * len(algs) * 3 + 120, env=harness.openssl_runtime_env(exe))
    el = time.monotonic() - t0
    res = {"rc": rc, "status": "ok" if rc == 0 else f"rc={rc}", "elapsed_s": el, "cpu_time_s": cpu, "flags": [],
           "detail": {"cmd": cmd, "openssl": exe, "output_tail": tail(out, 6000), "parsed_best_effort": {}}}
    parsed = {}
    for line in out.splitlines():
        m = re.match(r"\s*(AES-256-GCM|aes-256-cbc|ChaCha20-Poly1305)\s+([\d.]+)k\s*$", line, re.I)            # "AES-256-GCM   10054390.11k" (kB/s)
        if m and isaes:
            parsed[m.group(1).lower()] = {"mb_per_s": round(float(m.group(2)) / 1000.0, 2)}
            continue
        # OpenSSL 3.5.8 rows (verified on DUT-Pi5 2026-10-11):
        #   rsa  2048 bits 0.003564s 0.000097s 0.000099s 0.003559s    280.6  10265.0  10089.9    281.0
        #                  ML-KEM-768 0.000056s 0.000033s 0.000052s   17822.6   30528.8   19373.9
        m = re.match(r"\s*rsa\s+(\d+)\s+bits(?:\s+[\d.]+s){4}\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*$", line)
        if m:
            parsed["rsa%s" % m.group(1)] = {"sign_per_s": float(m.group(2)), "verify_per_s": float(m.group(3)),
                                            "encrypt_per_s": float(m.group(4)), "decrypt_per_s": float(m.group(5))}
            continue
        m = re.match(r"\s*(ML-KEM-\d+|ML-DSA-\d+)((?:\s+[\d.]+s){3})((?:\s+[\d.]+){3})\s*$", line)
        if m and m.group(1).lower() in [x.lower() for x in algs]:
            r = [float(x) for x in m.group(3).split()]
            parsed[m.group(1)] = {"keygen_per_s": r[0], "encaps_per_s": r[1], "decaps_per_s": r[2]}
    res["detail"]["parsed_best_effort"] = parsed
    if variant == "rsa2048" and "rsa2048" in parsed:
        res.update(metric_name="rsa2048_sign_per_s", metric_value=parsed["rsa2048"]["sign_per_s"], metric_unit="sign/s")
    for v, key, mname in (("aes256gcm", "aes-256-gcm", "openssl_aes256gcm_mb_per_s"), ("aes256cbc", "aes-256-cbc", "openssl_aes256cbc_mb_per_s"),
                          ("chacha20poly1305", "chacha20-poly1305", "openssl_chacha20poly1305_mb_per_s")):
        if variant == v and key in parsed:
            res.update(metric_name=mname, metric_value=parsed[key]["mb_per_s"], metric_unit="MB/s")
    d = parsed.get("ML-KEM-768")
    if variant == "mlkem768" and d and all(d.get(k) for k in ("keygen_per_s", "encaps_per_s", "decaps_per_s")):
        ex = 1.0 / (1.0 / d["keygen_per_s"] + 1.0 / d["encaps_per_s"] + 1.0 / d["decaps_per_s"])
        res.update(metric_name="mlkem768_exchange_per_s", metric_value=round(ex, 1), metric_unit="exchanges/s")
    return res


def scimark2_path(a):
    """--scimark2, else the binary Phoronix built (pts/scimark2-*/scimark2_files/scimark2) in the invoking user's home,
    else ~/bench/scimark2/scimark2."""
    import glob
    if a.scimark2:
        return a.scimark2
    home = harness.invoking_user_home()
    for pat in (os.path.join(home, ".phoronix-test-suite/installed-tests/pts/scimark2-*/scimark2_files/scimark2"),
                os.path.join(home, "bench/scimark2/scimark2")):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return None


def run_scimark2(a, variant, secs):
    """variant 'large' = `scimark2 -large` (what Phoronix pts/scimark2 runs, so comparable with the 2026-09 figures);
    'small' = the standard cache-resident run. Single-threaded; ~30 s. Output verified on DUT-Pi5 2026-10-11."""
    exe = scimark2_path(a)
    if not exe or not os.path.isfile(exe):
        return {"rc": 127, "status": "scimark2 not found", "flags": ["tool_missing"], "detail": {}}
    cmd = [exe] + (["-large"] if variant == "large" else [])
    t0 = time.monotonic()
    rc, out, cpu = run_capture(cmd, timeout=600, cwd=os.path.dirname(exe))
    el = time.monotonic() - t0
    res = {"rc": rc, "status": "ok" if rc == 0 else f"rc={rc}", "elapsed_s": el, "cpu_time_s": cpu, "flags": [],
           "detail": {"cmd": cmd, "output_tail": tail(out, 3000)}}
    m = re.search(r"Composite Score:\s+([\d.]+)", out)
    kern = {}
    for key, pat in (("fft", r"FFT\s+Mflops:\s+([\d.]+)"), ("sor", r"SOR\s+Mflops:\s+([\d.]+)"),
                     ("montecarlo", r"MonteCarlo:\s+Mflops:\s+([\d.]+)"),
                     ("sparse", r"Sparse matmult\s+Mflops:\s+([\d.]+)"), ("lu", r"LU\s+Mflops:\s+([\d.]+)")):
        mm = re.search(pat, out)
        if mm:
            kern[key] = float(mm.group(1))
    res["detail"]["kernels_mflops"] = kern
    if m and len(kern) == 5:
        res.update(metric_name="composite_mflops", metric_value=float(m.group(1)), metric_unit="Mflops")
    elif rc == 0:
        res["status"] = "unparsed"
        res["flags"].append("parse_failed")
    return res


def geekbench_path(a):
    """--geekbench, else the newest ~/bench/Geekbench-*/geekbench7 in the invoking user's home."""
    import glob
    if a.geekbench:
        return a.geekbench
    hits = sorted(glob.glob(os.path.join(harness.invoking_user_home(), "bench", "Geekbench-*", "geekbench7")))
    return hits[-1] if hits else None


def run_geekbench(a, variant, secs):
    """`geekbench7 --cpu`: ONLINE tool (the preview build refuses to run offline and always uploads the result).
    Output format verified on DUT-Pi5 with Geekbench 7.1.0 Preview, 2026-10-11 (7.0.0 Preview stopped silently with
    exit 255 on that board, so the VERSION matters: scores are only comparable within one version).
    Headline = multi-core score; the single-core score is a second CSV row; the result URL is kept (the private
    'claim' key printed by Geekbench is removed from the stored text)."""
    exe = geekbench_path(a)
    if not exe or not os.path.isfile(exe):
        return {"rc": 127, "status": "geekbench not found", "flags": ["tool_missing"], "detail": {}}
    if a.geekbench_swap_mb:
        ok, why = swap_enable(a.geekbench_swap_mb, a.log)
        if not ok:
            return {"rc": 1, "status": "swap setup failed", "flags": ["swap_setup_failed"], "detail": {"error": why}}
    try:
        t0 = time.monotonic()
        rc, out, cpu = run_capture([exe, "--cpu"], timeout=a.geekbench_timeout, cwd=os.path.dirname(exe))
        el = time.monotonic() - t0
    finally:
        if a.geekbench_swap_mb and not swap_disable(a.log):
            a.log("swap: WARNING swap was left on after Geekbench")
    safe = re.sub(r"(claim\?key=)\w+", r"\1REDACTED", out)
    res = {"rc": rc, "status": "ok" if rc == 0 else f"rc={rc}", "elapsed_s": el, "cpu_time_s": cpu, "flags": [],
           "detail": {"cmd": [exe, "--cpu"], "output_tail": tail(safe, 3000), "swap_file_mb": a.geekbench_swap_mb or None}}
    ver = re.search(r"Geekbench\s+(\d+\.\d+\.\d+)\s*(\w*)", out)
    single = re.search(r"Single-Core Score\s+(\d+)", out)
    multi = re.search(r"Multi-Core Score\s+(\d+)", out)
    url = re.search(r"view your results online:\s*(https://\S+)", out)
    res["detail"].update(version=ver.group(1) if ver else None, build=ver.group(2) if ver else None,
                         result_url=url.group(1) if url else None, uploaded=bool(url))
    if single and multi:
        res.update(metric_name="geekbench_multi_score", metric_value=int(multi.group(1)), metric_unit="points")
        res["extra_metrics"] = [("geekbench_single_score", int(single.group(1)), "points")]
        if not url:
            res["flags"].append("not_uploaded")
    elif rc == 0:
        res["status"] = "unparsed"
        res["flags"].append("parse_failed")
    return res


def cpufreq_snapshot(sysroot="/"):
    """governor, min and max of every cpufreq policy (Linux), to see whether a tool left them changed."""
    import glob
    snap = {}
    for pol in sorted(glob.glob(os.path.join(sysroot, "sys/devices/system/cpu/cpufreq/policy*"))):
        d = {}
        for k in ("scaling_governor", "scaling_min_freq", "scaling_max_freq"):
            try:
                d[k] = open(os.path.join(pol, k)).read().strip()
            except OSError:
                d[k] = None
        snap[os.path.basename(pol)] = d
    return snap


def sbcbench_path(a):
    return a.sbc_bench or os.path.join(harness.invoking_user_home(), "bench", "sbc-bench.sh")


SBC_GOOD_VALIDATION = ("Measured clockspeed not lower than advertised max CPU clockspeed",
                       "Background activity (%system) OK", "No throttling")
SBC_INFO_PREFIXES = ("Powercap detected", "No swapping")      # DUT-PC (Intel RAPL): informational line, not a complaint (smoke 2026-10-12)


def sbc_line_known(v):
    return v in SBC_GOOD_VALIDATION or v.startswith(SBC_INFO_PREFIXES)


def run_sbc_bench(a, variant, secs):
    """`sudo bash sbc-bench.sh -c` (ThomasKaiser/sbc-bench v0.9.72). Its first run needs the internet to install helpers
    (lm-sensors, links, p7zip 16.02, tinymembench, ramlat, mhz, cpufetch); later runs work OFFLINE (tested on DUT-Pi5
    2026-10-11; its pastebin upload then fails harmlessly). About 8 min on DUT-Pi5 (cpuminer could not be built there, so that step is skipped).
    sbc-bench steps the CPU through its own OPP list while it runs, so it goes LAST; the library restores the
    governor settings at teardown. Parsed from the screen output (verified on DUT-Pi5 2026-10-11): memcpy/memset,
    7-zip totals (3 runs) and single-thread, AES-256-CBC 16 KB lines (run twice), and the 'Results validation'
    bullets. The full text is kept in the run record; sbc-bench also writes /var/log/sbc-bench.log."""
    exe = sbcbench_path(a)
    if not exe or not os.path.isfile(exe):
        return {"rc": 127, "status": "sbc-bench.sh not found", "flags": ["tool_missing"], "detail": {}}
    freq0 = cpufreq_snapshot(a.sysroot)
    t0 = time.monotonic()
    rc, out, cpu = run_capture(["/bin/bash", exe, "-c"], timeout=3600, cwd=os.path.dirname(exe))
    el = time.monotonic() - t0
    res = {"rc": rc, "status": "ok" if rc == 0 else f"rc={rc}", "elapsed_s": el, "cpu_time_s": cpu, "flags": [],
           "detail": {"cmd": ["/bin/bash", exe, "-c"], "output_tail": tail(out, 2500), "output_full": out}}
    d = res["detail"]
    freq1 = cpufreq_snapshot(a.sysroot)
    d["cpufreq_before"], d["cpufreq_after"] = freq0, freq1
    if freq0 != freq1:
        res["flags"].append("cpufreq_changed_by_tool")        # the next run would not be at the pinned governor/limits
    m = re.search(r"7-zip total scores \(3 consecutive runs\):\s*([\d,]+),\s*single-threaded:\s*(\d+)", out)
    multi = [int(x) for x in m.group(1).split(",")] if m else []
    single = int(m.group(2)) if m else None
    mc = re.search(r"memcpy:\s*([\d.]+)\s*MB/s", out)
    ms = re.search(r"memset:\s*([\d.]+)\s*MB/s", out)
    aes = [float(x) / 1000.0 for x in re.findall(r"aes-256-cbc\s+(?:[\d.]+k\s+){5}([\d.]+)k", out)]   # 16384-byte column, kB/s -> MB/s
    val = []
    blk = re.search(r"Results validation:\s*\n((?:\s*\*.*\n|\s*\n)+)", out)
    if blk:
        val = [ln.strip().lstrip("* ").strip() for ln in blk.group(1).splitlines() if ln.strip().startswith("*")]
    d.update(sevenzip_multi_runs=multi, sevenzip_single=single, memcpy_mb_s=float(mc.group(1)) if mc else None,
             memset_mb_s=float(ms.group(1)) if ms else None, aes256cbc_16k_mb_s_runs=aes, validation=val)
    # A line we do not know is something sbc-bench itself complained about: flag the run for review.
    # A known-good line that is merely absent is NOT a problem (the RISC-V board never prints "No throttling":
    # sbc-bench cannot read a throttle state there; the library's own throttle probe covers it): recorded only.
    if [v for v in val if not sbc_line_known(v)]:
        res["flags"].append("sbc_validation_review")
    d["validation_not_reported"] = [g for g in SBC_GOOD_VALIDATION if g not in val]
    if not val:
        res["flags"].append("sbc_validation_missing")
    if multi and single and mc and ms:
        res.update(metric_name="sbcbench_7zip_multi_mips", metric_value=round(sum(multi) / len(multi), 1), metric_unit="7-zip MIPS (mean of 3)")
        res["extra_metrics"] = [("sbcbench_7zip_single_mips", single, "7-zip MIPS"),
                                ("sbcbench_memcpy", float(mc.group(1)), "MB/s"),
                                ("sbcbench_memset", float(ms.group(1)), "MB/s")]
        # sbc-bench's AES test can print readings taken at different CPU clocks in one run (DUT-PC: about 941 MB/s at
        # 2600 MHz and 1304 MB/s at 3600 MHz; their ratio is the clock ratio). A mean of those is not a measurement:
        # only report AES when every reading agrees within 2 %; the raw readings stay in the run JSON.
        if aes and max(aes) / min(aes) <= 1.02:
            res["extra_metrics"].append(("sbcbench_aes256cbc_16k", round(sum(aes) / len(aes), 1), "MB/s (system OpenSSL)"))
        elif aes:
            d["aes256cbc_note"] = "readings differ by more than 2% (taken at different CPU clocks): metric omitted"
    elif rc == 0:
        res["status"] = "unparsed"
        res["flags"].append("parse_failed")
    return res


# tool name -> (runner, variants, seconds-per-run for the schedule estimate)
TOOLS = {
    "stress-ng": (run_stress_ng, ("1", "all")),
    "mandelbrot": (run_mandelbrot, ("1", "all")),
    "openssl-speed": (run_openssl_speed, ("rsa2048", "mlkem768", "aes256gcm", "aes256cbc", "chacha20poly1305")),
    "scimark2": (run_scimark2, ("large", "small")),
    "geekbench": (run_geekbench, ("cpu",)),
    "sbc-bench": (run_sbc_bench, ("c",)),
}


# ---------------------------------------------------------------------------------------------- main
CSV_COLS = ["session_id", "run_id", "tool", "variant", "repeat", "status", "metric_name", "metric_value", "metric_unit",
            "elapsed_s", "cpu_time_s", "temp_start_c", "temp_end_c", "temp_max_c", "cpu_khz_min", "cpu_khz_max",
            "cpu_khz_mean", "swap_total_mb", "swap_used_max_mb", "throttled", "gate_met", "sync_ok", "sync_error_ms", "smoke", "flags"]



# ------------------------------------------------------------------ screen story: names, formats, estimates, end table
PAUSE_ALLOWANCE_S = 180   # padded onto the session ETA for operator prompts; shrinks to nothing as the session runs
SETUP_S = 75      # fan question + the 60 s reference-temperature window, before the first run
TOOL_LABEL = {("stress-ng", "1"): "stress-ng 1 core", ("stress-ng", "all"): "stress-ng all cores",
              ("mandelbrot", "1"): "mandelbrot 1 core", ("mandelbrot", "all"): "mandelbrot all cores",
              ("openssl-speed", "rsa2048"): "openssl RSA-2048 sign", ("openssl-speed", "mlkem768"): "openssl ML-KEM-768 exch", ("openssl-speed", "aes256gcm"): "openssl AES-256-GCM", ("openssl-speed", "aes256cbc"): "openssl AES-256-CBC", ("openssl-speed", "chacha20poly1305"): "openssl ChaCha20-Poly1305",
              ("scimark2", "large"): "scimark2 large", ("scimark2", "small"): "scimark2 small",
              ("sbc-bench", "c"): "sbc-bench", ("geekbench", "cpu"): "geekbench"}
METRIC_LABEL = {"rsa2048_sign_per_s": "openssl RSA-2048 sign", "mlkem768_exchange_per_s": "openssl ML-KEM-768 exchange", "openssl_aes256gcm_mb_per_s": "openssl AES-256-GCM", "openssl_aes256cbc_mb_per_s": "openssl AES-256-CBC", "openssl_chacha20poly1305_mb_per_s": "openssl ChaCha20-Poly1305", "sbcbench_7zip_multi_mips": "sbc-bench 7-zip multi",
                "sbcbench_7zip_single_mips": "sbc-bench 7-zip single", "sbcbench_memcpy": "sbc-bench memcpy",
                "sbcbench_memset": "sbc-bench memset", "sbcbench_aes256cbc_16k": "sbc-bench AES-256-CBC 16k",
                "geekbench_multi_score": "geekbench multi", "geekbench_single_score": "geekbench single"}
FLAG_TEXT = {"cpufreq_changed_by_tool": "clock set by tool; pins re-applied", "internet_left_connected": "internet left connected (operator override)", "sync_not_ok": "CLOCK SYNC FAILED",
             "gate_not_met": "THERMAL GATE NOT MET", "gate_skipped": "gate skipped", "throttled": "THROTTLED",
             "swap_used": "SWAP USED", "pin_not_restored": "PINS NOT RESTORED", "sync_skipped": "sync skipped",
             "sbc_validation_review": "sbc-bench validation: REVIEW", "sbc_validation_missing": "sbc-bench validation missing"}
QUALIFIER_FLAGS = ("cpufreq_changed_by_tool", "internet_left_connected")
CV_LIMIT_PCT = 2.0


def tool_label(tool, variant):
    return TOOL_LABEL.get((tool, variant), f"{tool} {variant}")


def metric_label(tool, variant, metric_name):
    if metric_name in METRIC_LABEL:
        return METRIC_LABEL[metric_name]
    m = re.match(r"mpix_per_s(?:_n(\d+))?$", metric_name or "")
    if m:
        return f"mandelbrot {'1 core' if variant == '1' else 'all cores'}" + (f" (n={m.group(1)})" if m.group(1) else "")
    if metric_name == "composite_mflops":
        return f"scimark2 {variant}"
    if metric_name == "bogo_ops_per_s_real":
        return tool_label(tool, variant)
    return f"{tool} {metric_name}"


def fmt_val(v):
    if v is None:
        return "-"
    v = float(v)
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 100:
        return f"{v:.1f}"
    if abs(v) >= 10:
        return f"{v:.2f}"
    return f"{v:.4g}"


def short_unit(u):
    return (u or "").split(" (")[0]


def flag_words(flags, drop=("smoke",)):
    return "; ".join(FLAG_TEXT.get(f, f) for f in flags if f not in drop)


HISTORY = {"elapsed": {}, "overhead": {}}      # filled from this board's earlier real sessions (load_history)
_RUN_START = re.compile(r"^(\d\d):(\d\d):(\d\d) --- run (\d+)/\d+: (\S+) variant=(\S+) repeat=\d+ ---")
_RUN_END = re.compile(r"^(\d\d):(\d\d):(\d\d) run (\d+): ok; (\S+?)=[^;]*; elapsed ([\d.]+) s;")


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])


def load_history(out_root, skip_dir=None):
    """Timings of this board's earlier REAL sessions, read from their session.log files (smoke and the current session
    are skipped). Returns {"elapsed": {(tool, variant, tag): [tool seconds]}, "overhead": {tool: [seconds around the
    tool: clock sync + thermal gate + start-up]}}. Mandelbrot's tag is the n in its metric name, so a session at another
    n is not mixed in."""
    el, ov = {}, {}
    try:
        names = sorted(os.listdir(out_root))
    except OSError:
        return {"elapsed": el, "overhead": ov}
    for name in names:
        d = os.path.join(out_root, name)
        if name.endswith("-SMOKE") or d == skip_dir or not os.path.isfile(os.path.join(d, "session.log")):
            continue
        starts = {}
        try:
            with open(os.path.join(d, "session.log"), errors="replace") as f:
                for line in f:
                    m = _RUN_START.match(line)
                    if m:
                        h, mi, se, idx, tool, var = m.groups()
                        starts[int(idx)] = (int(h) * 3600 + int(mi) * 60 + int(se), tool, var)
                        continue
                    m = _RUN_END.match(line)
                    if m and int(m.group(4)) in starts:
                        h, mi, se, idx, mname, secs = m.groups()
                        t0, tool, var = starts[int(idx)]
                        wall = (int(h) * 3600 + int(mi) * 60 + int(se) - t0) % 86400
                        tag = (re.search(r"_n(\d+)$", mname) or [None, ""])[1] if tool == "mandelbrot" else ""
                        el.setdefault((tool, var, tag), []).append(float(secs))
                        ov.setdefault(tool, []).append(max(0.0, wall - float(secs)))
        except OSError:
            continue
    return {"elapsed": el, "overhead": ov}


def est_nominal_s(a, tool, variant):
    """Seconds for one run including its sync and thermal gate. From this board's own earlier sessions where they exist
    (median), else a rough guess."""
    n2 = (a.mandelbrot_n / 4000.0) ** 2
    hov, hel = HISTORY["overhead"], HISTORY["elapsed"]
    ov = _median(hov[tool]) if hov.get(tool) else 30
    if tool == "stress-ng":
        return a.stressng_secs + ov
    if tool == "mandelbrot":
        h = hel.get((tool, variant, str(a.mandelbrot_n)))
        return (_median(h) if h else (40 if variant == "1" else 15) * n2) + ov
    if tool == "openssl-speed":
        # timed phases per run: AES 1, RSA-2048 4 (sign, verify, encrypt, decrypt), ML-KEM 3 (keygen, encaps, decaps);
        # history from sessions run with a shorter --speed-secs would under-estimate, so never go below phases x seconds
        h = hel.get((tool, variant, ""))
        floor = ((AES_SECS_SMOKE if a.smoke else AES_SECS) + 5) if variant in BULK_VARIANTS else {"rsa2048": 4, "mlkem768": 3}[variant] * a.speed_secs + 5
        return max(_median(h) if h else 0, floor) + ov
    if tool == "scimark2":
        h = hel.get((tool, variant, ""))
        return (_median(h) if h else 30) + ov
    if tool == "sbc-bench":
        h = hel.get((tool, variant, ""))
        return (_median(h) if h else 700) + ov
    if tool == "geekbench":
        h = hel.get((tool, variant, ""))
        return (_median(h) if h else 600) + ov
    return 60


def eta_remaining_s(a, plan, idx_done, actual):
    """Seconds left for plan[idx_done:], from measured times of the same tool/variant where we have them, else the
    nominal guess scaled by how the measured runs compare with their guesses so far."""
    done_keys = [(t, v) for (t, v, _r) in plan[:idx_done]]
    meas = sum(actual[k] for k in done_keys if k in actual)
    nom = sum(est_nominal_s(a, *k) for k in done_keys if k in actual)
    ratio = min(10.0, max(1.0, meas / nom)) if nom > 0 else 1.0     # only ever scale UP: an early estimate that is too long is better than one that is too short
    eta_remaining_s.ratio = ratio
    return sum(actual.get((t, v), est_nominal_s(a, t, v) * ratio) for (t, v, _r) in plan[idx_done:])


def clock_hhmm(secs_from_now):
    return time.strftime("%H:%M", time.localtime(time.time() + secs_from_now))


def fmt_duration(secs):
    secs = int(round(secs))
    if secs >= 3600:
        return f"{secs // 3600} h {secs % 3600 // 60:02d} min"
    return f"{max(1, round(secs / 60))} min"


def stats_table(rows):
    """rows: list of dict(tool, variant, metric_name, value, unit, hard, quals). Returns the end-of-session table lines."""
    order, groups = [], {}
    for r in rows:
        k = (r["tool"], r["variant"], r["metric_name"])
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(r)
    out = []
    for k in order:
        g = groups[k]
        kept = [r["value"] for r in g if not r["hard"] and r["value"] is not None]
        excl = sum(1 for r in g if r["hard"] or r["value"] is None)
        n = len(kept)
        mean = sum(kept) / n if n else None
        cv = None
        if n >= 2 and mean:
            sd = (sum((x - mean) ** 2 for x in kept) / (n - 1)) ** 0.5
            cv = 100.0 * sd / abs(mean)
        notes = []
        if cv is not None and cv > CV_LIMIT_PCT:
            notes.append(f"ABOVE {CV_LIMIT_PCT:g}% LIMIT")
        quals = sorted({q for r in g if not r["hard"] for q in r["quals"]})
        if k[2] == "sbcbench_aes256cbc_16k":
            notes.append("uses own OLD OpenSSL (v3.0), not 3.5.8")      # replaces the clock-pin note on this row
        else:
            notes += [FLAG_TEXT.get(q, q) for q in quals]
        if excl:
            notes.append(f"{excl} run(s) EXCLUDED (flagged)")
        out.append((metric_label(*k), n, "-" if mean is None else fmt_val(mean), "-" if cv is None else f"{cv:.2f}%",
                    "-" if not kept else f"{fmt_val(min(kept))} - {fmt_val(max(kept))}", short_unit(g[0]["unit"]), "; ".join(notes)))
    if not out:
        return []
    hdr = ("metric", "n", "mean", "CV", "min - max", "unit", "notes")
    w = [max(len(str(x[i])) for x in out + [hdr]) for i in range(7)]
    lines = [f"{hdr[0]:<{w[0]}}  {hdr[1]:>{w[1]}}  {hdr[2]:>{w[2]}}  {hdr[3]:>{w[3]}}  {hdr[4]:<{w[4]}}  {hdr[5]:<{w[5]}}  {hdr[6]}"]
    for x in out:
        lines.append(f"{x[0]:<{w[0]}}  {x[1]:>{w[1]}}  {x[2]:>{w[2]}}  {x[3]:>{w[3]}}  {x[4]:<{w[4]}}  {x[5]:<{w[5]}}  {x[6]}".rstrip())
    return lines


def screen_hides(msg):
    """Log lines that go to session.log only (the screen tells the story; --verbose shows everything)."""
    if msg.startswith(("session ", "command line:", "swap at start:", "cpu clock:", "governor:", "quiesce:", "fan:",
                       "external fan:", "environment:", "--- run", "WAITING FOR OPERATOR", "gate met:", "T_ref=",
                       "teardown:", "run ", "   ... ", "SESSION COMPLETE", "No errors.", "cpufreq was changed")):
        return True
    return msg.startswith("sync_clock:") and "ok=True" in msg


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="store_true")
    ap.add_argument("--tools", default=",".join(TOOLS_BUILT), help="comma list; built: " + ", ".join(TOOLS_BUILT))
    ap.add_argument("--variants", default=None, help="comma list of variants to run (all tools): e.g. chacha20poly1305, aes256cbc. Default: every variant except those switched off by default (openssl-speed aes256cbc)")
    ap.add_argument("--repeats", type=int, default=None, help="default 5 (smoke: 1)")
    ap.add_argument("--stressng-secs", type=int, default=None, help="default 60 (smoke: 20)")
    ap.add_argument("--stressng-method", default="all", help="'all' = stress-ng's default mix (as the 2026-09 runs), or a --cpu-method name e.g. matrixprod")
    ap.add_argument("--stress-ng", default=None, help="path to stress-ng")
    ap.add_argument("--mandelbrot-n", type=int, default=None, help="default 4000 (smoke: 500), the same on every board so the output md5 proves identical work. Work grows with n squared. 1 worker at n=4000: DUT-RISCV 232 s (measured 2026-10-12), DUT-Pi5 about 42 s (n=8000 took 167 s). n is part of the metric name")
    ap.add_argument("--mandelbrot-timeout", type=int, default=1800)
    ap.add_argument("--scimark2", default=None, help="path to the scimark2 binary (default: the Phoronix-built one, else ~/bench/scimark2/scimark2)")
    ap.add_argument("--geekbench-timeout", type=int, default=10800, help="seconds before a Geekbench run is killed (default 10800 = 3 h). One run took about 13 min on DUT-Pi5 but over 47 min on DUT-RISCV (single-core part alone about 35 min)")
    ap.add_argument("--geekbench-repeats", type=int, default=None, help="default 3 (smoke: 1). Geekbench is ~13 min a run and uploads every result; scores moved ~10%% between sessions on DUT-Pi5, so one run says nothing about its own spread")
    ap.add_argument("--sbcbench-repeats", type=int, default=None, help="default 3 (smoke: 1). sbc-bench takes ~8 min and steps the CPU through its own frequency list: run it this many times (its memcpy and 7-zip compression moved 8-9%% between two sessions on DUT-Pi5, so one run is not enough)")
    ap.add_argument("--sbc-bench", default=None, help="path to sbc-bench.sh (default ~/bench/sbc-bench.sh)")
    ap.add_argument("--geekbench", default=None, help="path to geekbench7 (default: newest ~/bench/Geekbench-*/geekbench7)")
    ap.add_argument("--openssl", default=None, help="path to openssl (default: first on PATH)")
    ap.add_argument("--speed-secs", type=int, default=None, help="seconds per `openssl speed` phase for RSA-2048 and ML-KEM-768; default 10 (smoke: 5). RSA runs 4 timed phases and ML-KEM 3, so those runs last 4 and 3 times this. AES-256 is fixed at 60 s (smoke 10 s)")
    ap.add_argument("--smoke", action="store_true", help="bare-minimum counts to prove the whole flow works; the session is named -SMOKE and every CSV row has smoke=True. Explicit options still override")
    ap.add_argument("--phases", default="both", choices=("both", "offline", "online"), help="offline = measured tools on the testbed network only; online = tools that need the internet; both = one session with an operator pause between")
    ap.add_argument("--internet-always", action="store_true", help="this board has no separate internet cable (DUT-Mac on Wi-Fi): the internet is always reachable, so skip the cable prompts and the internet-absent check; JUMP is still required")
    ap.add_argument("--geekbench-swap-mb", type=int, default=None, help="default: 4096 automatically on the RISC-V board only, 0 (none) everywhere else; give 0 to force it off. Switch ON a swap file of this many MB just before each Geekbench run and OFF (and delete it) straight after; 4096 is what worked. Geekbench's multi-core phase needs about 5.3 GB, so without swap it is killed by the kernel (measured 2026-10-12: peak swap use 1493 MB). Swap use during any other run is flagged swap_used (growth above %d MB) and excluded; the swap total and peak use are recorded in every row" % SWAP_GROWTH_FLAG_MB)
    ap.add_argument("--no-prompts", action="store_true", help="debug/unattended: no operator pauses and no network checks")
    ap.add_argument("--out", default=None, help="data folder (default: <invoking user's home>/E1)")
    ap.add_argument("--no-sync", action="store_true", help="debug only: skip sync_clock() (every run then has sync_ok=false)")
    ap.add_argument("--no-gate", action="store_true", help="debug only: skip the thermal gate")
    ap.add_argument("--plan", action="store_true", help="print the run plan and exit")
    ap.add_argument("--verbose", action="store_true", help="show the per-second thermal gate and T_ref lines on screen (they are always written to session.log)")
    ap.add_argument("--fan-confirmed", action="store_true")
    ap.add_argument("--cpu-clock", default="default", help="library option: default | turbo | noturbo | <MHz>")
    ap.add_argument("--ntp-server", default="192.168.55.100")
    ap.add_argument("--sysroot", default="/", help="testing only")
    a = ap.parse_args()
    if a.geekbench_swap_mb is None:                     # DUT-RISCV only (3.7 GB): Geekbench is killed without swap
        a.geekbench_swap_mb = 4096 if platform.machine().lower().startswith("riscv") else 0
    for k, dflt in (SMOKE if a.smoke else STANDARD).items():
        if getattr(a, k) is None:
            setattr(a, k, dflt)
    if a.version:
        print("experimenter_e1.py", E1_VERSION, "| platform_env", platform_env.PLATFORM_ENV_VERSION, "| harness", harness.HARNESS_VERSION)
        return 0

    tools = [t for t in a.tools.split(",") if t]
    for t in tools:
        if t in TOOLS_PLANNED:
            ap.error(f"tool '{t}' is planned but not built in this version")
        if t not in TOOLS:
            ap.error(f"unknown tool '{t}' (built: {', '.join(TOOLS_BUILT)})")
    a.variants = [v for v in (a.variants or "").split(",") if v]
    known_variants = {v for t in TOOLS for v in TOOLS[t][1]}
    for v in a.variants:
        if v not in known_variants:
            ap.error(f"unknown variant '{v}' (known: {', '.join(sorted(known_variants))})")
    plan = []
    for rep in range(1, a.repeats + 1):
        for t in tools:
            if (t == "geekbench" and rep > a.geekbench_repeats) or (t == "sbc-bench" and rep > a.sbcbench_repeats):
                continue                                    # long, and Geekbench uploads every result
            for v in [x for x in TOOLS[t][1] if (x in a.variants if a.variants else (t, x) not in OFF_BY_DEFAULT)]:
                plan.append((t, v, rep))
    # stable: ordinary measured tools first, then sbc-bench (it steps the CPU through its own frequencies), then online tools
    plan.sort(key=lambda r: 2 if r[0] in ONLINE_TOOLS else (1 if r[0] == "sbc-bench" else 0))
    if a.phases == "offline":
        plan = [r for r in plan if r[0] not in ONLINE_TOOLS]
    elif a.phases == "online":
        plan = [r for r in plan if r[0] in ONLINE_TOOLS]
    if not plan:
        ap.error(f"nothing to run for --phases {a.phases} with --tools {a.tools}")
    if a.plan:
        print("experimenter_e1.py", E1_VERSION, "| platform_env", platform_env.PLATFORM_ENV_VERSION, "| harness", harness.HARNESS_VERSION)
        print(f"plan: {len(plan)} runs (repeat-major): " + ", ".join(f"{t}/{v}#{r}" for t, v, r in plan[:12]) + (" ..." if len(plan) > 12 else ""))
        return 0
    print(f"E1 baseline  {socket.gethostname()}{'  SMOKE' if a.smoke else ''}   experimenter_e1 {E1_VERSION} | platform_env {platform_env.PLATFORM_ENV_VERSION} | harness {harness.HARNESS_VERSION}", flush=True)

    harness.ignore_sighup()
    out_root = a.out or os.path.join(harness.invoking_user_home(), "E1")
    session_id = time.strftime("%Y%m%d-%H%M%S") + "-" + socket.gethostname() + ("-SMOKE" if a.smoke else "")
    sdir = os.path.join(out_root, session_id)
    os.makedirs(sdir, exist_ok=True)
    log = harness.SessionLog(os.path.join(sdir, "session.log"), verbose=a.verbose)
    a.log = log.log                                       # runners that change the system (swap) log through the session log
    log.hide = screen_hides
    t_session0 = time.time()
    n_runs = len(plan)
    reps_of = {}
    for (t, v, _r) in plan:
        reps_of[(t, v)] = reps_of.get((t, v), 0) + 1

    def run_label(i, t, v, r):
        k = reps_of[(t, v)]
        return f"[{i}/{n_runs}] {tool_label(t, v)}" + (f" ({r}/{k})" if k > 1 else "")
    lw = max(len(run_label(n_runs, t, v, r)) for (t, v, r) in plan)
    cur = {"label": "[setup]", "idx": 0, "t0": time.time()}
    actual, result_rows = {}, []

    def eta_text():
        left = eta_remaining_s(a, plan, max(cur["idx"] - 1, 0), actual) - (time.time() - cur["t0"] if cur["idx"] else 0)
        left += PAUSE_ALLOWANCE_S * max(n_runs - max(cur["idx"] - 1, 0), 0) / n_runs
        fin = clock_hhmm(max(left, 0))
        if not cur["idx"]:
            return f"ETA {fin}"
        k = (plan[cur["idx"] - 1][0], plan[cur["idx"] - 1][1])
        est = actual.get(k, est_nominal_s(a, *k) * getattr(eta_remaining_s, "ratio", 1.0))
        this = clock_hhmm(max(est - (time.time() - cur["t0"]), 0))
        return f"ETAs {this}/{fin}"          # this test / whole session

    def live(msg):
        log.progress(f"{cur['label']:<{lw}}  {msg:<44}  {eta_text()}")

    def render_quiet(msg):
        m = re.match(r"gate: ([\d.]+) C \(raw [\d.]+\) -> ([\d.]+) C", msg)
        if m:
            return f"{cur['label']:<{lw}}  {'gate: cooling ' + format(float(m.group(1)), '.1f') + ' -> ' + format(float(m.group(2)), '.1f') + ' C':<44}  {eta_text()}"
        m = re.match(r"gate: warming up ([\d.]+) C \(raw [\d.]+\) -> ([\d.]+) C, (\d+) s", msg)
        if m:
            return f"{cur['label']:<{lw}}  {'gate: warming up ' + format(float(m.group(1)), '.1f') + ' -> ' + format(float(m.group(2)), '.1f') + ' C':<44}  {eta_text()}"
        m = re.match(r"T_ref wait: ([\d.]+) C; window (\d+)/(\d+).*?([\d.]+) s elapsed", msg)
        if m:
            return (f"setting up: waiting for the temperature to settle   {float(m.group(1)):.1f} C   (window {m.group(2)}/{m.group(3)}, "
                    f"{float(m.group(4)):.0f} s elapsed)")
        return None

    log.render_quiet = render_quiet
    HISTORY.update(load_history(out_root, skip_dir=sdir))
    n_hist = sum(len(v) for v in HISTORY["elapsed"].values())
    total_est = eta_remaining_s(a, plan, 0, actual) + SETUP_S
    log.screen(f"Plan         : {n_runs} runs ({', '.join(tools)}), about {fmt_duration(total_est)}, ETA {clock_hhmm(total_est + PAUSE_ALLOWANCE_S)}")
    status = harness.StatusFile(os.path.join(out_root, "STATUS"), len(plan), log.path)
    status.running(0, 0)
    log.log(f"session {session_id}; experimenter_e1 {E1_VERSION}; platform_env {platform_env.PLATFORM_ENV_VERSION}; harness {harness.HARNESS_VERSION}")
    log.log("command line: " + " ".join(sys.argv))
    sw0 = read_swap_mb()
    log.log("swap at start: " + ("not readable" if sw0 is None else f"total {sw0[0]:.0f} MB, used {sw0[1]:.0f} MB") +
            (f"; swap file of {a.geekbench_swap_mb} MB will be switched on around each Geekbench run only" if a.geekbench_swap_mb else ""))

    env = platform_env.PlatformEnvironment(sysroot=a.sysroot, log=log.log, fan_confirmed=a.fan_confirmed,
                                           cpu_clock=a.cpu_clock, ntp_server=a.ntp_server)
    tool_versions = {
        "python": platform.python_version(), "hostname": socket.gethostname(), "kernel": platform.release(),
        "stress-ng": cmd_output([a.stress_ng or harness.find_tool("stress-ng") or "stress-ng", "--version"]) if "stress-ng" in tools else None,
        "openssl_path": openssl_path(a) if "openssl-speed" in tools else None,
        "openssl": (cmd_output([openssl_path(a), "version", "-a"], 15, env=harness.openssl_runtime_env(openssl_path(a)))
                    if "openssl-speed" in tools and openssl_path(a) else None),
        "scimark2_path": scimark2_path(a) if "scimark2" in tools else None,
        "geekbench_path": geekbench_path(a) if "geekbench" in tools else None,
        "sbc_bench_path": sbcbench_path(a) if "sbc-bench" in tools else None,
        "uname": " ".join(platform.uname()),
    }
    jf = open(os.path.join(sdir, "results.jsonl"), "a", buffering=1)
    cf = open(os.path.join(sdir, "results.csv"), "w", newline="")
    cw = csv.DictWriter(cf, fieldnames=CSV_COLS)
    cw.writeheader()
    done, fails_in_row, abort_reason, gate_miss_in_row = 0, 0, None, 0
    net_state = {"internet": None}           # what the operator was last asked to set up

    def wait_for(msg, check, ok_msg=None):
        if a.no_prompts:
            log.log(f"(prompts off) {msg}")
            return

        def waiting(m):
            status.waiting(done, len(log.errors), m)

        log.clear_inline()
        r = harness.operator_wait(msg, out_root, log.log, on_wait=waiting, check=check, short=True, ok_msg=ok_msg,
                                  say=lambda t: log.screen(t, file=True), override_after=3)
        status.running(done, len(log.errors))            # the answer was given: STATUS no longer says WAITING
        return r

    def check_measured():
        if not harness.host_reachable(a.ntp_server):
            return False, f"the testbed network is not reachable (no reply from {a.ntp_server})"
        if not a.internet_always and harness.internet_reachable():
            return False, "the internet IS reachable; the measured tools must run with the internet cable disconnected"
        return True, ""

    def check_online():
        if not harness.host_reachable(a.ntp_server):
            return False, f"the testbed network is not reachable (no reply from {a.ntp_server}); keep its cable connected"
        if not harness.internet_reachable():
            return False, "the internet is not reachable yet"
        return True, ""

    def want_internet(flag):
        """Ask the operator to connect/disconnect the internet cable when the phase changes (no-op if already right)."""
        if a.internet_always:                              # Mac: permanently online, no cable to move; only JUMP must answer
            if net_state["internet"] is None:
                net_state["internet"] = flag
                ok, why = check_measured()
                if not ok:
                    wait_for("Testbed network problem: " + why + ". Fix it", check_measured, ok_msg="  check: testbed reachable")
            return
        if net_state["internet"] is flag:
            return
        net_state["internet"] = flag
        if flag:
            net_state["override"] = False
            wait_for("CONNECT the INTERNET cable / USB adapter now", check_online, ok_msg="  check: internet reachable")
        else:
            net_state["override"] = bool(wait_for("Testbed cable connected and INTERNET cable / USB adapter DISCONNECTED?", check_measured,
                                                  ok_msg="  check: testbed reachable, internet not reachable"))

    try:
        want_internet(plan[0][0] in ONLINE_TOOLS)         # BEFORE setup(): nothing has been changed yet
        env.setup()
        env_controls = env.describe()
        cc = env_controls.get("cpu_clock") or {}
        clock_txt = (f"clock fixed at {cc['target_khz'] / 1000:.0f} MHz" if cc.get("mode") == "fixed" and cc.get("target_khz")
                     else "clock left at the board's default")
        gov_txt = "governor pinned to performance" if getattr(env, "_snap", {}).get("governor") else "governor not controllable"
        log.screen(f"Environment  : {clock_txt}; {gov_txt}")
        tr, tt = env_controls.get("t_ref_c"), getattr(env, "t_target", None)
        log.screen("Reference    : " + (f"T_ref {tr:.1f} C" if tr is not None else "T_ref n/a") + (f", thermal gate target {tt:.1f} C" if tt is not None else ", no thermal gate")
                   + ("" if env_controls.get("t_ref_converged", True) else "  (T_REF DID NOT CONVERGE)") + "; external fan confirmed")
        log.log("environment: " + json.dumps({k: env_controls[k] for k in ("kind", "model", "cpu_clock", "t_ref_c", "t_ref_converged", "fan_mode")}))
        for idx, (tool, variant, rep) in enumerate(plan, 1):
            want_internet(tool in ONLINE_TOOLS)
            cur.update(label=run_label(idx, tool, variant, rep), idx=idx, t0=time.time())
            live("starting")
            run_id = f"{session_id}-{idx:03d}-{tool}-{variant}"
            runner = TOOLS[tool][0]
            secs = a.stressng_secs
            log.log(f"--- run {idx}/{len(plan)}: {tool} variant={variant} repeat={rep} ---")
            rec = {"session_id": session_id, "run_id": run_id, "tool": tool, "variant": variant, "repeat": rep,
                   "e1_version": E1_VERSION, "lib_version": platform_env.PLATFORM_ENV_VERSION, "flags": ["smoke"] if a.smoke else []}
            if net_state.get("override") and tool not in ONLINE_TOOLS:
                rec["flags"].append("internet_left_connected")      # operator carried on with the internet cable in
            live("syncing the clock")
            rec["sync"] = None if a.no_sync else env.sync_clock()
            if rec["sync"] is not None and not rec["sync"]["ok"]:
                rec["flags"].append("sync_not_ok")
            if a.no_sync:
                rec["flags"].append("sync_skipped")
            if a.no_gate or env.t_target is None:
                rec["gate"] = {"met": None, "note": "skipped" if a.no_gate else "no temperature sensor"}
                rec["flags"].append("gate_skipped")
            else:
                live("temperature gate: checking")
                rec["gate"] = env.prepare_iteration()
                if not rec["gate"].get("met"):
                    rec["flags"].append("gate_not_met")
            thr0 = env.throttle_state()
            sampler = Sampler(env, log=log.log, label=f"{tool}/{variant}", progress=live)
            sampler.start()
            wall0 = time.time()
            try:
                res = runner(a, variant, secs)
            except Exception as e:                      # a runner bug must not end the session
                res = {"rc": 1, "status": f"runner exception: {e!r}", "flags": ["runner_exception"], "detail": {}}
            rec["wall_start_unix"] = wall0
            rec["sampler"] = sampler.finish()
            if "cpufreq_changed_by_tool" in (res.get("flags") or []):
                # sbc-bench moves the CPU clock itself. Put the governor/clock pins back NOW so the next run is pinned
                # again (DUT-PC smoke: the Geekbench run after sbc-bench ran at 800-3600 MHz, unflagged).
                perrs = env.reassert_pins()
                log.log("cpufreq was changed by the tool: governor and CPU clock pins re-applied"
                        + (f"; ERRORS: {perrs}" if perrs else "; read back OK"))
                if perrs:
                    res.setdefault("flags", []).append("pin_not_restored")
                    log.error(f"{run_id}: pins not restored after the tool: {perrs}")
            gr = rec["sampler"].get("swap_growth_mb")
            if gr is not None and gr > SWAP_GROWTH_FLAG_MB and not (tool == "geekbench" and a.geekbench_swap_mb):
                rec["flags"].append("swap_used")
            thr1 = env.throttle_state(since=thr0)
            rec["throttle"] = thr1
            if thr1.get("throttled"):
                rec["flags"].append("throttled")
            rec.update({k: res.get(k) for k in ("rc", "status", "metric_name", "metric_value", "metric_unit", "elapsed_s", "cpu_time_s")})
            rec["flags"] += res.get("flags", [])
            rec["detail"] = res.get("detail", {})
            ok = res.get("rc") == 0
            if ok:
                fails_in_row = 0
            else:
                fails_in_row += 1
                why = (res.get("detail", {}).get("output_tail") or res.get("detail", {}).get("stderr_tail") or "").strip().splitlines()
                log.error(f"{run_id}: {res.get('status')}" + (f" | last output: {why[-1][:160]}" if why else ""))
            with open(os.path.join(sdir, f"run_{idx:03d}.json"), "w") as f:
                json.dump(dict(rec, trace=sampler.trace), f, indent=1, default=str)
            jf.write(json.dumps(rec, default=str) + "\n")
            sm = rec["sampler"]
            cw.writerow({"session_id": session_id, "run_id": run_id, "tool": tool, "variant": variant, "repeat": rep,
                         "status": rec["status"], "metric_name": rec.get("metric_name"), "metric_value": rec.get("metric_value"),
                         "metric_unit": rec.get("metric_unit"), "elapsed_s": None if rec.get("elapsed_s") is None else round(rec["elapsed_s"], 3),
                         "cpu_time_s": None if rec.get("cpu_time_s") is None else round(rec["cpu_time_s"], 2),
                         "temp_start_c": sm["temp_start_c"], "temp_end_c": sm["temp_end_c"], "temp_max_c": sm["temp_max_c"],
                         "cpu_khz_min": sm["cpu_khz_min"], "cpu_khz_max": sm["cpu_khz_max"], "cpu_khz_mean": sm["cpu_khz_mean"],
                         "swap_total_mb": sm.get("swap_total_mb"), "swap_used_max_mb": sm.get("swap_used_max_mb"),
                         "throttled": thr1.get("throttled"), "gate_met": rec["gate"].get("met"),
                         "sync_ok": None if rec["sync"] is None else rec["sync"]["ok"],
                         "sync_error_ms": None if rec["sync"] is None else rec["sync"].get("dut_clock_error_ms"),
                         "smoke": bool(a.smoke), "flags": ";".join(rec["flags"])})
            for (mn, mv, mu) in (res.get("extra_metrics") or []):
                cw.writerow({"session_id": session_id, "run_id": run_id, "tool": tool, "variant": variant, "repeat": rep,
                             "status": rec["status"], "metric_name": mn, "metric_value": mv, "metric_unit": mu,
                             "smoke": bool(a.smoke), "flags": ";".join(rec["flags"])})
            cf.flush()
            done = idx
            log.log(f"run {idx}: {rec['status']}; {rec.get('metric_name')}={rec.get('metric_value')}; elapsed {rec.get('elapsed_s') and round(rec['elapsed_s'], 1)} s; flags: {rec['flags'] or 'none'}")
            actual[(tool, variant)] = time.time() - cur["t0"]
            hard = (not ok) or any(f != "smoke" and f not in QUALIFIER_FLAGS for f in rec["flags"])
            quals = [f for f in rec["flags"] if f in QUALIFIER_FLAGS]
            for (mn, mv, mu) in [(rec.get("metric_name"), rec.get("metric_value"), rec.get("metric_unit"))] + list(res.get("extra_metrics") or []):
                if mn:
                    result_rows.append({"tool": tool, "variant": variant, "metric_name": mn, "value": mv, "unit": mu, "hard": hard, "quals": quals})
            mv0 = rec.get("metric_value")
            state = "ok" if ok else "FAILED: " + str(res.get("status"))
            fw = flag_words(rec["flags"])
            SHOW_EXTRA = {"geekbench_single_score": "single", "sbcbench_7zip_single_mips": "single"}
            extra_txt = "".join(f"   {SHOW_EXTRA[mn]} {int(mv) if float(mv).is_integer() else fmt_val(mv)}"       # second-tier results beside the headline
                                for (mn, mv, mu) in (res.get("extra_metrics") or []) if mn in SHOW_EXTRA and mv is not None)
            log.screen(f"{cur['label']:<{lw}}  {fmt_val(mv0) if mv0 is not None else '-':>10} {short_unit(rec.get('metric_unit')):<14} {state:<4}  {time.strftime('%H:%M:%S')}" + extra_txt + (f"   {fw}" if fw else ""))
            status.running(done, len(log.errors))
            gate_miss_in_row = gate_miss_in_row + 1 if "gate_not_met" in rec["flags"] else 0
            if fails_in_row >= 3:
                abort_reason = "3 failed runs in a row"
                break
            if gate_miss_in_row >= 2:
                abort_reason = ("thermal gate not met in 2 runs in a row: the board did not reach the gate target in time. "
                                "Usual causes: the external fan is closer or stronger than when T_ref was measured "
                                "(the warm-up burn levels off below its goal), the board was disturbed, or T_ref was taken "
                                "while it was still warming up. Check the fan position and the gate lines in session.log, "
                                "let it idle for 10-15 minutes and start again")
                break
        if not abort_reason and net_state["internet"]:
            wait_for("DISCONNECT the INTERNET cable / USB adapter now (testbed cable stays)", check_measured,
                     ok_msg="  check: testbed reachable, internet not reachable")
        with open(os.path.join(sdir, "session.json"), "w") as f:
            json.dump({"session_id": session_id, "e1_version": E1_VERSION, "lib_version": platform_env.PLATFORM_ENV_VERSION,
                       "harness_version": harness.HARNESS_VERSION, "tool_versions": tool_versions,
                       "swap_at_start_mb": sw0, "geekbench_swap_mb": a.geekbench_swap_mb or None,
                       "env_controls": env_controls, "args": {k: v for k, v in vars(a).items() if k != "log"}, "plan": plan}, f, indent=1, default=str)
    except platform_env.EnvError as e:
        abort_reason = f"environment error: {e}"
        log.error(abort_reason)
    except KeyboardInterrupt:
        abort_reason = "interrupted"
    except SystemExit:
        abort_reason = "terminated"
        raise
    finally:
        if a.geekbench_swap_mb:
            swap_disable(log.log)                       # never leave our swap file behind, whatever ended the session
        try:
            env.teardown()
        finally:
            log.clear_inline()
            log.summary()
            tbl = stats_table(result_rows)
            if tbl:
                log.screen("", file=True)
                for ln in tbl:
                    log.screen(ln, file=True)
                log.screen("", file=True)
            if abort_reason:
                log.log("SESSION ABORTED: " + abort_reason)
                status.aborted(done, abort_reason)
            else:
                log.log(f"SESSION COMPLETE: {done}/{len(plan)} runs, {len(log.errors)} error(s)")
                status.completed(done, len(log.errors))
                log.screen(f"SESSION COMPLETE  {done}/{len(plan)} runs  {len(log.errors)} error(s)  {fmt_duration(time.time() - t_session0)}   log: {log.path}")
            jf.close()
            cf.close()
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
