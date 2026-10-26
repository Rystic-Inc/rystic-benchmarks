# Stress: the billing flow under load

Scripts that run the billing flow many times at once, with many customers, and over many months, against Stripe test mode or against Rystic's Stripe simulator. Python 3, standard library only. The results recorded on 24 Oct 2026, and what they show, are in [`../README.md`](../README.md#stress-test-recorded-24-oct-2026).

| Script | What it runs |
|---|---|
| `concurrent_flows.py` | K copies of the harness's billing (or payments) flow at once against one server, each scored against Stripe's recorded solo run |
| `multi_instance.py` | The same, with each copy against its own simulator instance |
| `billing_year.py` | One client billing N customers for M months on test clocks (default 100 customers, 12 months) |
| `billing100.py` | K clients at once, each billing its own 100 customers for one month |
| `watch_clocks.py` | Counts an account's test clocks by status once a minute until none is advancing; read-only |
| `client.py` | The HTTP client the scripts share: keep-alive, 429s counted by Stripe's stated reason and sent again with backoff |

## The key

As in [`../harness/`](../harness/README.md#the-key): `--key-file <path>` or `$STRIPE_SECRET_KEY`, test-mode keys only, never printed. Local servers accept any test-shaped placeholder such as `sk_test_local`.

## Retries and timeouts

- **429s.** Stripe's SDKs don't retry a `rate_limit` 429. `billing_year.py`, `billing100.py` and `watch_clocks.py` try each request up to ten times, waiting a jittered 0.25 s, 0.5 s, 1 s, 2 s, then 4 s between tries. A 429 refusing to change a clock that is still advancing isn't a rate limit, and is counted as `clock-advancing` and not sent again. Each POST carries an Idempotency-Key, kept across its tries. `concurrent_flows.py` sends nothing again unless given `--retries N`.
- **Clock waits.** `concurrent_flows.py` waits up to 900 s for a clock, not the harness's 120 s, so a slow advance is measured rather than cut off. It reports how many flows took longer than 120 s as `would_time_out_in_harness`.
- **Clock checks.** The harness checks a clock once a second. A local simulator finishes an advance within milliseconds, so a 1 s check measures mostly the wait: pass `--poll-s 0.05` to time the simulator itself. `billing_year.py` and `billing100.py` read one clock every 2 s.
- **Time limits.** `billing100.py` stops a client's setup after `--setup-min` (default 20) and stops waiting for clocks after `--total-min` (default 40), then still checks the renewals on the clocks that finished. `billing_year.py` starts no advance, and stops waiting for clocks, after `--total-min` (default 75). Both then delete, so a run can end later than its limit.
- **Wall time.** `billing100.py`'s `wall_s` ends with the last client's last check; `wall_s_with_deletes` adds the clock deletes.

## What a run leaves in the account

- `concurrent_flows.py`: each billing flow deletes its test clock, with the clock's customer and subscription, and archives its price and product, as `run_flow.py` does. A payments flow leaves its customer, payments and refunds, as `run_payments.py` does.
- `billing_year.py` and `billing100.py` delete the clocks they made at the end, even after an error, and with them the customers and subscriptions, unless given `--keep-clocks`. Products and prices are archived (`billing_year.py`) or left (`billing100.py`).
- Charges, PaymentIntents, refunds, invoices of deleted customers, events and balance transactions have no delete in Stripe's API and stay.
- A clock still advancing can't be deleted: Stripe answers 429, "Test clock advancement underway - cannot perform modifications". Both scripts count such clocks, and any delete whose connection dropped, as `clocks_not_deleted`; `watch_clocks.py` shows the advancing ones.

## Reproduce the tables

Start each simulator fresh with `rystic run -version 0.0.18 -d --port 0 -name <name> stripe-twin`; it prints the port. Stop it with `rystic stop <name>`. Against Stripe, run each test on a sandbox with no other work in progress.

**Many clients, one customer each** (one server shared by every client):

    python3 concurrent_flows.py --flow billing --levels 1,10,50 --retries 10 \
        --base-url https://api.stripe.com --key-file ~/stripe.env --out billing-stripe.json
    STRIPE_SECRET_KEY=sk_test_local python3 concurrent_flows.py --flow billing --levels 1,10,50 \
        --poll-s 0.05 --reset-between --base-url http://127.0.0.1:PORT --out billing-rystic.json

and one simulator per client, for the 50-client row:

    for i in $(seq -w 0 49); do rystic run -version 0.0.18 -name c$i -d --port 0 stripe-twin; done
    STRIPE_SECRET_KEY=sk_test_local python3 multi_instance.py --flow billing --prefix c --poll-s 0.05 --out own-50.json

**A year of billing** (100 customers; add `--customers 1` for one):

    python3 billing_year.py --base-url https://api.stripe.com --key-file ~/stripe.env --out year-stripe.json
    STRIPE_SECRET_KEY=sk_test_local python3 billing_year.py --base-url http://127.0.0.1:PORT --out year-rystic.json

The recorded one-customer simulator run used `--poll-s 0.05`; at the default 2 s, its seven clock waits make it take about 14 s.

**Many clients, 100 customers each** (1, 10 and 50 clients):

    python3 billing100.py --base-url https://api.stripe.com --key-file ~/stripe.env --clients 10 --out 10-stripe.json
    for i in $(seq -w 0 9); do rystic run -version 0.0.18 -name h$i -d --port 0 stripe-twin; done
    STRIPE_SECRET_KEY=sk_test_local python3 billing100.py --prefix h --clients 10 --out 10-rystic.json

`--prefix` gives each client its own running instance, found with `rystic ps --json`; use a prefix no other instance has.

**After the 50-client run:** start `watch_clocks.py` as `billing100.py --clients 50` ends:

    python3 watch_clocks.py --key-file ~/stripe.env --out clocks.json

**How the recorded runs differ from these commands.** The 1- and 10-client runs of `billing100.py`, and every year run except stripe-twin's 100-customer one, added `--keep-clocks`. The shared-instance rows of the first table are three runs, one level each, on a freshly reset instance, which is what `--reset-between` does in one run. The 50-client `billing100.py` run on Stripe predates the clean-up after an error ([`../README.md`](../README.md#stress-caveats)).

## Tests

    python3 -m unittest test_stress

The tests cover the advance targets, the client's retries and idempotency keys, and the clean-up after an error, against a fake server on 127.0.0.1, and check that the recorded Stripe runs used for scoring load.
