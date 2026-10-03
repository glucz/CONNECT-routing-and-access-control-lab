# CONNECT routing and access-control lab

## What this is and what it is good for

This project is a controlled experiment for studying how IP-based access rules affect the delivery of a workload through proxies. It includes an HTTPS server, a CONNECT proxy pool, a workload generator, and analysis tools. You control both ends of the connection, so you can change an access rule and measure which requests succeed, which source addresses they use, and how much traffic they generate.

The practical question is: **when does changing an IP address restore useful access, and what resources does that access require?** A cheap replacement address is useful only if it can deliver the workload. A service that checks the source at login can behave differently from one that checks every request or requires the whole session to keep the login address. Connection reuse, per-address quotas, and request timing can also change the number of addresses and bytes needed.

For example, consider a video-style job with a small login and manifest exchange followed by a large payload. If the server checks only the login source, the client may be able to send control requests through an accepted source and the payload through another. If later requests must come from the login address, that split fails. This lab lets you reproduce that difference and measure the traffic on each path.

Use the project to:

- **Study access-control behavior.** Compare login-only checks, checks on every request, and sessions bound to a source address under known conditions.
- **Test routing and capacity assumptions.** Measure how held or rotating sources, connection reuse, and request quotas affect complete delivery.
- **Examine timing effects.** Compare an ideal request schedule with arrivals recorded at the server, including quota-window boundaries and reconnects.
- **Inform cost calculations.** Measure address use and tunnel bytes, then examine cost scenarios using the bundled price inputs. Delivery measurements establish whether the assumed route works under the lab's rules.
- **Reproduce the reported results.** Verify the bundled reports and regenerate their summary locally, without deploying servers.

The lab accompanies the study *Cost Bounds for Commercial Proxy Evasion of IP-Based Access Controls*. Its source classes and access rules are synthetic. Applying the results to a commercial service requires separate evidence about that service's rules, usable address capacity, and billing terms.

## Quick start: reproduce the published summary

With Python 3.11 or newer, run this from the repository root:

```sh
python reproduce.py --output output/summary.csv
```

This command uses only the Python standard library. It verifies the published files against their SHA-256 manifest, checks agreement between reported run counts, and writes a CSV summary. It requires no network access, server deployment, or private logs. Choose a new output filename if the file already exists; the script refuses to overwrite it.

The bundled reports contain **269 configurations, 671 retained valid runs, and 672 attempts** across four experiment blocks. Read [the results](RESULTS.md) for the interpretation and [the report guide](published/README.md) for the exported data formats. Regenerating these reports from individual request logs requires the private raw data.

## How the lab works

Two servers divide the experiment:

```text
Server B                                             Server A
Workload driver -> CONNECT proxy pool -> HTTPS -> Controlled origin
                   selects source IP                 applies access rule
```

The origin reads the TCP peer address and applies the configured policy. The client records response completion, while the origin and proxy record decisions, source addresses, and traffic. Comparing these records separates a completed workload from a connection that merely opened or a response that arrived only in part.

### Access policies

| Policy | Rule |
|---|---|
| Login-only | The source must belong to the accepted class at login. |
| Every-request | Every request must use a source in the accepted class. |
| Source binding | Later requests must use the same source address as login. |

All policies require a valid session cookie. A separate per-source request quota can be enabled; login and denied requests count toward it.

### Routing choices

| Route | Behavior |
|---|---|
| Held | Keeps one source per identity across destinations and new connections. |
| Rotating | Selects a new source for each CONNECT tunnel. |
| Split | Sends login and manifest requests through an accepted held source and payload requests through a held source in the other class. |

The code calls the two source classes `clean` and `dirty`. These labels come from owned IPv6 subranges and represent an imposed acceptance rule. They do not measure residential reputation or membership in a commercial blacklist.

Workloads include video-style segment delivery and page-style retrieval. Payloads are generated bytes. The reports distinguish complete delivery from the retrieval workload's 99% acceptance criterion, and distinguish response-body bytes from encrypted tunnel traffic.

## What the completed experiments show

| Experiment block | Configurations | Retained valid runs | Question |
|---|---:|---:|---|
| Capacity | 120 | 360 | How do quotas, session count, and tunnel reuse affect delivered records? |
| Composition | 81 | 243 | How does the control-to-payload ratio change the accepted-path byte share? |
| Rate/window | 48 | 48 | What happens when request arrivals approach a quota-window boundary? |
| Reconnect | 20 | 20 | Does reopening a connection differ from changing the session's source? |

In the composition grid, split routing completes under login-only checks and fails under every-request checks or source binding. Clean rotating routes work under every-request checks but fail under binding. Keeping the source fixed allows reconnects under binding.

Capacity and composition outcomes match the specified mechanisms. Five timing configurations differ from an ideal schedule; replay using measured origin arrivals accounts for those differences. One reconnect attempt was excluded because the client and origin disagreed about response completion, then successfully retried. These deviations and exclusions are documented in [RESULTS.md](RESULTS.md).

Smoke and pilot runs are separate deployment evidence. The proposed 1,020-run full baseline was not executed in the reported study, and [the protocol](preregistration.md) remains a draft.

## Run your own experiments

Follow [RUN-LAB.md](RUN-LAB.md) for configuration generation, IPv6 routing, certificates, origin startup, smoke checks, experiment execution, recovery, and teardown.

The runtime uses Python 3.11+ and the standard library. A deployment needs two IPv6-capable Linux servers, with a usable source /64 on the proxy server and a working return path. Preparation and local TLS tests use `cryptography`; optional plots use `matplotlib`.

To install development dependencies and run the local tests:

```sh
python -m pip install -r requirements-dev.txt
python -B -m unittest connect_lab.test_lab connect_lab.test_system connect_lab.test_sensitivity connect_lab.test_temporal -q
```

For optional plots:

```sh
python -m pip install -r requirements-plots.txt
```

The local socket tests use loopback connections. Check source routing separately on the deployment before collecting experimental data. The proxy accepts only configured destinations, and the reported experiments use owned endpoints.

## Repository guide

| Path | Contents |
|---|---|
| [connect_lab/](connect_lab/) | Origin, proxy, driver, campaign runner, report generators, and tests. |
| [inputs/](inputs/) | Frozen price inputs and provenance for preparation and cost scenarios. |
| [published/](published/) | Anonymized plans, delivery tables, aggregates, temporal timelines, and integrity manifest. |
| [reproduce.py](reproduce.py) | Offline verification and summary generation from bundled reports. |
| [RESULTS.md](RESULTS.md) | Findings, deviations, exclusions, and interpretation limits. |
| [RUN-LAB.md](RUN-LAB.md) | Full deployment and operational instructions. |
| [SENSITIVITY.md](SENSITIVITY.md) | Capacity and payload-composition sweeps. |
| [TEMPORAL.md](TEMPORAL.md) | Request pacing, quota windows, reconnects, and timeline reports. |
| [connect_lab/README.md](connect_lab/README.md) | Component interfaces, address allocation, and logging semantics. |
| [audit_results.py](audit_results.py) | Additional auditing for operators with the private raw logs. |

Individual request logs, deployed configurations, credentials, and infrastructure identifiers are excluded from the public reports. The published data support inspection of the reported mechanisms and reproduction of the summary; raw-log auditing is a separate workflow.
