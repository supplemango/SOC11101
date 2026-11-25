# Results

This folder holds the measured results behind the dissertation *Cross-Platform Energy Analysis of Post-Quantum Cryptography Key Generation* (SOC11101), in a small number of files. The per-run raw files from the Collector are not published (each row's `Experiment_ID` is the name of its raw file); the screen logs in `../logs/` record every session, and the experiments can be rerun with the software in `../software/`.

| File | Contents |
|---|---|
| `E2_runs.csv` | One row per measured run, E2 (key generation by one `openssl genpkey` process per key) |
| `E2b_runs.csv` | One row per measured run, E2b (key generation inside one process, through the OpenSSL library) |
| `E2c_runs.csv` | One row per measured run, E2c (as E2b, with the processor clock pinned at each step of a sweep) |

Only sessions used in the analysis are included. Smoke tests, calibration runs and superseded sessions are not.

## How the data was captured

- The device under test (DUT) runs the **Experimenter**, which generates the keys and sends run start and stop markers.
- The **Collector** (a separate Raspberry Pi 5) reads the USB power meter (a TC66C) and records the meter readings against those markers, one file per run.
- Each run's energy is the meter's energy reading over the run. The meter counts in 3.6 J steps.
- The per-run files are gathered into one `AllResults.csv` per experiment. The files here are that table, reduced to the sessions used in the analysis.
- The experiments pair runs: a `LIVE` run (keys generated) and a `NULL` run of the same length with nothing generated. The energy attributable to key generation is `LIVE` minus `NULL`.

## Fields (`E2_runs.csv`, `E2b_runs.csv`, `E2c_runs.csv`)

One row per run, with the Collector's column names. Each `LIVE` row has a `NULL` partner with the same `pair_id_EX` (inside `Extra`) and the same iteration count.

| Field | Meaning |
|---|---|
| `Timestamp` | When the run finished (local time, with UTC offset) |
| `Platform` | Device under test: `DUT-Pi5`, `DUT-CM5`, `DUT-RISCV`, `DUT-PC` or `DUT-Mac` |
| `Experiment`, `SubExp` | Experiment number (2) and sub-experiment letter where there is one (`b`, `c`) |
| `Iterations` | Keys generated (the `NULL` run of a pair uses the same count) |
| `Algorithm` | Key type, for example `P-256`, `ML-KEM-768`, `ML-DSA-65`, `X25519`, `secp160r1` |
| `Role` | `LIVE` (keys generated) or `NULL` (same loop, nothing generated) |
| `Experiment_ID` | Session id, experiment, pair number, role, algorithm and iteration count; it is also the name of the per-run raw file the row came from |
| `TotalJoules` | Energy used over the run (J), from the USB power meter |
| `DurationSeconds` | Run duration (s) |
| `IterP50Sec`, `IterP95Sec`, `IterMaxSec`, `IterCV` | Spread of the per-iteration time: median, 95th percentile and maximum (s), and the coefficient of variation |
| `NullSlipSec` | Recorded on `NULL` rows only: timing difference against the paired `LIVE` run (s) |
| `StartTempC`, `StopTempC` | Processor temperature at the start and end of the run (deg C) |
| `Throttled` | 1 if the platform reported thermal or power throttling during the run, otherwise 0 |
| `TargetMHz`, `MeasuredMHz` | Clock set and clock measured; filled in E2c (clock sweep) and blank in E2 and E2b |
| `Status`, `Flags` | `ok`, or the reason the run was flagged, and any warnings raised during it |
| `S1` to `S9` | Unused spare columns (blank) |
| `Extra` | JSON with the run's settings: `pair_id_EX`, `n_target_EX`, `sample_period_EX`, `cpu_clock_EX`, start and end clocks, temperature gate details, OpenSSL version, git commit and clock-sync state |

Worked example (E2, Mac mini, P-256, pair 0002): the `LIVE` run generated 168,248 keys using 7,707.6 J in 599.502 s. Its `NULL` partner used 1,580.4 J in 599.504 s. Key generation therefore cost 7,707.6 - 1,580.4 = 6,127.2 J, which is about 0.036 J per key.

The dissertation's tables (energy per key, per 1,000 keys and per million keys, with 95 % intervals) are computed from these rows by analysis scripts that are not published.

## E1 (platform capability baseline)

E1 runs standard benchmarks (compute, memory and OpenSSL speed tests) and does not use the paired energy method, so there is no `E1_runs.csv`. The results for each session were captured from the end-of-session summary tables, which are reproduced unchanged in `../logs/E1.txt`. Each E1 block there gives the session, whether it was used in the analysis, and the measured values per benchmark.
