# Lab runbook

Updated 2026-10-03 against the actual two-server deployment and preserved results.
For an overview and offline quick start, read [README.md](README.md).
For paper findings, read [RESULTS.md](RESULTS.md).
This runbook covers setup, recovery, smoke, pilot, sensitivity sweeps and the optional
full baseline. **Do not rerun completed experiments just because terminal output is lost.**


## Repository contents and result reproduction

- `connect_lab/`: runtime, preparation, report generators and tests.
- `inputs/`: frozen price inputs; preparation does not need a parent paper repository.
- `published/`: anonymized reports from the four completed sensitivity blocks.
- `RESULTS.md`: interpretation, deviations and exclusions.

```sh
python reproduce.py --output output/summary.csv
```

This reads the bundled reports, checks their integrity and regenerates a summary without
network traffic or private data. Raw `results/`, individual JSONL logs, generated plans,
credentials, deployed configurations and local working notes are excluded by `.gitignore`.
Keep those files privately for audit; do not delete them merely to prepare a release.
The repository is prepared locally; no remote publication or license grant is implied.

## Roles, layout and lifecycle

- **A = origin/defender**: `origin.json`, TLS key/certificate, HTTPS 8443, loopback control 9080.
- **B = workload and CONNECT pool**: `campaign.json`, CA certificate, source IPv6 /64,
  loopback proxy ports 3128/3129/3130 and SSH-forwarded control port 9081.
- Run commands from **`/opt/ip-lab`**, the directory containing `connect_lab/`.
  Do not run package commands from `/opt/ip-lab/connect_lab`.
- A's origin, B's NDP responder when needed, and B's SSH control tunnel stay running in
  separate persistent terminals. A campaign creates/closes its own proxies; do not start
  an additional standalone proxy. Run only one campaign at a time.
- Runtime: Python 3.11+, standard library only. Preparation/tests need `cryptography`;
  optional plots need `matplotlib`. There is no requirement to install these on runtime servers.
- Origin source labels come from the TCP peer address. All hostnames are private lab names
  pinned by the proxy to A; no public DNS, public CA or commercial proxy service is involved.

Both servers may display the same shell hostname. Identify them by their assigned addresses
and configs, not the prompt. A's listener must be A's own address; B's targets must name that
same address; B's exit prefix must belong to B and must not contain A's address.

## 1. Prepare once, on the workstation

From the repository root:

```sh
python -m pip install -r requirements-dev.txt
python -B -m unittest connect_lab.test_lab connect_lab.test_system connect_lab.test_sensitivity connect_lab.test_temporal -q
python -m connect_lab.prepare --output lab-config --origin-ipv6 2001:db8:200:1::10 --exit-prefix 2001:db8:100:1::/64
```

Replace documentation addresses with the actual A IPv6 and B /64. `lab-config` must be new.
The generator reads the bundled `inputs/calibration_inputs.json` and
`inputs/posted_prices.csv`, resolved relative to the package location. If those
files are elsewhere, pass `--calibration /absolute/path/...` and
`--posted-prices /absolute/path/...`. Merely uploading an `analysis` directory to `/root`
does not change these default paths. Preparation freezes selected price rows and hashes.

Copy `lab-config/origin/*` to A and `lab-config/client/*` to B. Keep the origin private key
only on A. Certificates last 90 days; regenerate the matching bundle when required.
Do not regenerate working configurations simply to update Python code.

Example upload from the workstation (replace SSH aliases):

```sh
ssh root@ORIGIN_SSH "mkdir -p /opt/ip-lab"
ssh root@CLIENT_SSH "mkdir -p /opt/ip-lab"
scp -r connect_lab root@ORIGIN_SSH:/opt/ip-lab/
scp -r connect_lab root@CLIENT_SSH:/opt/ip-lab/
scp lab-config/origin/* root@ORIGIN_SSH:/opt/ip-lab/
scp lab-config/client/* root@CLIENT_SSH:/opt/ip-lab/
```

For an **update**, copy only the entire `connect_lab` code directory to both servers;
preserve root-level JSON configs, certificates, tokens and `results/`.

On both servers:

