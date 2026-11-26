# Per-experiment screen logs

Audit trails for the experiments in the dissertation *Cross-Platform Energy Analysis of Post-Quantum Cryptography Key Generation* (SOC11101). One plain-text file per experiment:

| File | Experiment |
|---|---|
| `E1.txt` | E1: platform capability baseline (compute, memory and OpenSSL speed benchmarks) |
| `E2.txt` | E2: key generation via one `openssl genpkey` process per key (shell invocation) |
| `E2b.txt` | E2b: key generation through the OpenSSL library in a single process |
| `E2c.txt` | E2c: E2b-style key generation with the processor clock pinned across a sweep of frequencies |

## What is in each file

- Each block is one session, taken from that session's `session.log` as written on the device under test and collected to the jump host. The log text is unchanged except for some layout tweaks: Overleaf formatting was replaced with plain-text headings (`== DUT-Pi5 ==`, `-- Smoke --`, `-- Full Run --`) that separate the platforms and the stages of each experiment.
- A block shows the version line (script and harness versions), the command line (the script and its arguments), the end-of-session summary and the closing line. For E1, blocks also include a status line (USED, NOT USED, or UNUSED) indicating whether the session fed the analysis in the dissertation.
- Sessions are named `<date>-<time>-<board>[-SMOKE|-CAL]`, for example `20261011-222947-DUT-Pi5`. `SMOKE` is a short functional check and `CAL` is the calibration that sets run lengths.
- Boards: `DUT-Pi5` (Raspberry Pi 5), `DUT-CM5` (Raspberry Pi Compute Module 5), `DUT-RISCV` (Orange Pi RV2), `DUT-PC` (Intel N97) and `DUT-Mac` (Mac mini M4).
- Clock times: the Pi 5, CM5 and Mac log UK time; the PC and RISC-V board logged UTC in the earliest sessions and were changed to UK time for later runs.
- The scripts named in the command lines (for example `e1_baseline.py`, `experimenter_e2b.py`, `calibrate_e2.py`) are the per-experiment versions used at the time. The published versions are in `../software/`: `experimenter_e1.py` for E1 and `experimenter_e2.py` for E2, E2b and E2c (see `../software/README.md`).

## Notes

- The logs are as written. They show the lab user name and the private lab addresses (192.168.55.x, an isolated network).
- The measured results for E2, E2b and E2c are in `../results/`; each row's `Experiment_ID` starts with the session id used in these logs. The raw per-run files are not published (see the top-level README).
- The block structure differs slightly between experiments because the harness was extended as the work progressed (for example, the summary table at the end of a session was added after the first E1 sessions).
