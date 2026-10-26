"""One client bills N customers on $20/month subscriptions for M months on test clocks (default 100 for 12).

Setup, one request at a time: a product and price, test clocks frozen at 1 Nov 2026 (three customers per clock,
Stripe's limit), each customer with a Visa card, each subscribed. Then every clock is advanced in rounds, two
billing intervals at a time (Stripe's most per advance), to the start of the last month and then two hours past it
so the last renewal is paid. Each round advances every clock, then reads them one at a time, round-robin, every
--poll-s seconds, until each is ready. Finally every subscription is checked: active, in its last period, with
M + 1 invoices (the first and M renewals), all paid $20. Advancing stops after --total-min (default 75). At the end,
even after an error, the clocks, and with them their customers and subscriptions, are deleted and the price and
product archived, unless --keep-clocks is given.

    python3 billing_year.py --base-url https://api.stripe.com --key-file ~/stripe.env --out year-stripe.json
    python3 billing_year.py --base-url https://api.stripe.com --key-file ~/stripe.env --customers 1 --out year-1.json
    STRIPE_SECRET_KEY=sk_test_local python3 billing_year.py --base-url http://127.0.0.1:PORT --out year-rystic.json
"""
import argparse
import http.client
import json
import time
from datetime import datetime, timezone

import client
from client import PER_CLOCK, START, need, ok

POLL_S = 2.0


def ts(y, m, h=0):
    return int(datetime(y, m, 1, h, tzinfo=timezone.utc).timestamp())


def month(k, h=0):
    """1 Nov 2026 plus k months, at hour h."""
    return ts(2026 + (10 + k) // 12, (10 + k) % 12 + 1, h)


def targets(months):
    """The advance targets: two months at a time, then two hours past the last renewal.

    A last one-month step goes straight to the two hours past it.
    """
    ks = list(range(2, months + 1, 2)) + ([months] if months % 2 else [])
    if months % 2:
        return [month(k) for k in ks[:-1]] + [month(months, 2)]
    return [month(k) for k in ks] + [month(months, 2)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", required=True)
    client.add_key_args(ap)
    ap.add_argument("--customers", type=int, default=100)
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--poll-s", type=float, default=POLL_S, help="seconds between clock reads (default: 2)")
    ap.add_argument("--total-min", type=float, default=75, help="stop advancing after this many minutes")
    ap.add_argument("--keep-clocks", action="store_true", help="leave the clocks, customers and subscriptions in place")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    c = client.Client(a.base_url, client.key_from_args(ap, a))
    t0 = time.monotonic()
    deadline = t0 + a.total_min * 60
    n_cust, months = a.customers, a.months
    out = {"customers": n_cust, "months": months, "clocks": -(-n_cust // PER_CLOCK), "poll_s": a.poll_s, "rounds": []}
    prod = price = None
    clocks = []

    def save():
        out["requests"], out["r429"] = c.requests, c.r429
        out["elapsed_s"] = round(time.monotonic() - t0, 1)
        with open(a.out, "w") as f:
            json.dump(out, f, indent=1)

    try:
        prod = need(*c.call("POST", "/v1/products", {"name": "Billing year"}), "product")
        price = need(*c.call("POST", "/v1/prices", {"product": prod["id"], "unit_amount": 2000, "currency": "usd",
                                                    "recurring[interval]": "month"}), "price")
        for n in range(out["clocks"]):
            clocks.append(need(*c.call("POST", "/v1/test_helpers/test_clocks",
                                       {"frozen_time": START, "name": "year-%02d" % n}), "clock")["id"])
        custs = [need(*c.call("POST", "/v1/customers", {
            "email": "year-%03d@example.com" % n, "test_clock": clocks[n // PER_CLOCK], "payment_method": "pm_card_visa",
            "invoice_settings[default_payment_method]": "pm_card_visa"}), "customer") for n in range(n_cust)]
        subs = [need(*c.call("POST", "/v1/subscriptions", {"customer": cu["id"], "items[0][price]": price["id"]}),
                     "subscription") for cu in custs]
        out["setup_s"] = round(time.monotonic() - t0, 1)
        save()
        print("setup done in %.1f s" % out["setup_s"], flush=True)

        for target in targets(months):
            if time.monotonic() > deadline:
                out["stopped"] = "deadline before the advance to %s" % datetime.fromtimestamp(target, timezone.utc).date()
                break
            r0 = time.monotonic()
            advanced, errs = [], []
            for k in clocks:
                s, body = c.call("POST", "/v1/test_helpers/test_clocks/%s/advance" % k, {"frozen_time": target})
                if ok(s):
                    advanced.append(k)
                else:
                    errs.append("%s %s" % (s, (body.get("error") or {}).get("message", "")[:120]))
            sent = time.monotonic() - r0
            pending = list(advanced)  # a clock whose advance was refused never reaches the target
            while pending and time.monotonic() < deadline:
                s, ck = c.call("GET", "/v1/test_helpers/test_clocks/%s" % pending[0])
                if s == 200 and ck.get("status") == "ready" and ck.get("frozen_time") == target:
                    pending.pop(0)
                    continue
                pending.append(pending.pop(0))
                time.sleep(a.poll_s)
            rnd = {"to": datetime.fromtimestamp(target, timezone.utc).strftime("%Y-%m-%d %H:%M"),
                   "advances_sent_s": round(sent, 1), "all_ready_s": round(time.monotonic() - r0, 1),
                   "clocks_ready": len(advanced) - len(pending), "advance_errors": errs[:3],
                   "advance_error_count": len(errs)}
            out["rounds"].append(rnd)
            save()
            print(json.dumps(rnd), flush=True)
            if pending or errs:
                out["stopped"] = "the advance to %s did not complete" % rnd["to"]
                break

        if not out.get("stopped"):
            good, bad = 0, []
            for s in subs:
                _, sub = c.call("GET", "/v1/subscriptions/%s" % s["id"])
                _, invs = c.call("GET", "/v1/invoices?subscription=%s&limit=100" % s["id"])
                rows = invs.get("data") or []
                right = (sub.get("status") == "active" and sub["items"]["data"][0]["current_period_start"] == month(months)
                         and len(rows) == months + 1
                         and all(x.get("status") == "paid" and x.get("amount_paid") == 2000 for x in rows))
                good += right
                if not right and len(bad) < 3:
                    bad.append({"invoices": len(rows), "statuses": sorted({x.get("status") for x in rows}),
                                "period_start": sub["items"]["data"][0]["current_period_start"]})
            out["subscriptions_with_a_full_year_paid"], out["examples_wrong"] = good, bad
        out["total_s"] = round(time.monotonic() - t0, 1)
    except Exception as e:  # record where the run stopped, then still clean up
        out["error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
    finally:
        if not a.keep_clocks:
            out["clocks_deleted"], out["clocks_not_deleted"] = client.delete_clocks(c, clocks)
            for path in ([] if price is None else ["/v1/prices/" + price["id"]]) + (
                    [] if prod is None else ["/v1/products/" + prod["id"]]):
                try:
                    c.call("POST", path, {"active": "false"})
                except (http.client.HTTPException, OSError):
                    pass
        save()
        print(json.dumps({k: v for k, v in out.items() if k != "rounds"}), flush=True)


if __name__ == "__main__":
    main()