```sh
cd /opt/ip-lab
python3 --version
python3 -c 'import connect_lab; print(connect_lab.__file__)'
chmod 600 admin.token
```

On A: `chmod 600 lab-key.pem origin.json`. On B: `chmod 600 campaign.json`.
If tests are desired on Debian, install `python3-cryptography` first; run tests from the
package's parent directory. The preparation/provenance test skips if the bundled preparation
inputs are absent. Plot tests depend on optional matplotlib availability. Judge results
by the final unittest summary, not asyncio slow-task notices or expected rejection alerts.

## 2. Check address assignment before changing networking

On A:

```sh
cd /opt/ip-lab
python3 - <<'PYCODE'
import json
c = json.load(open('origin.json'))
print('A listener:', c['listen_ip'], c['port'])
print('Allowed source prefix from B:', c['source_prefix'])
PYCODE
ip -6 addr show
```

Confirm the listener is assigned to A. On B:

```sh
cd /opt/ip-lab
python3 - <<'PYCODE'
import ipaddress, json, subprocess
p = json.load(open('campaign.json'))['pool']
network = ipaddress.ip_network(p['exit_prefix'])
print('B exit prefix:', network)
for host, target in p['targets'].items():
    print(host, target['ip'], target['port'])
    assert ipaddress.ip_address(target['ip']) not in network, 'A target is inside B source pool: correct configuration'
    subprocess.run(['ip', '-6', 'route', 'get', target['ip']], check=True)
PYCODE
ip -6 addr show
```

A's `source_prefix` must match B's `exit_prefix`. The initial deployment accidentally used
B's own IPv6 as A's listener/target. A `local ... dev lo` route for the intended remote A
address on B is a strong indication of this configuration conflict. Correct `origin.json`
on A and every `pool.targets[*].ip` on B, then restart the origin. The private certificate
uses hostnames, so an IP correction alone does not require new certificates.

## 3. Enable local source delivery and NDP on B

First inspect the existing state; on an already working installation, keep it.

```sh
cd /opt/ip-lab
LAB_PREFIX=$(python3 -c 'import json; print(json.load(open("campaign.json"))["pool"]["exit_prefix"])')
ip -6 route show table local
sysctl net.ipv6.ip_nonlocal_bind
```

If the full local source-prefix route is absent, add it once. Save the prior sysctl value
only once, before changing it, so later restarts do not overwrite the original state:

```sh
test -e nonlocal-bind.before || sysctl -n net.ipv6.ip_nonlocal_bind > nonlocal-bind.before
sudo ip -6 route add local "$LAB_PREFIX" dev lo
sudo sysctl -w net.ipv6.ip_nonlocal_bind=1
```

If `route add` reports `File exists`, inspect the route rather than adding duplicates.
Record whether you added this route for teardown. **Binding successfully is not proof
that return packets will be delivered locally.** A diagnostic /128 assigned earlier can
work while every fresh allocated address fails. The local /64 route covers the whole pool.

A provider-routed /64 may need no NDP responder. Our deployed allocation was on-link:
the gateway asked B who owned each fresh source address. That required NDP **as well as**
the local route. Inspect `ip -6 route show default` / `ip -6 addr` for the actual interface.
The observed interface was `enp1s0`; substitute yours below if different.

On B, install `ndppd` if required. Create the following config only if it does not already
exist; otherwise inspect and reuse it:

```sh
sudo apt-get update
sudo apt-get install ndppd
cd /opt/ip-lab
python3 - <<'PYCODE'
import ipaddress, json
from pathlib import Path
interface = 'enp1s0'
p = json.load(open('campaign.json'))['pool']
network = ipaddress.IPv6Network(p['exit_prefix'])
assert network.prefixlen == 64
rules = []
for label in (0xc, 0xd):
    subnet = ipaddress.IPv6Network((int(network.network_address) | (label << 48), 80))
    rules.append(f'    rule {subnet} {{\n        static\n    }}')
with Path('ndppd-lab.conf').open('x') as out:
    out.write(f'proxy {interface} {{\n    router no\n' + '\n'.join(rules) + '\n}\n')
PYCODE
pgrep -a ndppd
sudo ndppd -c /opt/ip-lab/ndppd-lab.conf
```

