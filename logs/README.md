# Results

This folder holds the measured results behind the dissertation *Cross-Platform Energy Analysis of Post-Quantum Cryptography Key Generation* (SOC11101), in a small number of files. The full per-run raw files from the Collector are not published; the screen logs in `../logs/` record every session, and the experiments can be rerun with the software in `../software/`.

| File | Contents |
|---|---|
| `E2_runs.csv` | One row per measured run, E2 (key generation by one `openssl genpkey` process per key) |
| `E2b_runs.csv` | One row per measured run, E2b (key generation inside one process, through the OpenSSL library) |
| `E2c_runs.csv` | One row per measured run, E2c (as E2b, with the processor clock pinned at each step of a sweep) |
| `example_raw/` | One `LIVE` and `NULL` pair of raw per-run files, for illustration only (see below) |

Only sessions used in the analysis are included. Smoke tests, calibration runs and superseded sessions are not.

## How the data was captured

- The device under test (DUT) runs the **Experimenter**, which generates the keys and sends run start and stop markers.
- The **Collector** (a separate Raspberry Pi 5) reads the USB power meter (a TC66C) and records the meter readings against those markers, one file per run.
- Each run's energy is the meter's energy reading over the run. The meter counts in 3.6 J steps.
- The per-run files are gathered into one `AllResults.csv` per experiment. The files here are that table, reduced to the sessions used in the analysis.
- The experiments pair runs: a `LIVE` run (keys generated) and a `NULL` run of the same length with nothing generated. The energy attributable to key generation is `LIVE` minus `NULL`.

## Fields (`E2_runs.csv`, `E2b_runs.csv`, `E2c_runs.csv`)

| Field | Meaning |
|---|---|
| `Exp` | Experiment number |
| `#` | Pair number within the session |
| `Platform` | Device under test: `DUT-Pi5`, `DUT-CM5`, `DUT-RISCV`, `DUT-PC` or `DUT-Mac` |
| `Algorithm` | Key type, for example `P-256`, `ML-KEM-768`, `RSA-2048`; in E2c the pinned clock is part of the label (`ML-DSA-65@1600`, in MHz) |
| `Role` | `LIVE` or `NULL` |
| `Keys` | Keys generated in the run (the `NULL` run of a pair records the same count) |
| `Joules` | Energy used over the run (J) |
| `Seconds` | Run duration (s) |
| `Status` | `ok`, or the reason a run was flagged |
| `Flags` | Warnings raised during the run (empty if none) |
| `File` | Name of the per-run raw file the row came from |

Worked example (from a smoke test of E2 on the Mac mini): a `LIVE` run of 2,804 P-256 keys used 126.0 J in 10.165 s; its `NULL` pair used 28.8 J in 10.166 s. Key generation therefore cost 126.0 - 28.8 = 97.2 J, about 0.035 J per key.

The dissertation's tables (energy per key, per 1,000 keys and per million keys, with 95 % intervals) are computed from these rows by analysis scripts that are not published.

## E1 (platform capability baseline)

E1 runs standard benchmarks (compute, memory and OpenSSL speed tests) and does not use the paired energy method, so there is no `E1_runs.csv`. The results for each session were captured from the end-of-session summary tables, which are reproduced unchanged in `../logs/E1.txt`. Each E1 block there gives the session, whether it was used in the analysis, and the measured values per benchmark.

## `example_raw/`

`example_raw/` contains one `LIVE` and one `NULL` raw file from a single pair (the file names are given in the `File` column of the run tables). They show the format of the raw per-run data, which is described in the top-level README. They are included as an illustration only.
