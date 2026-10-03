# CONNECT exit pool and workload driver

The complete lab now includes `origin.py`, `campaign.py`, `analyze.py` and `prepare.py`.
Use [the two-server runbook](../RUN-LAB.md) to prepare, run and analyze the whole experiment.
The individual proxy/driver commands below remain useful for isolated jobs.

**For the deployed lab, start with RUN-LAB.md, not the standalone proxy commands below.**
The sequence is preflight, smoke, pilot, capacity, composition, rate/window, reconnect,
then an optional separately approved full baseline. The campaign owns its proxies.
Read [the audited results](../RESULTS.md)
for the completed experiments; the 1,020-run full baseline is not in the reported results.

For the post-pilot parameter sweeps, use [SENSITIVITY.md](../SENSITIVITY.md).
`sensitivity.py` creates offline frozen plans; `campaign --sensitivity-plan` runs them;
`sensitivity_report.py` exports delivery boundaries, price comparisons and optional SVG/PNG plots.

For request pacing across rate windows and cookie-preserving tunnel reconnects, use
[TEMPORAL.md](../TEMPORAL.md). `temporal.py` generates those plans and their timeline reports.

This is the missing traffic-generation component for the latest `../preregistration.md`:

```
driver -- CONNECT on loopback --> exit pool -- source-bound TCP + TLS --> owned origin
```

Production runtime: Python 3.11+ standard library. Run the origin on A, and the campaign
(which starts proxy and driver) on B. For standalone use, proxy and driver both run on B.
The production origin, authenticated policy-control API, matrix scheduler and aggregate analysis
are included as separate modules. `campaign.py` owns its proxy pool and invokes the driver directly.
The test fixture in `test_lab.py` remains test-only; production runs use `origin.py`.
Infrastructure provisioning and starting the registered experiment are explicit runbook steps.

## Standalone component reference (not the campaign startup sequence)

From the `experiment` directory:

```sh
python -m connect_lab.proxy --config pool.json
# A separate terminal/process, after the origin policy has been set:
python -m connect_lab.driver --config video.json --output results/video-001
python -m connect_lab.driver --config scrape.json --output results/scrape-001
```

Copy `pool.example.json`, `video.example.json` and `scrape.example.json` to your private run
configuration and substitute the lab addresses, hostnames and CA path. The examples contain only
documentation addresses and will not reach a real server. Paths are relative to the working
directory. Each output directory and proxy log must be new; existing results are never overwritten.

For standalone jobs, keep one proxy process running so all three exit sets share the configured
cap (230,000,000,000 bytes in the example). On reaching it the pool closes active tunnels and refuses subsequent
ones. Counters include both directions of relayed tunnel data, including TLS, but exclude CONNECT
headers and TCP/IP overhead. The cap bounds bytes enqueued for relay, not every byte a kernel might
receive before the sockets close. A restart resets counters and rotation state: it is a new campaign,
not a supported resume. Archive the old log and subtract its expenditure from any new campaign cap.
Use a fresh run ID for each attempt, including an infrastructure rerun.

## Linux source-address prerequisite

The proxy server must own a /64 with a working return path, and the origin must be reachable over IPv6.
The operator can enable local ownership/nonlocal binding for that lab prefix, for example:

```sh
# Replace the documentation prefix with the routed lab prefix before running.
sudo ip -6 route add local 2001:db8:100:1::/64 dev lo
sudo sysctl -w net.ipv6.ip_nonlocal_bind=1
```

These commands are not executed by the package. A provider offering only on-link neighbor
discovery may require additional address/NDP provisioning; successful binding alone does not
prove return routing works. Verify source addresses in the origin log before a campaign.
Restrict the origin firewall to the lab source block. No VPN forwarder is required.
The actual on-link deployment required both an NDP responder for the lab /80s and a
local /64 route. RUN-LAB.md includes fresh-address probes and packet-level diagnosis.

The pool always listens on `127.0.0.1`. It only accepts CONNECT to exact configured hostname/port
pairs and dials their pinned numeric IPs, avoiding DNS changes or redirects to an unintended host.
The driver verifies the origin's certificate against the supplied CA and hostname. There is no
TLS interception and no redirect-following or direct-route fallback.

## Wire contract for the origin and orchestrator

The proxy accepts HTTP CONNECT with:

- `Proxy-Authorization: Basic base64(identity:lab)`; the username is the session identity.
  This identifies an experiment session, not a secret authentication credential.
- `X-Lab-Tunnel-Id`: a unique token for each tunnel. Reuse is rejected.

Identity/tunnel tokens contain only letters, numbers, `_`, `.`, and `-`, at most 160 characters.
The client additionally sends `X-Lab-Run-Id`, `X-Lab-Session-Id` and `X-Lab-Tunnel-Id` **inside TLS**
on every request, so the origin can join its own log with the client and proxy records.
Neither HTTP headers nor cookies determine the source seen by the origin; it must read its TCP peer.

Required origin responses:

