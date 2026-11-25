# SOC11101: Cross-Platform Energy Analysis of Post-Quantum Cryptography Key Generation

Supporting material for the MSc Cybersecurity dissertation *Cross-Platform Energy Analysis of Post-Quantum Cryptography Key Generation* (SOC11101), Edinburgh Napier University, 2026. Author: James Cameron Patterson. Supervisor: Prof. Bill Buchanan.

The dissertation measures the energy used to generate keys for post-quantum algorithms (ML-KEM, ML-DSA) and classical ones (X25519, NIST P-curves, secp160r1) with OpenSSL 3.5, on five platforms: Raspberry Pi 5, Raspberry Pi Compute Module 5, a RISC-V board, a PC and a Mac mini. Energy is read from an external USB power meter. Each measured run is paired with a `NULL` run of the same length that generates nothing, so the energy of key generation is the `LIVE` run minus the `NULL` run.

This repository holds the measurement software, the logs of every session and the measured results. It is meant to show how the data was produced, so the experiments can be repeated. It's not a packaged tool (that's for another day!).

## Contents

| Folder | What is in it |
|---|---|
| [`software/`](software/) | The Collector, the Experimenter scripts and the shared code used to run and record the experiments |
| [`platform_build/`](platform_build/) | How each platform was built and set up (OpenSSL 3.5 build, network lock-down, benchmark tools) |
| [`logs/`](logs/) | The screen log of every session used in the dissertation, one file per experiment (E1, E2, E2b, E2c), unchanged apart from layout |
| [`results/`](results/) | The measured results: one row per run for E2, E2b and E2c, and a note on how E1 was recorded |

Each folder has its own `README.md`.

## The experiments

| Experiment | What it measures |
|---|---|
| E1 | Platform capability baseline: standard compute, memory and OpenSSL speed benchmarks |
| E2 | Energy of key generation, one `openssl genpkey` process per key |
| E2b | Energy of key generation inside one process, through the OpenSSL library |
| E2c | As E2b, with the processor clock pinned at each step of a sweep |

## How a measurement works

1. The **Experimenter** runs on the device under test (DUT). It sets a stable environment (fixed clock where possible, fan at full speed, clock synchronisation), then generates the required number of keys.
2. The **Collector**, a separate Raspberry Pi 5 with a TC66C USB power meter, records the meter readings against start and stop messages sent by the Experimenter.
3. Each `LIVE` run is followed or preceded by a `NULL` run of the same length, and the Collector writes one row per run to `AllResults.csv`. The files in `results/` are that table, reduced to the sessions used in the analysis.

The testbed is isolated: the devices have no internet access during runs (except for a couple in E1 and are disconnected when not these), and the Collector and time server (GPS fed) are fixed on a private network.

## Using this repository

- To see what was measured, start with `results/README.md` and the CSV files.
- To check a session, find it in `logs/`, each block is headed by its session ID and the `Experiment_ID` column in the CSVs starts with the same id.
- To repeat an experiment, build a platform following `platform_build/BUILD.md`, run `collector.py` on the Collector, then run the Experimenter on the device under test (see `software/README.md`).

## Not included

The per-run raw files from the Collector (several thousand small files), the raw analysis that produces the dissertation's tables and figures, and sessions that were not used in the analysis (smoke tests, calibration runs and superseded sessions).

## Licence and credit

Code and data written for the dissertation are published for academic reference. `software/mandelbrot_w.py` is the Benchmarks Game Python Mandelbrot program, contributed by Joerg Baumann, with a one-line local change described in its header. The TC66C meter is read with the approach of the open-source TC66 library by TheHWcave.
