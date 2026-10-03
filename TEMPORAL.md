# Request timing and session reconnect experiments

These two controlled experiments extend the completed capacity and composition sweeps.
Each configuration runs once; there are no independent repeat batches. An invalid
transport attempt can still be retried once. A policy denial is never retried.

## Update A and B

Upload the entire updated `connect_lab` directory into `/opt/ip-lab/connect_lab` on
both servers. Keep the existing private configurations, certificates and results.
Do not change the working firewall, IPv6 local route, NDP responder or SSH tunnel.

Stop the origin on A with Ctrl+C, then select a fresh log and restart:

```bash
cd /opt/ip-lab
python3 - <<'PY'
import json, time
from pathlib import Path
p = Path('origin.json')
c = json.loads(p.read_text())
c['log'] = f'origin-temporal-{time.time_ns()}.jsonl'
p.write_text(json.dumps(c, indent=2))
PY
python3 -m connect_lab.origin --config origin.json
```

Keep it running. This origin version records monotonic arrival timestamps. The
campaign checks the origin capability and refuses to run against an old version.
Existing origin logs do not need to be deleted.

## 1. Rate and limit-window sweep, on B

```bash
cd /opt/ip-lab
STAMP=$(date +%Y%m%d-%H%M%S)
PLAN="rate-window-plan-${STAMP}.json"
RUN="results/rate-window-${STAMP}"

python3 -m connect_lab.temporal --block rate-window --output "$PLAN"
python3 -m connect_lab.campaign --config campaign.json \
  --output "$RUN" --sensitivity-plan "$PLAN"
python3 -m connect_lab.temporal --report "$RUN" --output "${RUN}-report"
cat "$RUN/campaign.json"
```

48 configurations: windows of 2 and 10 seconds, address quota L=10, nominal
request rates of 0.5, 1 and 2 times L/window, every-request and source-binding
defenders, held and rotating clean routes, and tunnel reuse of 1 and 10 requests.
Each uses one login followed by 31 records of 1,024 bytes. Scheduled pacing totals
17.36 minutes, plus request and campaign overhead. Each cell receives a fresh policy.

The limiter is a first-arrival-anchored fixed window **per source**, not a sliding
window, token bucket or shared wall-clock window. Login and denied requests consume
quota. Requests are serial, with a minimum interval between request starts (including
login to first record); delayed requests never trigger catch-up bursts. This tests
offered-load timing, not concurrent saturation or total bandwidth throttling.

At the exact nominal boundary, TLS and network delay can move an arrival across a
window boundary. The report retains both ideal-schedule predictions and predictions
conditioned on measured origin arrival times. A window reset is intended and does
not mark a temporal run invalid. The old capacity sweep's no-expiry check is preserved.

## 2. Longer sessions and forced reconnects, on B

```bash
cd /opt/ip-lab
STAMP=$(date +%Y%m%d-%H%M%S)
PLAN="reconnect-plan-${STAMP}.json"
RUN="results/reconnect-${STAMP}"

python3 -m connect_lab.temporal --block reconnect --output "$PLAN"
python3 -m connect_lab.campaign --config campaign.json \
  --output "$RUN" --sensitivity-plan "$PLAN"
python3 -m connect_lab.temporal --report "$RUN" --output "${RUN}-report"
cat "$RUN/campaign.json"
```

20 configurations: 61 records paced at 0.5 or 2 seconds (minimum run durations
30.5 and 122 seconds), every-request and source-binding defenders, a no-forced-close
baseline and forced reconnects every 5 or 20 records, preserving or changing source.
Scheduled pacing totals 25.42 minutes plus overhead. No rate cap is applied in this block.

All reconnect configurations start on a held clean source. On a forced reconnect,
the driver closes its tunnels and retains its application identity and login cookie.
The hold variant keeps its proxy source identity; the rotate variant changes that
identity so the next connection uses a new clean source. There is no relogin.
This avoids confusing a source change immediately after login with a source change
partway through a session. Expected binding failures begin at the first forced source
change; reconnecting with the same source should retain full delivery.

The scrape probe continues after denials so the full observation period is retained.
It is not a video player that stops at its first failed segment. This isolates TCP/TLS
reconnection and source binding; the origin has no session expiry, and this block does
not test cookie lifetime or deliberate idle-timeout expiry.

## Traffic, outputs and interpretation

Each campaign has a 250 MB tunnel-byte cap, further limited by any lower existing
pool cap. Across both default grids, response bodies are at most about 2.84 MB;
TLS, HTTP, TCP/IP, control traffic and retries add overhead. The caps are safeguards,
not transfer targets or speed limits. Actual proxy bytes and interface deltas are saved.

Use **`connect_lab.temporal --report`**, not `sensitivity_report`, for these new plans.
Each report contains:

- `delivery.csv`: validity, ideal and observed records, timestamp-conditioned replay,
  source/tunnel-pattern mismatches, measured request rate, resets and reconnect counts.
- `timeline.csv`: per-request elapsed time, status/reason, anonymous source and tunnel
  ordinals, and reconstructed window state. No addresses or session identifiers.
- `aggregates.json`: the standard measurement and traffic summary.

Send both campaign summaries and both reports' `delivery.csv` and `timeline.csv`.
Retain all raw campaign directories and origin logs. Replay matches show consistency
with the specified mechanism; they are not independent evidence about real services.
Inspect ideal-vs-observed differences separately from replay and source/tunnel-pattern
mismatches. Missing or invalid runs must not be interpreted as policy failures.

Local regression command (requires the test certificate dependencies):

```bash
python3 -B -m unittest connect_lab.test_lab connect_lab.test_system \
  connect_lab.test_sensitivity connect_lab.test_temporal -q
```
