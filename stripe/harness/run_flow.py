"""Run one billing flow against any Stripe-shaped server and record what it saw.

A product and monthly price, a customer on a test clock with a card, a
subscription, then the clock advanced past the first renewal. Records each
step's observable answer, the renewal invoices, the events the flow caused,
and how long everything took, as comparable JSON.

    python3 run_flow.py --base-url https://api.stripe.com --key-file .env --label stripe
    STRIPE_SECRET_KEY=sk_test_x python3 run_flow.py --base-url http://127.0.0.1:PORT --label rystic

The time step uses Stripe's test-clock API on every server, so all three get
the same calls. Every step runs on every server: one whose input an earlier
step failed to make records which step it needed. Against Stripe it creates
test-mode objects and deletes the clock (with its customer and subscription)
at the end, even after failed steps.
"""
import argparse
import json
import time
from datetime import datetime, timezone

from run_receipts import API_VERSION, Client, add_key_args, error_of, field, key_from_args

START = int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp())
HOUR = 3600
CLOCK_WAIT_S = 120


def err_summary(status, body):
    err = error_of(body) or {}
    return {"status": status, "error": {k: err.get(k) for k in ("type", "code", "message") if err.get(k)}}


def ok(status):
    return isinstance(status, int) and 200 <= status < 300


def first_item(sub):
    data = (field(sub, "items") or {}).get("data") if isinstance(field(sub, "items"), dict) else None
    return data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}


