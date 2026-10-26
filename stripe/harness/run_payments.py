"""Run one payments-and-refunds flow against any Stripe-shaped server and record it.

A customer pays $50 with a card through a PaymentIntent, gets a $20 partial
refund, then the rest, then tries to refund again. A second payment is
declined. Records each step's answer, the charge's balance transaction, the
customer's charges and the events the flow caused, as comparable JSON.

    python3 run_payments.py --base-url https://api.stripe.com --key-file .env --label stripe
"""
import argparse
import json
import time
import uuid
from datetime import datetime, timezone

from run_receipts import API_VERSION, Client, add_key_args, error_of, field, key_from_args


AUTO = {"automatic_payment_methods[enabled]": "true", "automatic_payment_methods[allow_redirects]": "never"}
BT_WAIT_S = 30


def ok(status):
    return 200 <= status < 300


def err(body):
    e = error_of(body) or {}
    return {k: e.get(k) for k in ("type", "code", "decline_code") if e.get(k)}


class Payments:
    def __init__(self, client):
        self.c = client
        self.steps = []
        self.ids = set()
        self.since = None  # the server's time when the flow made its first object

    def record(self, name, result, started):
        self.steps.append({"step": len(self.steps) + 1, "name": name, "request": self.c.take_sent(),
                           "ms": round((time.monotonic() - started) * 1000, 1), "result": result})

    def keep(self, *ids):
        self.ids.update(i for i in ids if isinstance(i, str))

    def run(self):
        c = self.c
        t = time.monotonic()
        s, cus = c.call("POST", "/v1/customers", {"email": "jenny.rosen@example.com"})
        self.keep(field(cus, "id"))
        if isinstance(field(cus, "created"), int):
            self.since = cus["created"]
        self.record("create a customer", {"status": s}, t)
        if not ok(s):
            return

        t = time.monotonic()
        key = {"Idempotency-Key": str(uuid.uuid4())}
        pay = {"amount": "5000", "currency": "usd", "customer": cus["id"], "payment_method": "pm_card_visa",
               "confirm": "true", **AUTO}
        s, pi = c.call("POST", "/v1/payment_intents", pay, key)
        s2, again = c.call("POST", "/v1/payment_intents", pay, key)
        self.keep(field(pi, "id"), field(pi, "latest_charge"))
        self.record("pay $50 with a card, then replay the same request",
                    {"status": s, "pi_status": field(pi, "status"), "amount_received": field(pi, "amount_received"),
                     "replay_status": s2, "replay_same_id": field(again, "id") == field(pi, "id")}, t)
        if not ok(s) or not field(pi, "latest_charge"):
            return

        # Stripe attaches the balance transaction a few seconds after the charge.
        t = time.monotonic()
        s, ch = c.call("GET", "/v1/charges/" + pi["latest_charge"])
        while not field(ch, "balance_transaction") and time.monotonic() - t < BT_WAIT_S:
            time.sleep(1)
            s, ch = c.call("GET", "/v1/charges/" + pi["latest_charge"])
        waited = round(time.monotonic() - t, 1)
        bt_id = field(ch, "balance_transaction")
        s2, bt = c.call("GET", "/v1/balance_transactions/" + bt_id) if isinstance(bt_id, str) else (None, None)
        self.record("read the charge and its balance transaction",
                    {"status": s, "paid": field(ch, "paid"), "captured": field(ch, "captured"),
                     "card_brand": ((field(ch, "payment_method_details") or {}).get("card") or {}).get("brand"),
                     "bt_attached": isinstance(bt_id, str), "bt_status": s2,
                     "bt": {k: field(bt, k) for k in ("amount", "fee", "net", "type", "status")}}, t)
        self.steps[-1]["waited_s"] = waited

        t = time.monotonic()
        s, r1 = c.call("POST", "/v1/refunds", {"payment_intent": pi["id"], "amount": "2000"})
        _, ch = c.call("GET", "/v1/charges/" + pi["latest_charge"])
        self.keep(field(r1, "id"))
        self.record("refund $20",
                    {"status": s, "refund_status": field(r1, "status"), "refund_amount": field(r1, "amount"),
                     "charge_amount_refunded": field(ch, "amount_refunded"), "charge_refunded": field(ch, "refunded")}, t)

        t = time.monotonic()
        s, r2 = c.call("POST", "/v1/refunds", {"payment_intent": pi["id"]})
        _, ch = c.call("GET", "/v1/charges/" + pi["latest_charge"])
        self.keep(field(r2, "id"))
        self.record("refund the rest",
                    {"status": s, "refund_amount": field(r2, "amount"),
                     "charge_amount_refunded": field(ch, "amount_refunded"), "charge_refunded": field(ch, "refunded")}, t)

        t = time.monotonic()
        s, r3 = c.call("POST", "/v1/refunds", {"payment_intent": pi["id"]})
        self.record("try to refund again", {"status": s, "error": err(r3)}, t)

        t = time.monotonic()
        s, bad = c.call("POST", "/v1/payment_intents", {
            "amount": "5000", "currency": "usd", "customer": cus["id"],
            "payment_method": "pm_card_visa_chargeDeclined", "confirm": "true", **AUTO})
        bad_pi = (error_of(bad) or {}).get("payment_intent") or {}
        self.keep(bad_pi.get("id"), bad_pi.get("latest_charge"))
        s2, pi2 = c.call("GET", "/v1/payment_intents/" + bad_pi["id"]) if bad_pi.get("id") else (None, None)
        self.record("pay with a card that is declined",
                    {"status": s, "error": err(bad), "pi_status": field(pi2, "status"),
                     "last_payment_error_code": (field(pi2, "last_payment_error") or {}).get("code")}, t)

        t = time.monotonic()
        s, chs = c.call("GET", "/v1/charges?customer=%s&limit=10" % cus["id"])
        rows = field(chs, "data") or []
        self.keep(*[r.get("id") for r in rows])
        self.record("list the customer's charges",
                    {"status": s, "charges": sorted([r.get("status"), r.get("refunded")] for r in rows)}, t)

        t = time.monotonic()
        self.record("list the events the flow caused", {"event_types": self.events()}, t)

    def events(self):
        """Event types whose object is one this flow created, counted, from every event since the flow began."""
        since = "&created%%5Bgte%%5D=%d" % self.since if self.since is not None else ""
        counts, after = {}, None
        for _ in range(1000):  # a guard against a list that never ends, not a window
            s, page = self.c.call("GET", "/v1/events?limit=100" + since + ("&starting_after=" + after if after else ""))
            data = field(page, "data") or []
            for ev in data:
                obj = (ev.get("data") or {}).get("object") or {}
                if {obj.get("id"), obj.get("customer"), obj.get("payment_intent"), obj.get("charge")} & self.ids:
                    counts[ev["type"]] = counts.get(ev["type"], 0) + 1
            if not field(page, "has_more") or not data:
                break
            after = data[-1]["id"]
        return dict(sorted(counts.items()))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True)
    add_key_args(parser)
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", help="output file (default: stdout)")
    parser.add_argument("--stripe-version", default=API_VERSION)
    args = parser.parse_args()
    key = key_from_args(parser, args)
    run_at = datetime.now(timezone.utc)
    flow = Payments(Client(args.base_url, key, args.stripe_version))
    t = time.monotonic()
    flow.run()
    report = {"label": args.label, "base_url": args.base_url, "stripe_version": args.stripe_version,
              "run_at": run_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "total_seconds": round(time.monotonic() - t, 2), "steps": flow.steps}
    text = json.dumps(report, indent=2) + "\n"
    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
        print("wrote " + args.out)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
