# Stripe: Rystic's Stripe simulator and stripe-mock, refereed by Stripe

The same API calls go to three servers:

- **Stripe**, in test mode, in a fresh sandbox (`https://api.stripe.com`). Stripe is the referee: each other server's answers are scored against Stripe's.
- **Rystic's Stripe simulator**, `stripe-twin`, run locally with the `rystic` CLI.
- **[stripe-mock](https://github.com/stripe/stripe-mock)**, Stripe's open-source mock server.

There are three scenarios: six single calls, a billing flow (7 steps, on a test clock) and a payments flow (9 steps). A stress test then runs the billing flow many times at once, for many customers and over a year; see [Stress test](#stress-test-recorded-24-oct-2026). For each step the harness records the observable answer: status codes, chosen fields, and the events the flow caused. `compare_payments.py` then compares that answer field by field with Stripe's. Timings, recorded request lines and object ids are never compared. The scenarios are listed step by step in [`harness/README.md`](harness/README.md).

## Pinned versions

| Server | Version |
|---|---|
| Stripe | Test mode, in a fresh, empty sandbox |
| Rystic stripe-twin | **v0.0.18** (released 20 Oct 2026), plus v0.0.17 for comparison, run with the `rystic` CLI 0.0.24 |
| stripe-mock | **v0.206.0**, image `stripe/stripe-mock:v0.206.0@sha256:aeb96b83e87a893d2ac005e363ac01ffa6e59a5ad85b49c155dfc9129de02e78` |

Every call sends `Stripe-Version: 2026-06-24.dahlia`.

## Results, recorded 20 Oct 2026

Recorded in one session on one machine, between 9:10:38 and 9:11:05 AM PDT. Each server got all three scenarios in order: six calls, then billing, then payments. Stripe got them while the sandbox was still empty.

| Scenario | stripe-twin v0.0.18 | stripe-twin v0.0.17 | stripe-mock v0.206.0 |
|---|---|---|---|
| Six single calls: calls matching Stripe | **6 of 6** | 6 of 6 | 0 of 6 |
| Billing flow: steps matching Stripe | **7 of 7** | 6 of 7 | 4 of 7 |
| Billing flow: Stripe's 21 event types sent | **21 of 21** | 19 of 21 | 0 of 21 |
| Payments flow: steps matching Stripe | **8 of 9** | 8 of 9 | 1 of 9 |
| Payments flow: Stripe's 11 event types sent | **10 of 11** | 10 of 11 | 0 of 11 (not run) |

Wall time per flow:

| Flow | Stripe | v0.0.18 | v0.0.17 | stripe-mock |
|---|---|---|---|---|
| Billing | 14.09 s | 2.03 s | 2.04 s | 0.02 s |
| Payments | 8.35 s | 0.02 s | 0.02 s | 0.01 s |

Where the servers differ from Stripe:

- **Six single calls, stripe-mock:** it differs on every call. Retrieving the customer it just created returns no email. The list returns 1 item with `has_more: false`, where Stripe returns 3 with `has_more: true`. The declined card's charge succeeds. Reusing an idempotency key with a different body creates a second customer instead of returning `idempotency_error`. The deleted customer isn't marked `deleted`. A customer that was never created comes back 200 instead of 404.
- **Billing flow, v0.0.17:** it misses the `invoice.upcoming` and `invoice_payment.paid` events. v0.0.18 sends both.
- **Billing flow, stripe-mock:** it differs on step 4 (the first invoice's billing reason, status and amount paid, and the subscription period) and on step 6 (no renewal invoices, and the period doesn't move). It sends no events.
- **Payments flow, both twins:** the only step that differs is step 9, the events. Both twins send every one of Stripe's event types, each with Stripe's count, except `charge.updated` (see the caveats). Step 3 matches, including the balance transaction: amount 5000, fee 175, net 4825, pending.
- **Payments flow, stripe-mock:** it stops at step 2. The PaymentIntent comes back `requires_payment_method` with `amount_received: 0` and no charge, and the replay returns a different id, so steps 3 to 9 don't run.

The payments event-type count comes from `compare_payments.compare_events` applied to step 9, the same rule the billing flow's verdict uses. `compare_payments.py` scores that payments step field by field and doesn't print the count. The per-step verdicts are in `results/2026-10-20/{receipts,flow,payments}.json`, and the numbers above are in `results/2026-10-20/headline.json`.

## Startup and per-call latency

Recorded 9:13 to 9:15 AM PDT on 20 Oct 2026, on an Apple M5 with 32 GB RAM, macOS 26.6 and Docker 29.8.1. The load average was about 3.5 from other processes on the machine.

**Method.**

- **Startup:** `startup.py` runs 10 trials per target. Each trial starts a fresh server, polls `GET /v1/customers?limit=1` every 2 ms until it answers 200, records the elapsed time, then stops the server.
- **Latency:** `latency.sh` runs `bench.go` against each target, three times over. The bench uses one keep-alive connection and sends sequential calls: 500 warmup and 5,000 measured `GET /v1/customers/{id}`, then the same for `POST /v1/customers`.
- **Targets:**
  - stripe-mock, as its Docker image and as a native binary (`go install github.com/stripe/stripe-mock@v0.206.0`, Go 1.27.1);
  - stripe-twin through `rystic run -d`;
  - the bare stripe-twin binary, built from Rystic's source at each release commit and run directly, without the CLI.

Stripe itself isn't measured here, because the method covers local servers only.

Startup, process start to first 200, median of 10 trials:

| Target | ms |
|---|---|
| stripe-mock, Docker image | 242.9 |
| stripe-mock, native binary | 145.7 |
| stripe-twin v0.0.18 via `rystic run -d` | 239.7 |
| stripe-twin v0.0.17 via `rystic run -d` | 241.4 |
| stripe-twin v0.0.18, bare binary | 14.5 |
| stripe-twin v0.0.17, bare binary | 14.5 |

Per-call latency, median / p95 in ms, for runs 1, 2 and 3 (5,000 calls each):

| Target | GET a customer | POST a customer |
|---|---|---|
| stripe-mock, Docker | 0.162/0.193, 0.172/0.313, 0.163/0.203 | 0.162/0.192, 0.163/0.197, 0.161/0.197 |
| stripe-mock, native | 0.055/0.067, 0.060/0.073, 0.057/0.078 | 0.056/0.069, 0.056/0.069, 0.058/0.076 |
| v0.0.18 via `rystic run` | 0.057/0.070, 0.057/0.067, 0.058/0.070 | 0.096/0.133, 0.097/0.133, 0.097/0.137 |
| v0.0.17 via `rystic run` | 0.058/0.070, 0.058/0.071, 0.058/0.072 | 0.097/0.129, 0.096/0.131, 0.096/0.131 |
| v0.0.18, bare binary | 0.057/0.071, 0.058/0.074, 0.058/0.075 | 0.095/0.123, 0.096/0.115, 0.095/0.128 |
| v0.0.17, bare binary | 0.059/0.074, 0.058/0.073, 0.060/0.086 | 0.095/0.132, 0.095/0.117, 0.095/0.120 |

What the numbers show:

- **GETs tie** at about 0.057 ms across the four native targets.
- **stripe-mock leads on POSTs:** native stripe-mock takes 0.056 ms and stripe-twin 0.095 to 0.097 ms, a gap of about 0.04 ms a call.
- **stripe-mock in Docker** takes about 0.16 ms a call.
- **Startup:** `rystic run` takes about 240 ms, about the same as stripe-mock's Docker image. The bare simulator takes about 15 ms.
- **v0.0.17 and v0.0.18 can't be told apart** on startup or latency.

The first startup trial of each fresh bare build (775.4 ms and 411.3 ms) includes macOS's first-run checks of a newly built binary; the median is unaffected. `home-turf.json` also carries an earlier session from 17 Oct 2026 on the same machine, with stripe-twin v0.0.14. Every target ran slower that day, stripe-mock included, so read the two as separate sessions, not as a change in either tool.

## Stress test, recorded 24 Oct 2026

The billing flow run many times at once, for many customers and over a year, against Stripe test mode and stripe-twin v0.0.18 (`rystic` CLI 0.0.24), on the same machine as above. The scripts are in [`stress/`](stress/README.md), with the command behind each table; the runs are in `results/2026-10-24/`. stripe-mock is left out: it renews nothing, so there is nothing to time. Every Stripe number is one run, on one Dashboard sandbox with no other work in progress, between 11:00 AM and 4:45 PM PDT.

### Many clients, one customer each

K copies of the billing flow start at once against one server (`stress/concurrent_flows.py`), each scored against Stripe's 20 Oct run. On Stripe, each 429 is sent again up to 10 times. On stripe-twin, the clock is checked every 50 ms, and the instance is reset before each level.

| Clients | Stripe: flows matching all 7 steps | Stripe wall time | Stripe clock wait, median / max | stripe-twin: flows matching | stripe-twin wall time |
|---|---|---|---|---|---|
| 1 | 1/1 | 19.0 s | 10.4 s | 1/1 | 0.1 s |
| 10 | 10/10 | 42.3 s | 23.2 / 33.9 s | 10/10 | 0.3 s |
| 50 | 50/50, after 572 retries | 127.8 s | 63.2 / 112.1 s | 50/50 | 4.0 s on one instance; 0.4 s with one instance per client |

Files: `multiple-clients/stripe-billing-1-5-10-25-50.json` (the 1 and 10 rows, plus 5 and 25), `multiple-clients/stripe-billing-1-50.json` (the 50 row), `multiple-clients/rystic-billing-shared-{1,10,50}.json` and `multiple-clients/rystic-billing-own-instance-50.json`.

### A year of billing

One client, one request at a time (`stress/billing_year.py`): a test clock holds at most three customers, so 100 customers need 34 clocks, and an advance moves a clock at most two billing intervals, so a year takes seven advances, each waiting for every clock.

| Step | Stripe, 1 customer | Stripe, 100 customers | stripe-twin, 1 customer | stripe-twin, 100 customers |
|---|---|---|---|---|
| Setup | 3.1 s | 269.6 s | under 0.1 s | 0.1 s |
| Seven clock advances, 1 Nov 2026 to 1 Nov 2027 02:00 | 112.7 s | 2,457.6 s | under 0.4 s | 1.5 s |
| Total, with the checks | 116.2 s | 2,763.0 s (46 min) | 0.4 s | 2.2 s |
| Subscriptions with 13 paid $20 invoices | 1/1 | 100/100 | 1/1 | 100/100 |

The clock is read every 2 s, except for stripe-twin's one-customer run, read every 50 ms: at 2 s, its seven waits make it take about 14 s. Advance times are recorded to 0.1 s each. Files: `year/`.

### Many clients, 100 customers each

K clients at once, each billing its own 100 customers for one month, one request at a time (`stress/billing100.py`). Each request is tried up to 10 times on a 429.

| | 1 client | 10 clients | 50 clients |
|---|---|---|---|
| Stripe: clients that finished | 1 of 1 | 9 of 10 | 4 of 50 |
| Stripe: clients stopped by 429s | 0 | 1, while sending its advances | 34: 21 during setup, 13 at the advance |
| Stripe: setup per client, median | 274.4 s | 274.5 s | 446.4 s |
| Stripe: renewals paid | 100 of 100 | 900 of 1,000 | 514 of 5,000 |
| Stripe: 429s / requests | 0 / 602 | 436 / 11,436 | 11,482 / 34,808 |
| Stripe: wall time | 540.1 s (9 min) | 2,100.1 s (35 min) | 3,420.5 s (57 min); clients stopped waiting for clocks at 40 min |
| stripe-twin, one instance per client | 1 of 1 in 1.0 s | 10 of 10 in 3.0 s | 50 of 50 in 20.2 s |

Every renewal the clients checked was paid, on both servers. Files: `clients-x100/`.

### After the 50-client run

The 50-client run exited at 1:45 PM PDT with 226 of its clocks still advancing; the last finished at 2:08 PM (`after-50-clients/clocks-advancing.json`). Stripe refuses to delete a clock that is advancing, with a 429, "Test clock advancement underway - cannot perform modifications": 361 of the run's 544 clock deletes were refused (`clients-x100/stripe-50-clients.json`). A one-customer billing flow started at about 1:34 PM, during the backlog, waited 900 s for its clock, which never read `ready` (`after-50-clients/probe-billing-flow.json`). On a quiet sandbox that clock takes 10 to 15 s.

### Stress caveats

- **One run each.** Each Stripe number is a single run on one sandbox on one afternoon. Stripe processes an account's clocks together, so the times depend on whatever else the account is doing.
- **The 1 and 10 rows of the first table come from an earlier run of the events step.** That run read only the account's newest 1,000 events, as the harness did before this change. Up to 25 clients it made no difference, and every flow matched; at 50, the other flows' events pushed 43 flows' own events out of that window. The 50 row comes from a second run with the current events step.
- **Clock checks.** A check interval adds up to one interval to each clock wait. That matters for the simulator, which finishes an advance in milliseconds, and little for Stripe's waits of 10 s and more.
- **One shared simulator instance slows with many clients.** Its clock stands still unless moved, so every flow's objects get the same creation time, and each flow's events step pages through every flow's events. One instance per client avoids it.
- **A validation run, 25 Oct.** In one of three one-customer, one-month runs of `billing_year.py` on Stripe, the renewal invoice still read `draft` just after the clock reported `ready`; the other two read it paid. Every recorded run read every renewal paid.

## Caveats

- **The payments `charge.updated` gap.** Stripe attaches the charge's balance transaction about 3 s after the confirm (2.7 s in this run), and it sends one `charge.updated` when it does. Both twins attach the balance transaction at once and send no `charge.updated`. So step 3 matches, but step 9 differs by that one event type. The twins score 8 of 9 steps because of it.
- **Billing wall time includes clock polling.** The harness polls the test clock once a second until it reports `ready`. Both twins reported `ready` on the second poll, so about 2 s of their 2.03 s and 2.04 s is the harness waiting. On Stripe, 9.8 s of the 14.09 s was the clock advance (8 polls). stripe-mock answered `ready` at once.
- **Startup and latency are one machine's session.** They come from one run of 10 trials and three latency runs, on a laptop with other load. Absolute numbers will differ on other hardware, and they moved by about a third between two sessions on this one.
- **The bare-binary rows aren't reproducible from public artifacts.** Those builds come from Rystic's source and aren't published. `rystic run` is the public route.
- **stripe-mock is stateless by design.** Its own README says so: it validates requests against Stripe's OpenAPI spec and answers with fixture data. The stateful scenarios here test something it doesn't attempt.
- **Stripe's answers can change.** The Stripe column is what Stripe's test mode answered on 20 Oct 2026 with `Stripe-Version: 2026-06-24.dahlia`.
- **The harness changed in two respects since the recording.** The published scripts read the key from `STRIPE_SECRET_KEY` or `--key-file`; the recorded local runs passed a placeholder key on the command line with a flag the public copy no longer has. And each flow's events step now reads every event created since the flow's first object, where it read the account's newest 1,000, so flows running at the same time in one account can't push each other's events out (see the stress test). For one flow in a fresh sandbox both read the same events. `run_flow.py` also takes `--poll-s`, the seconds between clock checks; the default is the 1 s the recording used.

## Reproduce

### 1. Get a Stripe sandbox test key

1. In the Stripe Dashboard, open the account menu and choose **Switch to sandbox**, then **Create sandbox**.
2. Choose **"Create an account from scratch"**, so the sandbox starts empty.
3. In the new sandbox, copy the **secret key**. It starts with `sk_test_`.

Use test mode only: the harness refuses live keys. Keep the key in a dotenv-style file outside version control, e.g. `~/stripe.env` holding one line, `STRIPE_SECRET_KEY=sk_test_…`. The scenarios create and delete test-mode objects in that sandbox.

### 2. Start the local servers

Rystic's Stripe simulator:

    curl -fsSL https://www.rystic.ai/install.sh | sh          # the rystic CLI
    rystic run -version 0.0.18 stripe-twin                    # serves on http://localhost:8080

`rystic run` fetches the version if it isn't installed yet. The recorded run used `rystic run -version 0.0.18 -d --port 0 -name v18 stripe-twin`, which detaches and prints the URL it serves on; `rystic stop v18` ends it. For v0.0.17 alongside it, pass `-version 0.0.17` with its own `--port` and `-name`.

stripe-mock, pinned by digest:

    docker run --rm -p 12111:12111 stripe/stripe-mock:v0.206.0@sha256:aeb96b83e87a893d2ac005e363ac01ffa6e59a5ad85b49c155dfc9129de02e78

Start each server fresh, so it has no state from an earlier run.

### 3. Run the scenarios

From `harness/`, run all three scenarios against each server:

    cd harness

    # Stripe, test mode: the key comes from the file
    python3 run_receipts.py --base-url https://api.stripe.com --key-file ~/stripe.env --label "Stripe test mode" --out receipts-stripe.json
    python3 run_flow.py     --base-url https://api.stripe.com --key-file ~/stripe.env --label "Stripe test mode" --out flow-stripe.json
    python3 run_payments.py --base-url https://api.stripe.com --key-file ~/stripe.env --label "Stripe test mode" --out payments-stripe.json

    # Local servers accept any test-shaped placeholder key
    export STRIPE_SECRET_KEY=sk_test_receipts
    for s in receipts flow payments; do
      python3 run_$s.py --base-url http://localhost:8080  --label "Rystic stripe-twin v0.0.18" --out $s-v0.0.18.json
      python3 run_$s.py --base-url http://localhost:12111 --label "stripe-mock v0.206.0"       --out $s-mock.json
    done

Each script prints `wrote <file>`. A Stripe run takes about 30 s in all, most of it the test clock and the balance transaction.

### 4. Compare

    python3 compare_payments.py receipts-stripe.json receipts-v0.0.18.json receipts-mock.json --out receipts.json
    python3 compare_payments.py flow-stripe.json     flow-v0.0.18.json     flow-mock.json     --out flow.json
    python3 compare_payments.py payments-stripe.json payments-v0.0.18.json payments-mock.json --out payments.json

Each prints a table of per-step verdicts, ending with how many steps each server agreed with Stripe on. To re-score the recorded runs without a key:

    R=../results/2026-10-20/runs
    python3 compare_payments.py $R/flow-stripe.json $R/flow-v0.0.18.json $R/flow-v0.0.17.json $R/flow-mock.json

### 5. Startup and latency

`results/2026-10-20/home-turf/` holds the `startup.py`, `latency.sh` and `bench.go` used. `startup.py --targets mock-docker,mock-native,rystic-run-0.0.18,rystic-run-0.0.17` needs Docker, the `rystic` CLI and a native `stripe-mock` binary next to the script. `latency.sh` also needs `bench` (`go build bench.go`) there.

### 6. Stress test

[`stress/README.md`](stress/README.md) gives the command behind each stress-test table. Run each Stripe test on a sandbox with no other work in progress: the 50-client runs take about an hour and leave clocks advancing for up to half an hour after they end.

## What's here

- **`harness/`:** the scenario scripts, the comparison script, and their tests with synthetic fixtures. See [`harness/README.md`](harness/README.md).
- **`stress/`:** the stress-test scripts and their tests. See [`stress/README.md`](stress/README.md).
- **`results/2026-10-24/`:** the stress-test runs, one folder per table above: `multiple-clients/`, `year/`, `clients-x100/` and `after-50-clients/`. They hold no object ids, keys or account ids.
- **`results/2026-10-20/`:**
  - `receipts.json`, `flow.json`, `payments.json`: the merged comparisons from `compare_payments.py`, with per-step verdicts and the four runs.
  - `headline.json`: the headline numbers per scenario and server, including the payments event-type count.
  - `runs/<scenario>-<server>.json`: the 12 individual runs.
  - `home-turf.json`: startup and latency, with method, machine and binaries, plus the 17 Oct session.
  - `home-turf/`: `startup-results.json`, `run1/` to `run3/` latency outputs, and the scripts used.

Object ids in the recorded request lines are masked to their prefix (`cus_…`). The runs hold no keys, account ids or request ids, and every email address is an `example.com` one. The sandbox's name is withheld.
