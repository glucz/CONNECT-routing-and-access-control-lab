# Controlled lab results

Results recorded 2026-10-03 for a controlled HTTPS/CONNECT study.
Preserve deviations and exclusions when using these results.

## Evidence and collection status

The study archived complete campaign directories and regenerated all four sensitivity reports
from raw logs. Delivery CSVs matched the collected reports, and no retained body-accounting
discrepancy exceeded 1%. This repository distributes anonymized aggregates and timelines in
`published/`; private per-request logs and deployed configurations are excluded.

Run `python reproduce.py --output output/summary.csv` to check the published files and
regenerate the summary. `audit_results.py` is available for operators with the private logs.

| Campaign directory | Configurations | Planned / attempted runs | Final valid runs | Duration (s) | Proxy tunnel bytes |
|---|---:|---:|---:|---:|---:|
| `smoke-20261002-230742` | 34 | 34 / 34 | 34 | 2.596 | 749,871 |
| `pilot-20261002-231906` | 4 | 4 / 4 | 4 | 18.201 | 2,004,120,810 |
| `capacity-20261003-061741` | 120 | 360 / 360 | 360 | 1,651.900 | 1,028,674,328 |
| `composition-20261003-070131` | 81 | 243 / 243 | 243 | 146.736 | 4,048,364,796 |
| `rate-window-20261003-080012` | 48 | 48 / 48 | 48 | 1,043.871 | 5,042,554 |
| `reconnect-20261003-082629` | 20 | 20 / 21 | 20 | 1,556.596 | 2,514,082 |

The four sensitivity blocks comprise **269 configurations, 671 retained valid runs and
672 attempts**, using **5,084,595,760 tunnel bytes (5.085 decimal GB)**. Capacity and
composition have three repetitions per cell. Temporal blocks have one retained run per
cell; their retry is an infrastructure exclusion, not an added independent repeat batch.
Do not compute a global "success rate" across this deliberately mixed set of policy cases.

Smoke and pilot are deployment/calibration evidence, not additional sensitivity replicates.
Four earlier smoke directories document failed/interrupted deployment attempts; keep them
in the provenance record and exclude them from scientific delivery comparisons. The two
capacity report directories are duplicate analyses of one campaign, not two experiments.

**The reported study did not execute the 34-by-30 registered full campaign.** `preregistration.md` remains marked
draft. Saved plans and code hashes establish what the runner executed, not external
registration or author approval. Do not describe these sweeps as a completed registered
1,020-run baseline, or retrospectively label the existing draft as prospectively approved.

## 1. Capacity: thresholds match, but 99% is not complete delivery

Evidence: [capacity delivery table](published/capacity/delivery.csv)
and its `aggregates.json`. All 360 retained runs match predicted delivered-record counts,
source-address counts and tunnel counts. There are no missing/invalid retained runs.

| Sub-block | Cells / runs | Runs meeting 99% criterion | Runs delivering all records |
|---|---:|---:|---:|
| Reuse versus address quota | 44 / 132 | 27 | 24 |
| Held-session threshold | 76 / 228 | 168 | 120 |
| Total | 120 / 360 | 195 | 144 |

Each job requests R=1,000 records of 1,024 bytes. In a window that does not reset,
each held source pays one request for login, leaving L-1 record requests. For the
balanced session allocation used here, full delivery requires S >= ceil(R/(L-1)).
This is an observed agreement with an imposed quota mechanism, not an estimate of a
real service's capacity.

Illustrative measured boundary: at L=334, S=2 delivers 666/1,000, S=3 delivers
999/1,000, and S=4 delivers 1,000/1,000. The distinction holds for every-request and
binding policies, and for reuse M=1 and M=100. Three sources satisfy the 99% rule but
do not complete the job. Across the whole capacity grid, **51 retained runs passed
99% while failing complete delivery**; manuscript tables must preserve both criteria.

For rotating clean exits with every-request checks, M=100 and L=99 delivers 990/1,000;
L=100 or 101 delivers all 1,000. The quota applies per newly selected source, so M
controls when rotation refreshes capacity. At L=99, one held source instead delivers
98 records after login. Source binding rejects rotating post-login sources regardless
of their clean classification. These distinctions explain the address/reuse mechanism.