Do not launch a second responder if one already uses this config. Leave it running in its
own terminal; Ctrl+C stops it. `static` covers only the clean and dirty /80 lab subranges,
and `router no` represents locally owned sources. The warning about a low prefix length
is expected for these intentional /80 rules; it does not itself establish failure.
Route/sysctl/responder setup here is not reboot-persistent. Recheck after a reboot.

## 4. A firewall, origin and SSH control tunnel

On A, allow TCP 8443 from **B's whole source /64**, not just B's main /128. Check both host
and provider firewalls. If UFW is already in use, the scoped host rule is:

```sh
cd /opt/ip-lab
B_PREFIX=$(python3 -c 'import json; print(json.load(open("origin.json"))["source_prefix"])')
sudo ufw allow proto tcp from "$B_PREFIX" to any port 8443
sudo ufw status verbose
```

Ensure IPv6 filtering is enabled in that UFW installation. Preserve SSH access. B is an
outgoing client here: opening inbound TCP 8443 on B is not the corresponding fix. Its
stateful firewall must allow replies and essential ICMPv6/NDP. Keep control and proxy ports
loopback-only; do not expose 9080/9081/3128-3130 publicly.

On A, stop an old origin before restarting. Always select a fresh log filename:

```sh
cd /opt/ip-lab
python3 - <<'PYCODE'
import json, time
from pathlib import Path
p = Path('origin.json')
c = json.loads(p.read_text())
c['log'] = f'origin-{time.time_ns()}.jsonl'
p.write_text(json.dumps(c, indent=2))
PYCODE
python3 -m connect_lab.origin --config origin.json
```

Leave it running. Expected default startup ports: `https: 8443`, `admin: 9080`.
The log is whatever `origin.json` says, not always `origin-internal.jsonl`. Keep old logs;
`cat` only displays a file and does not reset it. An origin restart changes the signing key
and loses policy state: do not restart it during a campaign.

In another A terminal, confirm the listener:

```sh
ss -lntp 'sport = :8443'
ss -lntp 'sport = :9080'
```

In a persistent B terminal (replace `ORIGIN_SSH`):

```sh
ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 127.0.0.1:9081:127.0.0.1:9080 root@ORIGIN_SSH
```

Use the existing SSH key/agent. A's 9080 must be reachable from the SSH session on A.

## 5. Preflight before any campaign traffic

In a separate B terminal, verify control health without printing the token:

```sh
cd /opt/ip-lab
python3 - <<'PYCODE'
import json
from connect_lab.campaign import Control
c = json.load(open('campaign.json'))
h = Control(c['control_port'], c['admin_token_file']).call('GET', '/health')
print({k: h.get(k) for k in ('epoch', 'active', 'arrival_clock')})
PYCODE
```

Current temporal runs need `arrival_clock: monotonic-v1`; otherwise update/restart A's
origin. `ConnectionRefusedError` here concerns the control path, not the CONNECT pool:
check A's origin, B's SSH tunnel and matching configured control ports. HTTP 403 means the
control tokens differ. Do not print tokens into troubleshooting transcripts.

Then check fresh addresses in all exit modes:

```sh
python3 -u - <<'PYCODE'
import json, socket, uuid
from connect_lab.proxy import AddressPool
p = json.load(open('campaign.json'))['pool']
t = next(iter(p['targets'].values()))
pool = AddressPool(p['exit_prefix'])
for tier in ('clean-stick', 'clean-rot', 'dirty-stick'):
    address = pool.allocate(tier, uuid.uuid4().hex)
    print(tier, 'fresh source:', address, flush=True)
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.bind((address, 0))
        s.connect((t['ip'], t['port']))
    print(tier, 'TCP OK', flush=True)
PYCODE
```

This validates TCP reachability only; smoke validates CONNECT, TLS, cookies and policy.
If fresh-source connects fail, stop here. Do not repeatedly burn campaigns to diagnose it.

### Packet-level diagnosis used during deployment

Capture simultaneously on A and B while repeating the fresh-source probe:

```sh
sudo tcpdump -ni any -nn 'icmp6 or (ip6 and tcp port 8443)'
```

| Observation | Next check |
|---|---|
| Default source works, fresh lab source times out | Whole-prefix firewall permission, NDP and local route; a single working /128 proves little. |
| A never sees B's SYN | Wrong destination, B egress routing or provider/host filtering before A. |
| A sees SYN but sends no SYN-ACK | Correct listening address/port and A's firewall. |
| A sends SYN-ACK; B sees gateway solicitations for fresh source without an advertisement | NDP responder/config/interface on B. A periodic router advertisement alone is not an NDP success test. |
| B advertises the source and receives SYN-ACK but sends no ACK | Check `ip -6 route get FRESH_SOURCE`: it must resolve as local; then inspect B's input firewall counters. This deployment was missing the full local route. |
| B's route to fresh source goes out `enp1s0`, not `local` | Add the local source-prefix route; nonlocal bind and NDP alone are insufficient. |
| TCP works, campaign still fails | Inspect TLS CA/hostname/expiry, proxy allowlist and actual `requests.jsonl`/`proxy.jsonl` errors. |

A packet capture sees packets before all local processing. Receiving SYN-ACK in tcpdump
alone does not prove that B's TCP socket received it. Do not disable the firewall blindly.

## 6. Campaign sequence and traffic planning

For a new deployment: **preflight -> smoke -> full-size pilot -> capacity -> composition
-> rate/window -> reconnect -> inspect results -> optional approved full baseline**.
For the reported study, these first six campaigns were completed. The repository ships
anonymized sensitivity reports under `published/`; private per-request logs are excluded.
The registered baseline was not part of the reported study.

| Stage | Planned runs | Runner/plan cap | Observed tunnel traffic in this deployment |
|---|---:|---:|---:|
| Smoke | 34 | 10 MB | 0.750 MB |
| Pilot | 4 | 3 GB | 2.004 GB |
| Capacity | 360 | 2 GB | 1.029 GB |
| Composition | 243 | 8 GB | 4.048 GB |
| Rate/window | 48 | 250 MB | 5.043 MB |
| Reconnect | 20 | 250 MB | 2.514 MB including one retry |
| Optional registered baseline | 1,020 | Configured pool cap; prepared default 230 GB | Not run; roughly 200 GB planning estimate |

Caps are total tunnel-byte limits, **not speed limits**. A lower `pool.byte_budget` reduces
all effective caps. Check the campaign summary for the actual cap. Interface counters also
include TCP/IP, SSH/control, NDP and background traffic; do not add A and B as unique traffic.
The pilot's high transfer throughput does not predict many-small-tunnel or paced-run duration.

All commands below run on B from `/opt/ip-lab`. Output directories must be new, even if a
previous attempt failed immediately. There is no automatic resume. Choose a fresh timestamp;
never delete completed results merely to reuse a name. Stop if a command exits with an error.

### 6a. Smoke

```sh
cd /opt/ip-lab
RUN="results/smoke-$(date +%Y%m%d-%H%M%S)"
python3 -m connect_lab.campaign --config campaign.json --output "$RUN" --smoke
python3 -m connect_lab.analyze "$RUN" --output "${RUN}-aggregate.json"
cat "$RUN/campaign.json"
```

Require `status=complete`, 34 finished runs and zero final invalids; check aggregate validity
and body accounting too. `success=False valid=True` is often an expected policy rejection.
`valid=False` is an infrastructure/accounting problem, not evidence of stronger policy.

| Route | Login-only | Every request | Source binding |
|---|---|---|---|
| Dirty held | Denied at login | Denied at login | Denied at login |
| Clean rotating | Pass | Pass | Fail |
| Split control/payload | Pass | Fail | Fail |
| Clean held | Pass | Pass | Pass |

The additional rate cells test exhaustion and enough held sessions. An invalid attempt is
retried once with a fresh identity; a valid policy denial is not retried. Both attempts stay
in `runs.jsonl`. `invalid_runs=0` describes final attempts, not necessarily zero invalid attempts.

