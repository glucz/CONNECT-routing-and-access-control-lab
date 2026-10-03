# Paper I lab: pre-registration

Status: **DRAFT, awaiting author approval. Freezes on approval; no run before that.**
Written 2026-10-02. Internal document: it names no server address, domain or service, and none may be
added. The paper cites this file as "the pre-registration" and reproduces Sections 1 to 8.

Design decisions confirmed by the author on 2026-10-02: no VPN forwarder (exit tiers are emulated
by proxies that send from IPv6 addresses we assign), W-video sessions of 1 GB with about 200 GB of
total lab traffic, k = 30 runs per cell, and the price mapping in Section 6.

## 1. Question and what the lab can establish

The lab measures whether a supplier's routing policy **delivers** a workload under an access check
whose rule we set, and what each delivered workload costs at listed prices. It is a reproducible
mechanism test on infrastructure we own. It establishes:

- which routing policies deliver a session under each of three check structures (check at login
  only, check every request, bind the session to the login exit);
- the share `w` of a delivered session's bytes that had to leave through a `clean` exit, by case;
- how many exit addresses a delivered job consumes under a per-address request limit we set;
- how many distinct source addresses one session presents under rotating and held exits;
- the cost of each delivered session or job at listed prices.

It does **not** establish:

- what any real service checks: the three check structures mirror documented behaviour, which the
  paper cites separately;
- that a real residential address passes a real reputation check: `clean` and `dirty` are labels we
  assign to our own addresses, and the gap between real residential and datacenter addresses is cited
  literature, not measured here;