## 2. Composition: feasibility is stable; expensive-byte exposure changes

Evidence: [composition table](published/composition/delivery.csv)
and [aggregate measurements](published/composition/aggregates.json).
The grid crosses 10/100/1,000 segments of 65,536 bytes, manifests of
1,000/10,000/1,000,000 bytes, three policies and three routes, with three repetitions.
All 243 runs match delivery, source-count and tunnel-count predictions.

| Policy | Split clean-control/dirty-payload | All-clean rotating | All-clean held |
|---|---|---|---|
| Login-only | Complete | Complete | Complete |
| Every request | Blocked | Complete | Complete |
| Bound to login source | Blocked | Blocked | Complete |

This table holds at every tested composition. There are 162 complete deliveries and
81 expected policy failures, with no partial segment deliveries in retained runs.

Measured clean **tunnel-byte** share for successful split routing under login-only checks
(percent; each mean covers three runs, identical within the exported precision):

| Segments | 1,000-byte manifest | 10,000-byte manifest | 1,000,000-byte manifest |
|---:|---:|---:|---:|
| 10 | 1.35154% | 2.64967% | 60.25401% |
| 100 | 0.13745% | 0.27268% | 13.21664% |
| 1,000 | 0.01376% | 0.02734% | 1.50007% |

The clean share ranges from about **0.0138% to 60.254%** within this synthetic grid.
It includes TLS/framing and both traffic directions, not just response-body composition.
For example, a 1,000-byte manifest still incurs about 9,123 clean tunnel bytes for
login/control. Do not equate this with the separately cited real-world control-share
estimate or claim it measures a real streaming service. All-clean successful routes
have clean share 1 by construction. A blocked split route has no successful-delivery
cost observation; it must not be treated as a cheap successful route.

The economic contribution is a measured mapping from feasible routing to chargeable
traffic share. `prices.csv` evaluates archived price scenarios offline; no commercial
proxy invoice or current market price was measured. Held-address costing reserves a
whole address-month per used source and assumes no amortization across workloads.

## 3. Timing: the exact rate boundary is sensitive to arrival timing

Evidence: [rate/window delivery](published/rate-window/delivery.csv)
and [request timeline](published/rate-window/timeline.csv).
All 48 runs are valid on their first attempt; 23 deliver all 31 records. Quota L=10;
windows W=2 or 10 seconds; nominal offered rate is 0.5, 1 or 2 times L/W. Windows
start at each source's first arrival; login and denied requests count. Requests are
serial with minimum client start spacing, not a concurrent load/saturation experiment.

- Held sources at 0.5 L/W deliver 31/31 in all eight corresponding cells.
- Held sources at 2 L/W deliver 19/31 in all eight cells. This is a finite-job,
  first-window/login result, not a general steady-state acceptance fraction.
- At exactly L/W, three of eight held cells deliver 31/31; four deliver 30/31 and
  one delivers 29/31. These five cells differ from the ideal schedule's 31/31.
- Rotating sources under every-request checks deliver 31/31 in all 12 cells.
  Rotating sources under binding deliver 0/31 in all 12 cells.

| Boundary cell suffix | W (s) | Policy | M | Ideal / observed records |
|---|---:|---|---:|---:|
| 009 | 2 | every | 10 | 31 / 30 |
| 012 | 2 | bind | 1 | 31 / 30 |
| 013 | 2 | bind | 10 | 31 / 30 |
| 033 | 10 | every | 10 | 31 / 30 |
| 037 | 10 | bind | 10 | 31 / 29 |

In these cells, the six denied requests are the eleventh request in the active window,
arriving approximately **0.577 to 9.375 milliseconds before its reset boundary**.
This is directly calculable from the saved origin-arrival timeline. Client-side spacing
does not guarantee identical spacing at the origin across connection/TLS delays.

Replaying the specified limiter using recorded source identities and arrival times
matches every observed status/reason (zero mismatches over 1,536 requests). Source and
tunnel patterns also match. **Do not say every ideal prediction matched.** The ideal
boundary deviations are a useful finding. Timestamp-conditioned replay verifies internal
mechanism consistency; it is not an independent prediction of network arrival timing.

## 4. Reconnection: binding follows the source, not the transport connection

