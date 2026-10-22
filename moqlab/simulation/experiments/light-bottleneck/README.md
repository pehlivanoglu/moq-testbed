# Light shared-bottleneck experiment — 2026-10-22

Completed **40 runs**: CUBIC/BBR × FIFO/RED/PIE/FQ-CoDel/DualPI2 × seeds
17/29 × run numbers 1/2. Each pair has four runs. All use the same binary,
two-client topology, ECT(0), and 120 simulated seconds. No detector equations
or thresholds changed, and no run was excluded based on detector performance.

[Full report](../../.runs/light-matrix-20261022/REPORT.md) ·
[All 40 results CSV](../../.runs/light-matrix-20261022/runs.csv) ·
[Pair means and ranges CSV](../../.runs/light-matrix-20261022/pairs.csv) ·
[Comparison plot](../../.runs/light-matrix-20261022/comparison.png)

These links refer to local ignored experiment archives. Preserve the complete
`simulation/.runs/light-matrix-20261022/` directory with the research results;
generated configurations, exact run/analysis commands, raw outputs and provenance
are there. A version-controlled starting configuration is
[shared-light.yaml](../../examples/shared-light.yaml) (CUBIC/FIFO, seed17,
two repetitions).

## Workload and calibration

Origin production falls from 7 to **3.9 Mbps**, so unconstrained fan-out demand
falls from 14 to **7.8 Mbps** over the same **8 Mbps** shared link. Protocol/wire
overhead is additional. Default queue limit falls from 100 to **40 packets**;
RED thresholds scale from 20/60 to **8/24 packets**. Target is 15 ms where
applicable; DualPI2 step/update/coupling remain 1 ms/16 ms/2. Its limit is
60,000 bytes (40×1500), not a strict 40-packet limit. All directed default
queues change with the selected AQM. No added loss/jitter or external traffic.

Separate seed91 FIFO pilots established load/queue settings; they are not
included in the 40-run results. The 100-packet pilot retained long FIFO delays,
so the final matrix uses 40. Calibration did not optimize SBD accuracy.

## Results

Numbers are equal-weight means of four runs. Loss is the mean of the detector's
recorded flow/interval loss metric, not total physical packet-loss percentage.
Recall/F1 exclude unknown decisions. All pairs have approximately **70.47%**
known-decision coverage; 101 initial unknown windows and 241 known windows per
run. Detailed confusion matrices, undefined values and ranges are in the CSVs.

| CC | AQM | Queue mean ms | Mean SBD loss % | Active-reference recall % | F1 % |
| --- | --- | ---: | ---: | ---: | ---: |
| CUBIC | FIFO | 34.94 | 1.662 | 100.00 | 99.95 |
| CUBIC | RED | 14.88 | 0.193 | 85.08 | 90.81 |
| CUBIC | PIE | 22.41 | 0.645 | 100.00 | 99.74 |
| CUBIC | FQ-CoDel | 14.25 | 0.231 | 38.28 | 54.89 |
| CUBIC | DualPI2 | 14.32 | 0.031 | 95.38 | 96.74 |
| BBR | FIFO | 8.48 | 0.652 | 18.13 | 17.05 |
| BBR | RED | 7.25 | 0.440 | 0.00 | 0.00 |
| BBR | PIE | 4.93 | 0.694 | 0.00 | 0.00 |
| BBR | FQ-CoDel | 8.43 | 0.277 | 17.72 | 29.58 |
| BBR | DualPI2 | 6.84 | 0.374 | 8.73 | 14.51 |

All **27,200** recorded unique flow/interval loss summaries are below the
detector's strict **>25%** high-loss threshold. Worst startup value is 24.125%;
after 35.7 s the worst value is **2.096%**. No missing intermediate recorded
intervals, and every grouping interval is represented in these observations.

CUBIC generally groups these flows more often than BBR under this workload;
FQ-CoDel exposes misses even with very low loss. BBR does not have continuous
active congestion under the operational reference: reference-active time ranges
from roughly 56% to 97% depending on AQM. BBR/FIFO recall varies from 0% to
72.54% across runs. Means alone should not be read as stable rankings.

## Limits

Two seeds/four runs per pair are a small sample; min/max values are not
confidence intervals. This is one shared topology without an independent-path
negative control. Ground truth is an operational queue criterion; FQ-CoDel
shares root capacity while isolating inner queues. Undefined precision/FPR
remain undefined. ECT(0) is classic traffic, not a scalable-controller/L4S test.
No physical calibration or PCAP/qlog in this matrix.