- a real value of `w` or of usable capacity `Y`. Under a check of the bulk, `w = 1` by construction;
  under a login-only check, `w` equals our synthetic control bytes over session bytes. The lab
  confirms **which case applies**. The 1.4% control share from published traces stays a cited
  scenario value for the login-only case. Under the rate rule, `Y` is the limit we set; the lab
  confirms that delivery then needs about `R/Y` held addresses (exactly `ceil(R/(Y - 1))` when
  each address also carries its session's login); it does not estimate `Y`;
- any price a provider actually charges: prices are listed prices from our dated dataset.

Because the check rules and routes are deterministic, most cells are expected at 0/30 or 30/30. The
test is that the full stack (HTTPS, proxy CONNECT tunnels, connection reuse, separate hostnames per
request class, held and rotating exits) produces exactly the predicted outcome. Any deviation is a
finding and is reported, not repaired after the fact.

## 2. Components (all owned)

- **Origin (the defender)** on one server: an HTTPS service with three request classes on three
  hostnames: `login` (issues a signed session cookie that records the login source address),
  `manifest` (10,000 B), and `seg` (segments of 1,000,000 B; scraping records of 26,227 B). Every
  non-login request needs a valid session cookie, under every policy. TLS uses a private lab CA.
  Inbound HTTPS is accepted only from the other server's address block.
- **Exit proxies (the supplier's tiers)** on the other server: HTTP CONNECT proxies that send each
  tunnel from an IPv6 source address chosen from that server's /64. The class is encoded in the
  address (`clean` and `dirty` sub-blocks), so the origin reads it without a shared table. The
  client names its identity in the proxy username, as commercial pools do with session tokens.
  - **stick** mode: the source address is a hash of the identity, so one identity holds one address;
  - **rot** mode: the source address is a hash of the identity and a per-identity tunnel counter, so
    every new tunnel leaves from a new address.

  The proxies listen on loopback only and refuse any destination other than the three lab hostnames.
- **Clients** on the proxy server: scripted HTTPS clients that open one tunnel per hostname, reuse a
  tunnel for at most `M` requests, then open a new one (Section 4).

## 3. Exit sets and routing policies

| Exit set | Class | Mode |
|---|---|---|
| `clean-stick` | clean | stick |
| `clean-rot` | clean | rot |
| `dirty-stick` | dirty | stick |

| Routing policy | login | manifest | seg |
|---|---|---|---|
| R-dirty-stick | dirty-stick | dirty-stick | dirty-stick |
| R-clean-rot | clean-rot | clean-rot | clean-rot |
| R-split | clean-stick | clean-stick | dirty-stick |
| R-clean-stick | clean-stick | clean-stick | clean-stick |

Within one session every request uses the same identity, so under `clean-stick` login, manifest and
segments leave from one address. (The handoff's R-split-sticky routes exactly like R-clean-stick and
is dropped. Its economic difference, a held address billed per month rather than per GB, is covered
in Section 6 by costing R-clean-stick both ways.)

## 4. Defender policies and workloads

Defender policies (the class requirement is on in every cell: a checked request must come from a
`clean` address):

- **D-login:** only the login request is checked.
- **D-every:** every request is checked on its own; there is no link between requests.
- **D-bind:** the login request is checked, and every later request of the session must come from
  the address that passed login.

A denial is HTTP 403 with a reason (`login_denied`, `dirty_exit`, `bind_mismatch`, `rate`,
`no_session`).

Workloads:

- **W-video:** 1 login, 1 manifest, then N = 1,000 segments of 1,000,000 B in order (1.0 GB per
  session). Tunnel reuse M = 100 requests. The client stops at the first non-200 response. Per-request
  timeout 30 s.
- **W-scrape:** 1 login, then R = 1,000 records of 26,227 B (the paper's scraping-page wire size,
  `calibration_inputs.json` `scenarios.scrape_page_wire_bytes`). Tunnel reuse M = 25. The client does
  not stop on a denial and does not retry.

Each run uses a fresh identity derived from its run id, so no address is reused across runs.

## 5. Run blocks, predictions and hypotheses

**Block 1, main matrix:** both workloads x 4 routing policies x 3 defender policies = 24 cells, k = 30
runs each, rate rule off. Predicted outcomes (the same for both workloads):

| routing / defender | D-login | D-every | D-bind |
|---|---|---|---|
| R-dirty-stick | fail (login) | fail (login) | fail (login) |
| R-clean-rot | pass | pass | fail (bind_mismatch at the manifest) |
| R-split | pass | fail (dirty_exit at the first segment) | fail (bind_mismatch at the first segment) |
| R-clean-stick | pass | pass | pass |

**Block 2, rate rule (W-scrape only):** the defender caps each source address at L requests per
3,600 s window (all request classes count). L in {50, 200}. Cells, k = 30 each:

| defender | routing | sessions per job S | predicted |
|---|---|---|---|
| D-every + rate | R-clean-rot | 1 | pass; about R/M + 1 = 41 addresses used |
| D-every + rate | R-clean-stick | 1 | fail (rate after L requests) |
| D-every + rate | R-clean-stick | ceil(R/(L - 1)) | pass; S held addresses |
| D-bind + rate | R-clean-rot | 1 | fail (bind_mismatch) |
| D-bind + rate | R-clean-stick | ceil(R/(L - 1)) | pass; S held addresses |

With S > 1, a job splits its R records evenly over S sessions, each with its own identity and its own
login. Each login counts toward its address's limit, so S sessions need R + S <= S L, that is
S = ceil(R/(L - 1)): S = 21 for L = 50 and S = 6 for L = 200.

**Hypotheses (directional, confirmatory):**

- **H1 (route compatibility).** Under D-every and D-bind, R-split fails; under D-bind, R-clean-rot
  fails. R-clean-stick passes under all three. Tested as one-sided success-rate differences between
  the named cells, Fisher exact test, alpha = 0.05.
- **H2 (mixed source addresses).** Under R-clean-rot, a delivered W-video session arrives from more
  than one source address (predicted: 1 for login, 1 for the manifest, ceil(N/M) = 10 for the
  segments); under R-clean-stick, from exactly one.
- **H3 (cost conditioned on delivery).** The cheapest delivered W-video session is R-split under
  D-login. Once the defender checks the bulk (D-every, D-bind), the cheapest delivered session is the
  all-clean route, whose cost is the full session volume at the clean price, or one held address.
- **H4 (capacity under binding).** Under the rate rule, a rotating clean pool delivers the job
  without binding and fails with binding; with binding, delivery needs S = ceil(R/(L - 1)) held addresses.

## 6. Prices (applied after the runs; the runs do not depend on them)

All from Paper I's dated dataset (`analysis/price_series/posted_prices.csv`, observed 2026-09-29).
`env.json` records the rows used.

- **clean, per GB:** 1.45 USD/GB, the median residential price at the largest self-serve tier,
  n = 13 (`calibration_inputs.json` `prices.residential_largest_tier_usd_per_gb`).
- **dirty, per GB, two values reported side by side:**
  - rented datacenter proxy: 0.38 USD/GB, the median over providers of the cheapest current per-GB
    datacenter product, n = 5 (computed by `tools/analyze_lab.py` with this rule);
  - own server path `c_0`: 2.76e-5 to 0.12 USD/GB (`calibration_inputs.json` `c0_usd_per_gb`).
- **clean held address, per month** (the second costing of R-clean-stick and Block 2): 1.25 USD per
  address-month, the median ISP-registered static price at the largest plan, n = 8
  (`calibration_inputs.json` `prices.isp_usd_per_ip_month_largest_plan`).

Cost per delivered run = bytes per class of exit, counted at the proxies, times the per-GB price; or
addresses used times the per-address price. Bytes are application payload through the tunnel (TLS
included, CONNECT exchange excluded).

## 7. Outcomes and analysis

Primary outcomes, per cell: success rate with a Wilson 95% interval; `w` (clean-exit byte share among
successful runs); distinct source addresses per session, by class; addresses used per job (Block 2);
cost per successful run under each price in Section 6.

Success: **W-video**, all 1,000 segments return 200, in order, each within the timeout. **W-scrape**,
at least 99% of records return 200.

Bytes come from two independent counts: the proxies' per-tunnel byte counters and the origin's
per-request log. The client's own counts are the third view. A discrepancy above 1% between proxy and
origin payload for any cell is reported.

Analysis: `tools/analyze_lab.py` reads the three logs and writes `analysis/lab_results.json`
(aggregates only, no addresses or identifiers). There are no post-hoc cells. Exploratory analyses, if
any, are labelled as such.

## 8. Procedure, scope and stopping rules

- Order: smoke test (each exit set, the class read at the origin, HTTPS, each denial reason), then
  Blocks 1 and 2 with cell order randomised under a fixed seed recorded in `env.json`. One defender
  policy per cell, set before its first run; rate counters reset between cells.
- **Scope:** only our own endpoints. Every byte goes to the lab origin; the proxies refuse any other
  destination. No real service is contacted, no third-party provider is used, nothing is bought.
- **Traffic budget:** expected about 195 GB of payload (Block 1: about 180 GB of W-video and 5 GB of
  W-scrape; Block 2: under 7 GB). Hard stop at 230 GB counted at the proxies; if it trips, the run
  stops and the incomplete cells are reported as incomplete.
- **Infrastructure failure** (a server down, a disk full, a proxy crash) invalidates the affected
  runs. They are rerun once, logged with the reason, and reported. A denial by the defender is an
  outcome, never a failure to rerun.
