# Software

The measurement software used for the experiments in the dissertation *Cross-Platform Energy Analysis of Post-Quantum Cryptography Key Generation* (SOC11101). The aim is to show how the data was collected, so the experiments can be rerun. The build steps for each platform are in `../platform_build/BUILD.md`.

| File | Role |
|---|---|
| `collector.py` | Runs on the Collector (a Raspberry Pi 5 with the TC66C USB power meter). Listens for the Experimenter's start and stop messages over UDP, records the meter against them, and writes one raw file per run plus the `AllResults.csv` table |
| `experimenter_e2.py` | Runs on the device under test for E2, E2b and E2c: generates the keys (one `openssl genpkey` process per key, or inside one process through the OpenSSL library), signals the Collector, and pairs each `LIVE` run with a `NULL` run |
| `conditions.csv`, `conditions_e2b.csv`, `conditions_e2c.csv` | The run plan read by `experimenter_e2.py` for E2, E2b and E2c respectively |
| `e2_common.py` | Helper functions used by `experimenter_e2.py` |
| `experimenter_e1.py` | Runs on the device under test for E1 (the capability baseline: stress-ng, Mandelbrot, OpenSSL speed, SciMark, Geekbench, sbc-bench) |
| `mandelbrot_w.py` | The Benchmarks Game Python Mandelbrot program (contributed by Joerg Baumann), with one local change: the number of worker processes can be set with `MANDEL_WORKERS`. Used by `experimenter_e1.py` |
| `harness.py`, `platform_env.py` | Shared code: run control, and setting up and recording a stable platform (fixed clock, fan, clock synchronisation, temperature and throttle checks) |

The dissertation refers to the Experimenter, the Collector and the shared harness. In the code the Experimenter is `experimenter_e1.py` for E1 and `experimenter_e2.py` for E2, E2b and E2c. E2b was first run with an earlier standalone script, which was merged into `experimenter_e2.py` before E2c.

Example, on a device under test with the Collector running (usage and options are in each script's `--help`):

```
python3 experimenter_e2.py --help
```

The scripts use the testbed's fixed addresses as defaults (the Collector at 192.168.55.200 and the time server at 192.168.55.100); change them with `--collector` and `--ntp-server`. Python packages needed include `pycryptodome` and `pyserial`, OpenSSL 3.5 is needed for the cryptographic algorithms.