Evidence: [reconnect delivery](published/reconnect/delivery.csv)
and its `timeline.csv`. Each session requests 61 records, paced at 0.5 or 2 seconds
(about 30.5 or 122 seconds). Baselines have no forced reconnect; other cells close
tunnels every 5 or 20 records and either retain or change the source. The same cookie
and application identity remain in use; there is no relogin and no rate quota.

| Condition | Retained cells | Records delivered per cell |
|---|---:|---:|
| Every-request policy, all reconnect/source variants | 10 | 61/61 |
| Binding, baseline or source-preserving reconnect | 6 | 61/61 |
| Binding, source change every 5 records | 2 | 5/61 |
| Binding, source change every 20 records | 2 | 20/61 |

All 20 retained runs match the ideal record count, observed-source/tunnel pattern and
timestamp-conditioned replay (1,240 retained requests). Binding fails at the first
source change, not merely because TCP/TLS was reopened. The probe continues sending
after denial to observe the rest of the session; this is not a video player stopping
at its first denied segment. No cookie-expiry, long idle-timeout, multi-hour session
or real-service resilience conclusion follows from these durations.

### Preserve the excluded attempt

`time-reconnect-003`, attempt 1, delivered all 61 records with no transport errors,
but `origin_consistent` was false. The raw paired logs identify **segment index 59**:
both client and origin record HTTP 200 and 1,024 bytes, but the origin row has
`complete=false` while the client has `complete=true`. The infrastructure validity
check excluded this attempt. Attempt 2 is valid and supplies the retained result.

A close/drain bookkeeping interaction is plausible, but these logs do not establish
the root cause. Do not report data loss, a policy failure, zero invalid attempts, or
a proven software diagnosis. Campaign `invalid_runs=0` counts final retained attempts;
it does not erase the excluded first attempt. The campaign's 21 attempts and traffic
total include it. A separate one-file origin backup is not a complete cross-campaign
server trace; B's per-campaign `origin.jsonl` is the audit source here.

## Suggested manuscript text

> We evaluated the mechanisms in a two-server, owned-endpoint HTTPS/CONNECT lab with
> controlled source-address selection and three imposed access policies. Four parameter
> blocks covered 269 configurations and produced 671 retained valid runs (672 attempts).
> The capacity and payload-composition blocks matched predicted delivery, source-address
> and tunnel counts in all 603 runs. Capacity results distinguished 99% acceptance from
> complete delivery: 51 runs satisfied the former but not the latter. Under login-only
> checks, successful split routing's measured clean tunnel-byte share ranged from
> 0.0138% to 60.254% across the synthetic composition grid; every-request checking and
> source binding restricted which routes remained feasible.
>
> Timing experiments exposed a boundary qualification. Five of 48 configurations
> delivered one or two fewer records than an ideal request schedule predicted; their
> denied arrivals occurred just before per-source window resets. Replay using measured
> arrival times matched all decisions. In the reconnect block, reopening tunnels with
> the same source preserved delivery, whereas changing the source caused bound sessions
> to fail at the first change. One attempt was excluded for inconsistent completion
> bookkeeping and passed on the permitted retry. These experiments validate behavior
> under specified synthetic policies; they do not estimate real-service enforcement,
> residential-address acceptance, session lifetime, or commercial operating profit.

## Integration guidance and remaining decisions

Use the composition feasibility table and the two threshold examples in the main text;
show the rate-boundary qualification rather than burying it as noise. Put full grids,
timeline plots, price scenarios, accounting definitions and the excluded attempt in
the supplement. Plots must label the 99% and 100% criteria separately and distinguish
ideal-schedule predictions from timestamp-conditioned replay.

There is no need to rerun a completed campaign merely to recover a missing terminal
summary. Read `campaign.json` and regenerate reports from raw directories. Existing
sweeps are enough to write the controlled-mechanism results above. Whether to execute
the older 1,020-run baseline is a manuscript/design decision; its 30 repetitions are
not implied by the decision to omit independent temporal repeat batches.
If that baseline is retained, freeze its protocol prospectively and report it separately
from the already observed sensitivity data. The operating sequence and commands are
in [RUN-LAB.md](RUN-LAB.md).