| Request | Response contract |
|---|---|
| `/login` on login host | HTTP 200 and `Set-Cookie: lab_session=...`; cookie name configurable. Origin signs/verifies its own cookie. |
| `/manifest` on manifest host | HTTP 200, exactly `manifest_bytes` body bytes (default 10,000). |
| `/seg/{index}` on segment host | HTTP 200, exactly `body_bytes` body bytes. Zero-based index. Configure origin for video or scrape size before the job. |
| Denial | HTTP 403; `X-Lab-Reason: login_denied`, `dirty_exit`, `bind_mismatch`, `rate`, or `no_session`. |
| Every response | Valid `Content-Length`; HTTP/1.1 keep-alive for planned tunnel reuse. Chunked responses are intentionally not supported. |

The driver explicitly forwards the chosen login cookie only to its configured lab hosts. It
does not rely on a browser accepting a cross-host cookie. Missing cookies, missing framing,
short bodies, certificate failures and timeouts cannot count as successful records.

Origin policies must follow the **preregistration**, not the older handoff: `D-every` checks the
class on every request and allows different clean addresses; only `D-bind` compares the current
source with the login source. A scrape job has no manifest request, so its first possible binding
failure after login is the first record.

## Address and connection semantics

All exit sets share one allocator:

| Exit set / port in examples | Meaning |
|---|---|
| `clean-stick` / 3128 | Same identity, same source across all destination hosts and new tunnels. |
| `clean-rot` / 3129 | New source for every CONNECT, using a per-identity counter. |
| `dirty-stick` / 3130 | Same identity, same source, in the dirty class. |

The IPv6 host portion is exactly 64 bits: 16 class bits (`0x000c` clean / `0x000d` dirty), one
mode bit, and 47 SHA-256-derived bits. To classify, first validate membership in the configured /64,
then read `(int(ipv6_address) >> 48) & 0xffff`. Source collisions are detected and rejected; they
must invalidate affected runs. These labels are synthetic, not evidence of residential reputation.

Held mappings hash `(exit_set, identity, 0)`; rotating mappings hash `(exit_set, identity, counter)`.
The driver derives a session identity from its UUID run ID and session number. Stable mapping is
independent of hostname. A new connection, not a request within an existing connection, rotates
the address. Each host/exit pair has its own connection, reused for at most `reuse` requests.
If the origin closes a connection early, the next request opens and logs a new tunnel; no request
is automatically retried. Origin connection behavior can therefore change measured address counts.

The routing choices are `R-dirty-stick`, `R-clean-rot`, `R-split` and `R-clean-stick`.
`R-split` sends login/manifest through clean-stick and segments through dirty-stick.

Temporal reconnect tests are an explicit exception to the usual single proxy identity:
they retain the application session/cookie while the rotate variant changes proxy identity
at forced reconnects. Client logs record `source_generation` and `forced_reconnect`.
Origin timestamps support replay across per-source first-arrival-anchored windows.

## Jobs and results

The video example implements 1 login + 1 manifest + 1,000 one-MB segments, reuse 100. It stops
at the first failed request. The scrape example implements 1 login + 1,000 records of 26,227 bytes,
reuse 25; it continues after denials, including a login denial, with no retries. Success requires
all video segments or at least 99% of scrape records to be complete 200 responses of the expected
size. The whole-request deadline includes CONNECT, TLS and reading the response.

For held-address rate experiments, set `sessions` to `ceil(count/(L-1))`: 21 at L=50, or 6 at L=200,
for 1,000 records. The driver distributes the records evenly, with one login and identity per session.
Sessions run sequentially. The single-session control uses `sessions: 1`.

Output:

- `config.json`: exact job inputs, including generated run ID.
- `requests.jsonl`: one row per attempted request, session/tunnel IDs, route, status, denial reason,
  actual body bytes received, completeness, elapsed time, and transport/protocol error when present.
- `run.json`: success, counts, first failure, duration, and `valid` (false for transport/protocol errors).
- Proxy JSONL: start/config event; one final row per tunnel with selected and bound source,
  identity, tunnel ID, exit class, up/down bytes and outcome; budget-exhaustion event if triggered.

Wait for tunnels to close before collecting proxy rows. Abruptly killing the process can lose
unfinished tunnel rows; treat those runs as incomplete. Raw logs/configs contain internal addresses
and identities: keep them internal, and publish only the separately produced aggregate artifact.

`success` records the workload threshold; `valid` is separate so an infrastructure error cannot be
silently treated as an experimental result. The scheduler decides whether an allowed infrastructure
rerun is appropriate. It must halt scheduling after a budget event, set/reset origin policy and rate
counters between cells, randomize cells, and preserve both attempts of any rerun. This driver never
changes defender policies or retries whole jobs on its own.

Proxy bytes include TLS and HTTP framing; client `body_bytes` and origin body counters do not.
Compare client and origin body counts directly, and report proxy overhead separately. A raw 1%
comparison between these different accounting layers is not a valid consistency check.

## Local verification

```sh
# Only tests need this dependency; runtime needs no packages.
python -m pip install cryptography
python -B -m unittest connect_lab.test_lab -v
```

Tests generate an ephemeral local TLS certificate and contact only loopback. They exercise all
24 workload/routing/defender combinations at tiny volumes, real source binding and tunnel reuse,
rate-limit/multi-session behavior, destination rejection, truncated 200 responses, and the byte cap.
Portable socket tests substitute multiple IPv4 loopback sources; a separate test checks the real
IPv6 allocator and address layout. These tests do **not** validate the servers' routed IPv6 /64,
firewalls, production origin, or registered experiment results. Perform the separate IPv6 smoke
check on the lab infrastructure before collecting experimental data.
