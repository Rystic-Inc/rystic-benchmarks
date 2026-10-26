"""K clients at once, each billing its own 100 customers for one month, as K CI jobs with a large fixture would.

Each client, one request at a time: a product and $20/month price, 34 test clocks at 1 Nov 2026 (three customers
per clock, Stripe's limit), 100 customers with a Visa card, 100 subscriptions; checks each first invoice; advances
its 34 clocks two hours past the renewal and reads one clock every 2 s, round-robin, until each is ready; checks
each renewal (the period moved on, and a paid subscription_cycle invoice). At the end, even after an error, it deletes
the clocks it made, and with them their customers and subscriptions, unless --keep-clocks is given; Stripe refuses
to delete a clock still advancing. Each request is tried up to ten times on a 429 (client.TRIES). The setup and the
waiting are bounded by --setup-min and --total-min; progress prints every minute. The wall time ends with the last
client.

    python3 billing100.py --base-url https://api.stripe.com --key-file ~/stripe.env --clients 10 --out 10.json
    STRIPE_SECRET_KEY=sk_test_local python3 billing100.py --prefix c --clients 50 --out own.json   # one simulator each
"""
import argparse
import json
import statistics
import threading
import time

import client
from client import PER_CLOCK, START, need

CUST, POLL_S, HOUR = 100, 2.0, 3600


class Progress:
    def __init__(self):
        self.lock = threading.Lock()
        self.n = {"customers": 0, "subscriptions": 0, "clocks_ready": 0}

    def bump(self, k):
        with self.lock:
            self.n[k] += 1