### 6b. Full-size traffic pilot

```sh
RUN="results/pilot-$(date +%Y%m%d-%H%M%S)"
python3 -m connect_lab.campaign --config campaign.json --output "$RUN" --pilot
python3 -m connect_lab.analyze "$RUN" --output "${RUN}-aggregate.json"
cat "$RUN/campaign.json"
```

Four video cases, 1,000 segments of 1 MB: split/login and held/bind should deliver fully;
split/every and rotating/bind should stop on policy denial. This calibrates byte volume,
not the sensitivity conclusions. Preserve it separately as `mode=pilot`.

### 6c. Capacity, then composition

Run this block first with `BLOCK=capacity`. After reviewing its report, execute it again
with `BLOCK=composition`. It creates a frozen plan before sending experiment traffic.

```sh
BLOCK=capacity
STAMP=$(date +%Y%m%d-%H%M%S)
PLAN="${BLOCK}-plan-${STAMP}.json"
RUN="results/${BLOCK}-${STAMP}"
python3 -m connect_lab.sensitivity --block "$BLOCK" --output "$PLAN"
python3 -m connect_lab.campaign --config campaign.json --output "$RUN" --sensitivity-plan "$PLAN"
python3 -m connect_lab.sensitivity_report "$RUN" --output "${RUN}-report"
cat "$RUN/campaign.json"
echo "Report: ${RUN}-report"
```

Capacity: 120 cells x 3 repetitions; composition: 81 x 3. Read [SENSITIVITY.md](SENSITIVITY.md)
for the grids. Inspect `delivery.csv`, including full delivery versus 99%, prediction
mismatches and missing/invalid cells. `aggregates.json` contains measured clean-byte shares;
`prices.csv` is an offline scenario sweep, not additional traffic or an observed invoice.

### 6d. Rate/window, then reconnect

Run first with `BLOCK=rate-window`, then with `BLOCK=reconnect`:

```sh
BLOCK=rate-window
STAMP=$(date +%Y%m%d-%H%M%S)
PLAN="${BLOCK}-plan-${STAMP}.json"
RUN="results/${BLOCK}-${STAMP}"
python3 -m connect_lab.temporal --block "$BLOCK" --output "$PLAN"
python3 -m connect_lab.campaign --config campaign.json --output "$RUN" --sensitivity-plan "$PLAN"
python3 -m connect_lab.temporal --report "$RUN" --output "${RUN}-report"
cat "$RUN/campaign.json"
echo "Report: ${RUN}-report"
```

These use **`temporal --report`**, not `sensitivity_report`. There is one run per cell and
no independent repeat batches. Scheduled pacing totals about 17.4 and 25.4 minutes plus
overhead. [TEMPORAL.md](TEMPORAL.md) defines the window and reconnect semantics. Read both
`delivery.csv` and `timeline.csv`: distinguish ideal-time deviations from measured-arrival
replay, and check source/tunnel patterns. Longer duration here does not test cookie expiry.

### 6e. Optional full baseline

This is a separate older design: 34 cells x 30 repetitions, full-size video payloads and
scrape rate limits L=50/200. It is not another name for the sensitivity sweeps and is not
required simply to recover missing summaries. Decide whether to retain it in the paper.

The repository `preregistration.md` is still a draft. If proceeding, prospectively freeze
the actual protocol as `approved-preregistration.md` on B; describe already seen sensitivity
results as prior work, not as data collected under a retroactively approved plan. The CLI
flag records a file/hash and the operator's assertion; it does not obtain external approval.
Inspect the current cap before starting:

```sh
python3 - <<'PYCODE'
import json
c = json.load(open('campaign.json'))
print('Configured full-run tunnel cap, bytes:', c['pool']['byte_budget'])
PYCODE
```

The prepared default is 230,000,000,000 bytes. If it was reduced for diagnostics, restore
an appropriate budget deliberately; the full mode has no separate built-in 230 GB override.
Then:

```sh
RUN="results/full-$(date +%Y%m%d-%H%M%S)"
python3 -m connect_lab.campaign --config campaign.json --output "$RUN" --approved-preregistration approved-preregistration.md
python3 -m connect_lab.analyze "$RUN" --output "${RUN}-aggregate.json"
cat "$RUN/campaign.json"
```

