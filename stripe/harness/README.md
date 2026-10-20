# Harness: Stripe scenarios for any Stripe-shaped server

Four Python 3 scripts, standard library only. Each run script sends one scenario to one server and writes what came back as JSON. `compare_payments.py` then scores every server's run against Stripe's.

| Script | Scenario |
|---|---|
| `run_receipts.py` | Six single calls (below) |
| `run_flow.py` | Billing flow, 7 steps: a product and a $20/month price, a test clock at 1 Nov 2026, a customer on the clock with a card, a subscription, the clock advanced past the first renewal, the subscription and its invoices read back, and the events the flow caused |
| `run_payments.py` | Payments flow, 9 steps: a customer pays $50 through a PaymentIntent (and the request is replayed with the same `Idempotency-Key`), the charge and its balance transaction are read, $20 is refunded, then the rest, then a third refund is tried, a second payment is declined, and the customer's charges and the flow's events are listed |
| `compare_payments.py` | Compares runs of one script against the Stripe run, step by step, prints a table and writes the merged JSON |

The six single calls:

1. Create a customer, then retrieve it: status and `email`.
2. Create four more, then `GET /v1/customers?limit=3`: status, item count, `has_more`.
3. Charge `tok_chargeDeclined`: status and `error.code` / `decline_code`, or the charge `status`.
4. Reuse one `Idempotency-Key` with a different email: status and `error.type`, or the returned email.
5. Delete the first customer, then retrieve it: status, `deleted`, `email`.
6. Retrieve `cus_doesnotexist`, a customer never created: status and `error.code`, or the returned `id`.

## The key

Each run script takes the key from `--key-file <path>`, else from the `STRIPE_SECRET_KEY` environment variable. A key file is dotenv-style: the scripts read `STRIPE_SECRET_KEY` from it, or else the first `sk_test_`/`rk_test_` value. Only test-mode keys (`sk_test_`, `rk_test_`) are accepted, and a live key is refused. There is no `--key` flag, so a key never goes on a command line. The key is never printed or recorded, and errors name the file, never a value in it.

Local servers accept any test-shaped placeholder, such as `sk_test_receipts`.

## Run

    # Local servers
    STRIPE_SECRET_KEY=sk_test_receipts python3 run_receipts.py --base-url http://127.0.0.1:8080 --label "Rystic stripe-twin v0.0.18" --out receipts-v0.0.18.json
    STRIPE_SECRET_KEY=sk_test_receipts python3 run_receipts.py --base-url http://127.0.0.1:12111 --label "stripe-mock v0.206.0" --out receipts-mock.json

    # Stripe, test mode: creates and deletes test-mode objects in that account
    python3 run_receipts.py --base-url https://api.stripe.com --key-file ~/stripe.env --label "Stripe test mode" --out receipts-stripe.json

    # Score against Stripe
    python3 compare_payments.py receipts-stripe.json receipts-v0.0.18.json receipts-mock.json --out receipts.json

`run_flow.py` and `run_payments.py` take the same flags. `run_flow.py` deletes its test clock (and with it the clock's customer and subscription) at the end, even after failed steps, and archives its price and product; `--keep` leaves them in place.

Every call sends `Stripe-Version: 2026-06-24.dahlia` (change it with `--stripe-version`).

## What is recorded and compared

Each run records, per step, the observable answer (status codes and chosen fields), the request lines with object ids masked to their prefix (`cus_…`), and timings. `compare_payments.py` compares each step's answer field by field with Stripe's. It never compares timings, request lines or masked ids. In the billing flow it compares times only as offsets from the test clock, scores a step that was not run as not matching, and reports which of Stripe's event types each server sent.

## Tests

    python3 -m unittest test_compare_payments      # or: python3 -m pytest

The tests run against a small fake Stripe-shaped server and the synthetic fixtures in `testdata/`. They make no network calls beyond 127.0.0.1.