def run_client(base, key, i, setup_deadline, deadline, keep_clocks, progress):
    c = client.Client(base, key)
    t0 = time.monotonic()
    r = {"client": i, "phase": "setup"}
    clocks = []
    try:
        prod = need(*c.call("POST", "/v1/products", {"name": "Billing 100 client %02d" % i}), "product")
        price = need(*c.call("POST", "/v1/prices", {"product": prod["id"], "unit_amount": 2000,
                                                    "currency": "usd", "recurring[interval]": "month"}), "price")
        for n in range(-(-CUST // PER_CLOCK)):
            clocks.append(need(*c.call("POST", "/v1/test_helpers/test_clocks",
                                       {"frozen_time": START, "name": "c%02d-%02d" % (i, n)}), "clock")["id"])
        custs, subs = [], []
        for n in range(CUST):
            if time.monotonic() > setup_deadline:
                raise TimeoutError("setup deadline: %d customers, %d subscriptions" % (len(custs), len(subs)))
            custs.append(need(*c.call("POST", "/v1/customers", {
                "email": "c%02d-%03d@example.com" % (i, n), "test_clock": clocks[n // PER_CLOCK],
                "payment_method": "pm_card_visa", "invoice_settings[default_payment_method]": "pm_card_visa"}), "customer"))
            progress.bump("customers")
        for n in range(CUST):
            if time.monotonic() > setup_deadline:
                raise TimeoutError("setup deadline: %d customers, %d subscriptions" % (len(custs), len(subs)))
            subs.append(need(*c.call("POST", "/v1/subscriptions", {
                "customer": custs[n]["id"], "items[0][price]": price["id"], "expand[0]": "latest_invoice"}), "subscription"))
            progress.bump("subscriptions")
        r["setup_s"] = round(time.monotonic() - t0, 1)
        first_ok = 0
        for s in subs:
            it, inv = s["items"]["data"][0], s.get("latest_invoice") or {}
            first_ok += (s.get("status") == "active" and it["current_period_start"] == START
                         and it["current_period_end"] == START + 30 * 24 * HOUR
                         and inv.get("billing_reason") == "subscription_create" and inv.get("status") == "paid"
                         and inv.get("amount_paid") == 2000)
        r["first_invoice_ok"] = first_ok
        r["phase"] = "advance"
        target = subs[0]["items"]["data"][0]["current_period_end"] + 2 * HOUR
        for k in clocks:
            need(*c.call("POST", "/v1/test_helpers/test_clocks/%s/advance" % k, {"frozen_time": target}), "advance")
        r["advances_sent_s"] = round(time.monotonic() - t0, 1)
        pending, ready_at = list(clocks), {}
        while pending and time.monotonic() < deadline:
            k = pending[0]
            s, ck = c.call("GET", "/v1/test_helpers/test_clocks/%s" % k)
            if s == 200 and ck.get("status") == "ready" and ck.get("frozen_time") == target:
                ready_at[k] = round(time.monotonic() - t0, 1)
                pending.pop(0)
                progress.bump("clocks_ready")
                continue
            pending.append(pending.pop(0))
            time.sleep(POLL_S)
        r["clocks_ready"], r["clock_ready_at_s"] = len(ready_at), sorted(ready_at.values())
        r["phase"] = "check"
        renewed = 0
        for n, s in enumerate(subs):
            if clocks[n // PER_CLOCK] not in ready_at:
                continue
            _, sub = c.call("GET", "/v1/subscriptions/%s" % s["id"])
            _, invs = c.call("GET", "/v1/invoices?subscription=%s&limit=10" % s["id"])
            rows = invs.get("data") or []
            cyc = [x for x in rows if x.get("billing_reason") == "subscription_cycle"]
            renewed += (sub.get("status") == "active"
                        and sub["items"]["data"][0]["current_period_start"] == s["items"]["data"][0]["current_period_end"]
                        and len(rows) == 2 and len(cyc) == 1 and cyc[0].get("status") == "paid"
                        and cyc[0].get("amount_paid") == 2000)
        r["renewed_ok"] = renewed
        r["phase"] = "done" if not pending else "clock deadline"
        r["total_s"] = round(time.monotonic() - t0, 1)
    except Exception as e:  # record where the client stopped
        r["error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
    finally:
        if not keep_clocks:
            codes = [c.call("DELETE", "/v1/test_helpers/test_clocks/%s" % k)[0] for k in clocks]
            r["clocks_deleted"] = sum(1 for s in codes if client.ok(s))
            r["clocks_not_deleted"] = len(codes) - r["clocks_deleted"]
        r["ended_s"] = round(time.monotonic() - t0, 1)
    r["requests"], r["r429"] = c.requests, c.r429
    return r


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", help="one server shared by every client")
    ap.add_argument("--ports", help="comma-separated local simulator ports, one per client")
    ap.add_argument("--prefix", help="one client per running simulator instance whose name starts with this")
    client.add_key_args(ap)
    ap.add_argument("--clients", type=int, default=50)
    ap.add_argument("--setup-min", type=float, default=20)
    ap.add_argument("--total-min", type=float, default=40)
    ap.add_argument("--keep-clocks", action="store_true", help="leave each client's clocks, customers and subscriptions")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if sum(bool(x) for x in (a.base_url, a.ports, a.prefix)) != 1:
        ap.error("give one of --base-url, --ports or --prefix")
    key = client.key_from_args(ap, a)
    if a.base_url:
        bases = [a.base_url] * a.clients
    else:
        ports = a.ports.split(",") if a.ports else client.instance_ports(a.prefix)
        bases = ["http://127.0.0.1:%s" % p for p in ports][:a.clients]
        if len(bases) < a.clients:
            ap.error("%d clients asked for, %d simulator ports found" % (a.clients, len(bases)))
    local = not a.base_url
    t0 = time.monotonic()
    setup_deadline, deadline = t0 + a.setup_min * 60, t0 + a.total_min * 60
    res, progress = [None] * len(bases), Progress()
    go = threading.Barrier(len(bases))

    def run(i):
        go.wait()
        res[i] = run_client(bases[i], key, i, setup_deadline, deadline, a.keep_clocks, progress)

    th = [threading.Thread(target=run, args=(i,)) for i in range(len(bases))]
    [x.start() for x in th]
    timeline, tick = [], 1 if local else 60
    while True:
        until = time.monotonic() + tick
        for x in th:
            x.join(timeout=max(0.0, until - time.monotonic()))
        if not any(x.is_alive() for x in th):
            break
        snap = [round(time.monotonic() - t0)] + [progress.n[k] for k in ("customers", "subscriptions", "clocks_ready")]
        timeline.append(snap)
        if not local:
            print("t=%ds customers=%d subscriptions=%d clocks_ready=%d" % tuple(snap), flush=True)
    setups = [r["setup_s"] for r in res if "setup_s" in r]
    agg429 = {}
    for r in res:
        for k, v in (r.get("r429") or {}).items():
            agg429[k] = agg429.get(k, 0) + v
    out = {"clients": len(bases), "customers_per_client": CUST, "wall_s": max(r["ended_s"] for r in res),
           "customers_created": progress.n["customers"], "subscriptions_created": progress.n["subscriptions"],
           "clocks_total": len(bases) * -(-CUST // PER_CLOCK), "clocks_ready": progress.n["clocks_ready"],
           "first_invoices_ok": sum(r.get("first_invoice_ok", 0) for r in res),
           "renewals_ok": sum(r.get("renewed_ok", 0) for r in res),
           "clients_done": sum(1 for r in res if r.get("phase") == "done"),
           "setup_s_median": statistics.median(setups) if setups else None, "clients_set_up": len(setups),
           "setup_s_max": max(setups, default=None),
           "errors": sorted({r["error"] for r in res if "error" in r})[:5],
           "requests": sum(r.get("requests", 0) for r in res), "r429": agg429,
           "timeline_s_customers_subs_clocksready": timeline, "per_client": res}
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps({k: v for k, v in out.items() if k not in ("per_client", "timeline_s_customers_subs_clocksready")}))


if __name__ == "__main__":
    main()