Expect 1,020 finished runs only if the cap and infrastructure permit it. Policy counters
reset between cells; attempts use fresh identities. The roughly 200 GB estimate is not a
measured full-run total. Ctrl+C marks interruption and retains partial results; reaching
budget closes tunnels and stops scheduling. Neither outcome silently becomes policy failure.

## 7. Recover summaries and reports without new traffic

The shell variables `RUN`/`REPORT` disappear in a new terminal. That does not lose results.
Find campaign summaries separately from adjacent report directories:

```sh
cd /opt/ip-lab
find results -type f -name campaign.json -print
find results -type f -name delivery.csv -print
python3 - <<'PYCODE'
import json
from pathlib import Path
for p in sorted(Path('results').rglob('campaign.json')):
    c = json.loads(p.read_text())
    print(p.parent, c['status'], 'finished', c['finished_runs'], '/', c['planned_runs'],
          'attempts', c['attempts'], 'final_invalid', c['invalid_runs'], 'bytes', c['proxy_bytes'])
PYCODE
```

To regenerate one report, set `RUN` to its exact **campaign directory** (not a report folder):

```sh
RUN="results/capacity-20261003-061741"
OUT="${RUN}-report-$(date +%Y%m%d-%H%M%S)"
python3 -m connect_lab.sensitivity_report "$RUN" --output "$OUT"
```

Use `temporal --report "$RUN" --output "$OUT"` for temporal campaigns. Use
`analyze "$RUN" --output "${OUT}.json"` for smoke/pilot/full. These commands read local logs
and send no experiment traffic. Regenerate into a new path rather than overwriting evidence.

`delivery.csv` lives in the report directory, **not** the raw campaign directory. Raw contents:

| File | Purpose |
|---|---|
| `campaign.json` | Status, attempts/final invalids, traffic and available interface deltas |
| `env.json` | Frozen cells, seed, code hashes, private config/price provenance |
| `sensitivity-plan.json` | Archived capacity/composition/temporal plan when applicable |
| `approved-preregistration.md` | Archived protocol for full baseline only |
| `runs.jsonl` | Every attempt, not only the retained outcome |
| `requests.jsonl` | Client completion, bytes, statuses and identifiers |
| `origin.jsonl` | Collected origin decisions, peer sources and completion records |
| `proxy.jsonl` | Source-bound tunnels and encrypted/framing byte counts |
| `network-counters.json` | Interface snapshots when supported by that runner version |

A's original internal log is a useful backup. B collects campaign-specific origin rows,
so deleting an old A log does not automatically invalidate completed B-side evidence.
Prefer preserving/renaming it. Raw files contain private addresses/identifiers/configuration;
use generated anonymous reports for the paper supplement.

Package all generated reports for transfer (this includes duplicate reports, not duplicate runs):

```sh
cd /opt/ip-lab
find results -type f -path '*-report*/*' -print0 |
  tar --null -czf "lab-reports-$(date +%Y%m%d-%H%M%S).tar.gz" -T -
```

Also retain the entire raw results tree privately. On the workstation, `audit_results.py`
rebuilds reports when private raw logs are available. To reproduce the published summary
without private logs, run `python reproduce.py --output output/summary.csv`.

## 8. Stop and restore only lab settings you changed

Stop campaigns first, then A's origin, B's SSH tunnel and foreground NDP responder with Ctrl+C.
Proxies close with their campaign. Only if you added the local-prefix route, remove it:

```sh
cd /opt/ip-lab
LAB_PREFIX=$(python3 -c 'import json; print(json.load(open("campaign.json"))["pool"]["exit_prefix"])')
sudo ip -6 route del local "$LAB_PREFIX" dev lo
sudo sysctl -w "net.ipv6.ip_nonlocal_bind=$(cat nonlocal-bind.before)"
```

Do not remove routes/settings that existed before the lab. Remove only diagnostic /128s you
added, retain result files, and keep required management access. No VPN forwarder or NAT is
part of this setup.