class Flow:
    """The billing flow, run step by step on every server.

    Each step runs when the objects it needs exist; otherwise it records which
    earlier step it needed. Calls are sent as written: no explicit attach, no
    invoice read in place of the expand.
    """
    # The object each later step needs, and the step that makes it.
    MAKES = {"product": 1, "price": 1, "clock": 2, "customer": 3, "subscription": 4}
    NAMES = ["create a product and a $20/month price", "create a test clock at 1 Nov 2026",
             "create a customer on the clock with a card", "subscribe the customer",
             "advance the clock past the first renewal",
             "read the subscription and its invoices after the renewal", "list the events the flow caused"]

    def __init__(self, client, run_tag):
        self.c = client
        self.tag = run_tag
        self.steps = []
        self.ids = {}
        self.period_end = None

    def record(self, name, result, started):
        self.steps.append({"step": len(self.steps) + 1, "name": name,
                           "request": self.c.take_sent(),
                           "ms": round((time.monotonic() - started) * 1000, 1),
                           "result": result})
        return result

    def call(self, method, path, params=None):
        try:
            return self.c.call(method, path, params)
        except OSError as e:  # unreachable server or timeout
            return 0, {"error": {"type": "connection_error", "message": type(e).__name__}}

    def run(self):
        for n, (name, step) in enumerate(zip(self.NAMES, (
                self.product_and_price, self.clock, self.customer, self.subscribe,
                self.advance, self.after_renewal, self.list_events)), 1):
            t = time.monotonic()
            needs = [k for k in getattr(step, "needs", ()) if k not in self.ids]
            if needs:
                made = self.MAKES[needs[0]]
                result = {"not_run": "needs the %s from step %d (%s)" % (needs[0], made, self.NAMES[made - 1])}
            else:
                try:
                    result = step()
                except Exception as e:  # an answer shaped unlike Stripe's
                    result = {"harness_error": "%s in step %d" % (type(e).__name__, n)}
            self.record(name, result, t)

    def product_and_price(self):
        s, prod = self.call("POST", "/v1/products", {"name": "Blog plan " + self.tag})
        if not ok(s) or not field(prod, "id"):
            return err_summary(s, prod)
        self.ids["product"] = prod["id"]
        s, price = self.call("POST", "/v1/prices", {
            "product": prod["id"], "unit_amount": "2000", "currency": "usd",
            "recurring[interval]": "month"})
        if not ok(s) or not field(price, "id"):
            return err_summary(s, price)
        self.ids["price"] = price["id"]
        return {"status": s, "unit_amount": field(price, "unit_amount"),
                "interval": (field(price, "recurring") or {}).get("interval")}

    def clock(self):
        s, clock = self.call("POST", "/v1/test_helpers/test_clocks",
                             {"frozen_time": str(START), "name": "blog " + self.tag})
        if not ok(s) or not field(clock, "id"):
            return err_summary(s, clock)
        self.ids["clock"] = clock["id"]
        return {"status": s, "frozen_time": field(clock, "frozen_time"), "clock_status": field(clock, "status")}

    def customer(self):
        s, cus = self.call("POST", "/v1/customers", {
            "email": "jenny.rosen@example.com", "test_clock": self.ids["clock"],
            "payment_method": "pm_card_visa", "invoice_settings[default_payment_method]": "pm_card_visa"})
        if not ok(s) or not field(cus, "id"):
            return err_summary(s, cus)
        self.ids["customer"] = cus["id"]
        s2, pms = self.call("GET", "/v1/customers/%s/payment_methods" % cus["id"])
        cards = [c for c in field(pms, "data") or [] if isinstance(c, dict)]
        if cards and cards[0].get("id"):
            self.ids["payment_method"] = cards[0]["id"]
        result = {"status": s, "test_clock_matches": field(cus, "test_clock") == self.ids["clock"],
                  "cards_attached": len(cards),
                  "card_brand": (cards[0].get("card") or {}).get("brand") if cards else None}
        if not ok(s2):
            result["list_cards"] = err_summary(s2, pms)
        return result
    customer.needs = ("clock",)

    def subscribe(self):
        params = {"customer": self.ids["customer"], "items[0][price]": self.ids["price"],
                  "expand[0]": "latest_invoice"}
        # With no listed card, the customer's invoice_settings default pays.
        if "payment_method" in self.ids:
            params["default_payment_method"] = self.ids["payment_method"]
        s, sub = self.call("POST", "/v1/subscriptions", params)
        if not ok(s) or not field(sub, "id"):
            return err_summary(s, sub)
        self.ids["subscription"] = sub["id"]
        item = first_item(sub)
        raw = field(sub, "latest_invoice")
        inv = raw if isinstance(raw, dict) else {}
        self.period_end = item.get("current_period_end")
        return {"status": s, "subscription_status": field(sub, "status"),
                "period_start": item.get("current_period_start"), "period_end": self.period_end,
                "latest_invoice_type": "object" if isinstance(raw, dict) else "string" if isinstance(raw, str) else None,
                "first_invoice": {"billing_reason": inv.get("billing_reason"),
                                  "status": inv.get("status"), "amount_paid": inv.get("amount_paid")}}
    subscribe.needs = ("customer", "price")

    def advance(self):
        t = time.monotonic()
        clock = self.ids["clock"]
        end = self.period_end if isinstance(self.period_end, int) else START + 30 * 24 * HOUR
        target = end + 2 * HOUR
        s, adv = self.call("POST", "/v1/test_helpers/test_clocks/%s/advance" % clock,
                           {"frozen_time": str(target)})
        polls, clock_status, frozen = 0, field(adv, "status"), field(adv, "frozen_time")
        if ok(s):
            while clock_status != "ready" and time.monotonic() - t < CLOCK_WAIT_S:
                time.sleep(1)
                polls += 1
                _, cl = self.call("GET", "/v1/test_helpers/test_clocks/%s" % clock)
                clock_status, frozen = field(cl, "status"), field(cl, "frozen_time")
        result = {"status": s, "clock_status": clock_status, "reached_target": frozen == target, "polls": polls}
        if not ok(s):
            result.update(err_summary(s, adv))
        return result
    advance.needs = ("clock",)

    def after_renewal(self):
        sub_id = self.ids["subscription"]
        s, sub2 = self.call("GET", "/v1/subscriptions/%s" % sub_id)
        item2 = first_item(sub2)
        s2, invs = self.call("GET", "/v1/invoices?subscription=%s&limit=10" % sub_id)
        rows = sorted((i for i in field(invs, "data") or [] if isinstance(i, dict)),
                      key=lambda i: (i.get("created") or 0, i.get("billing_reason") or ""))
        self.ids["invoices"] = [i["id"] for i in rows if i.get("id")]
        return {"status": [s, s2], "subscription_status": field(sub2, "status"),
                "period_moved_one_month": self.period_end is not None
                and item2.get("current_period_start") == self.period_end,
                "invoices": [{"billing_reason": i.get("billing_reason"), "status": i.get("status"),
                              "amount_paid": i.get("amount_paid")} for i in rows]}
    after_renewal.needs = ("subscription",)

    def list_events(self):
        return {"event_types": self.events()}

    def events(self):
        """Event types whose object is one this flow created, counted."""
        mine = {v for k, v in self.ids.items() if isinstance(v, str)} | set(self.ids.get("invoices", []))
        counts, after = {}, None
        for _ in range(10):
            path = "/v1/events?limit=100" + ("&starting_after=" + after if after else "")
            s, page = self.call("GET", path)
            data = field(page, "data") or []
            for ev in data:
                if not isinstance(ev, dict):
                    continue
                obj = (ev.get("data") or {}).get("object") or {}
                related = {obj.get("id"), obj.get("customer"), obj.get("subscription"), obj.get("test_clock"),
                           obj.get("product"), obj.get("invoice"), ((obj.get("parent") or {}).get("subscription_details") or {}).get("subscription")}
                if related & mine:
                    counts[ev.get("type")] = counts.get(ev.get("type"), 0) + 1
            if not field(page, "has_more") or not data or not isinstance(data[-1], dict):
                break
            after = data[-1].get("id")
        return dict(sorted(counts.items()))

    def cleanup(self):
        if "clock" in self.ids:
            self.call("DELETE", "/v1/test_helpers/test_clocks/%s" % self.ids["clock"])
        if "price" in self.ids:
            self.call("POST", "/v1/prices/%s" % self.ids["price"], {"active": "false"})
        if "product" in self.ids:
            self.call("POST", "/v1/products/%s" % self.ids["product"], {"active": "false"})
        self.c.take_sent()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True)
    add_key_args(parser)
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", help="output file (default: stdout)")
    parser.add_argument("--stripe-version", default=API_VERSION)
    parser.add_argument("--keep", action="store_true", help="leave the clock and its objects in place")
    args = parser.parse_args()

    key = key_from_args(parser, args)
    run_at = datetime.now(timezone.utc)
    flow = Flow(Client(args.base_url, key, args.stripe_version), run_at.strftime("%Y%m%d%H%M%S"))
    t = time.monotonic()
    try:
        flow.run()
    finally:
        total = round(time.monotonic() - t, 2)
        if not args.keep:
            flow.cleanup()
    report = {"label": args.label, "flow": "billing", "base_url": args.base_url, "stripe_version": args.stripe_version,
              "run_at": run_at.strftime("%Y-%m-%dT%H:%M:%SZ"), "total_seconds": total, "steps": flow.steps}
    text = json.dumps(report, indent=2) + "\n"
    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
        print("wrote " + args.out)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
