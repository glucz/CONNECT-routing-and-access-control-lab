# Run the sensitivity campaigns

Implemented 2026-10-03, after the successful deployment smoke and 2.004 GB video pilot.
These are separate pre-specified mechanism-sensitivity blocks, not additions silently merged
into the old baseline preregistration. Save/review each generated JSON plan before executing it.
The plan, predictions, price grid, code hashes and seed are archived with the observations.

## 1. Update both servers

Upload the updated **entire `connect_lab` code directory** to `/opt/ip-lab/connect_lab/` on
**both A and B**. Keep each server's existing JSON configurations, certificates, token and
results. Do not regenerate addresses or change the working firewall, local /64 route or NDP setup.

On A, stop only the origin with Ctrl+C. Select a new log name and restart it:

```sh
cd /opt/ip-lab
python3 - <<'PY'
import json, time
from pathlib import Path
p = Path('origin.json')
c = json.loads(p.read_text())
c['log'] = f'origin-sensitivity-{time.time_ns()}.jsonl'
p.write_text(json.dumps(c, indent=2))
PY
python3 -m connect_lab.origin --config origin.json
```

Keep it running. Keep B's NDP responder and SSH control tunnel running too. A's update adds
read-only interface counters to its control API; B records its own counters as well.

## 2. Generate the small-payload capacity plan on B

```sh
cd /opt/ip-lab
python3 -m connect_lab.sensitivity --block capacity --output capacity-plan.json
```

This command is offline: no experiment traffic. Default grid:

| Block | Parameters | Cells |
|---|---|---:|
| Reuse × address limit | M=1,10,25,100; L immediately below/equal/above M where positive; every/bind; clean rotating/held | 44 |
| Held-session boundary | L=10,50,101,200,334; S around both 99% and complete thresholds; M=1,100; every/bind | 76 |

Every cell uses R=1,000 scrape records of **1,024 bytes**; three repetitions give **360 planned runs**.
The model predicts 0.261 GB response-body traffic. The explicit HTTP/TLS planning allowance brings
the estimate to **1.918 GB**, or 3.836 GB if every run needed its one allowed infrastructure retry.
These are assumptions, not measured or guaranteed totals. The proxy has a **2 GB hard cap**, including
retries. If it is reached, remaining cells stay missing; no automatic extra budget is added.

Many small TLS tunnels may take much longer than a similarly sized video transfer. Do not extrapolate
runtime solely from the pilot's MB/s. The runner reports cumulative `traffic_MB` after each attempt.

Inspect `capacity-plan.json`, then run exactly that saved plan:

```sh
RUN="results/capacity-$(date +%Y%m%d-%H%M%S)"
python3 -m connect_lab.campaign --config campaign.json --output "$RUN" --sensitivity-plan capacity-plan.json
python3 -m connect_lab.sensitivity_report "$RUN" --output "${RUN}-report"
cat "$RUN/campaign.json"
```

Expected infrastructure result: complete, zero invalid runs. Delivery failures are intended in
many cells. Compare observations with predictions in the report; mismatches remain findings.
Send the campaign summary and `${RUN}-report/delivery.csv` before committing to the heavier block.

## 3. Run composition separately after checking capacity results

```sh
python3 -m connect_lab.sensitivity --block composition --output composition-plan.json
RUN="results/composition-$(date +%Y%m%d-%H%M%S)"
python3 -m connect_lab.campaign --config campaign.json --output "$RUN" --sensitivity-plan composition-plan.json
python3 -m connect_lab.sensitivity_report "$RUN" --output "${RUN}-report"
cat "$RUN/campaign.json"
```

Composition varies 10/100/1,000 segments of 65,536 bytes and a 1,000/10,000/1,000,000-byte
manifest, crossed with login/every/bind and split/clean-rotating/clean-held routes. There are
**81 cells × 3 repetitions = 243 planned runs**. Estimated body traffic is **4.001 GB**;
the HTTP/TLS planning estimate is **4.073 GB**, with an **8 GB hard cap** including retries.
This varies byte composition and session segment count; it does not simulate paced playback duration.

The two independent campaign caps total 10 GB; they do not include earlier smoke/pilot traffic,
TCP/IP overhead, NDP or control traffic. No stage launches the next stage automatically.
Existing configuration caps can further reduce the effective cap; `campaign.json` records it.

## 4. Reports, prices and plots

Each report contains:

- `delivery.csv`: inputs, predicted/observed delivered fractions, 99% successes, complete deliveries,
  missing/invalid repetitions and prediction mismatches for delivery, addresses and tunnel count.
- `aggregates.json`: measurement statistics, timing, costs and interface counter deltas without
  raw addresses/run/session IDs.
- `prices.csv`: **offline** combinations of clean prices 0.49/1.45/5 USD/GB, dirty prices
  0.0000276/0.01/0.12/0.38 USD/GB and rents 0.225/1.25/4.6 USD/address-month.
  No extra traffic. Routes qualify only when every planned repetition delivers the chosen criterion.
- `README.md`: interpretation and accounting limits.

To generate standalone SVG/PNG scientific plots, install matplotlib on the reporting machine
(it need not be on the origin) and choose a new report directory:

```sh
python3 -m pip install matplotlib
python3 -m connect_lab.sensitivity_report results/CAMPAIGN-DIRECTORY --output results/NEW-REPORT-DIRECTORY --plots
```

Plots cover reuse-versus-limit delivery, held-session thresholds and clean-byte share versus
configured control-body fraction. Missing cells are not plotted as zeros. Raw campaign folders
are required for report generation: keep them private and publish only reviewed aggregates/figures.

`network-counters.json` records before/after host counters; `campaign.json` and aggregates contain
their deltas. Use the physical interface (e.g. `enp1s0`), not the sum of overlapping virtual devices.
Counters include background traffic, SSH/control transfers and NDP. Null values mean resets or
unavailable measurements, not zero. Proxy counts remain the enforced traffic budget, and neither
counter is automatically equated to the provider's billed bandwidth.

## Design interpretation

For held scrape addresses, one login consumes capacity, so complete delivery requires
`S = ceil(R/(L-1))`. The 99% criterion instead needs `ceil(ceil(0.99 R)/(L-1))` under even splitting.
For example, R=1,000 and L=334: three held sessions deliver 999 records, meeting 99% but not complete
delivery; four sessions deliver all records. Both boundaries are sampled explicitly.

Without binding, rotating record tunnels each carry up to M requests and can deliver at most L
requests per fresh source: the predicted delivered count is
`floor(R/M)*min(M,L) + min(R mod M,L)` for a single session. Under binding, their record sources
differ from the login source. Predictions assume normal reuse, no address collisions, no early
connection closes and no rate-window expiry. Runs lasting beyond a rate window are flagged invalid.

The three repetitions check repeatability of deterministic mechanisms; they are not a power analysis
for real-service success rates. Prices are hypothetical re-costings of measured lab quantities.
Whole-address-month costs assume dedicated reservations, not session sharing or amortization.
The cheapest candidate in the recorded grid is not a market-wide optimum or proof of profitability.
Reviewers should be shown discrepancies and missing cells, not only outcomes that match predictions.
