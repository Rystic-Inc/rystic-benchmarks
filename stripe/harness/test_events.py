"""The events step counts a flow's own events however many other flows' events come first, and the
billing flow checks its clock as often as POLL_S says.

    python3 -m unittest test_events
"""
import time
import unittest
import urllib.parse

import run_flow
import run_payments

T = 1793491200  # the server's time when the flow made its first object


class FakeEvents:
    """Serves /v1/events newest first, 100 a page, honouring created[gte] and starting_after."""

    def __init__(self, events):
        self.events = sorted(events, key=lambda e: (-e["created"], e["id"]))
        self.paths = []

    def call(self, method, path, params=None, headers=None):
        self.paths.append(path)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        rows = self.events
        if "created[gte]" in q:
            rows = [e for e in rows if e["created"] >= int(q["created[gte]"][0])]
        if "starting_after" in q:
            ids = [e["id"] for e in rows]
            rows = rows[ids.index(q["starting_after"][0]) + 1:]
        page = rows[:100]
        return 200, {"object": "list", "data": page, "has_more": len(rows) > 100}

    def take_sent(self):
        return []


def event(n, created, typ, obj):
    return {"id": "evt_%05d" % n, "object": "event", "created": created, "type": typ, "data": {"object": obj}}


def world(own):
    """1,200 events from other flows after this one began, this flow's own, and 500 from before it."""
    evs = [event(i, T + 60, "customer.created", {"id": "cus_other%d" % i}) for i in range(1200)]
    evs += [event(5000 + i, T + 5, typ, obj) for i, (typ, obj) in enumerate(own)]
    evs += [event(9000 + i, T - 1000, "customer.created", {"id": "cus_old%d" % i}) for i in range(500)]
    return evs


class BillingEvents(unittest.TestCase):
    OWN = [("product.created", {"id": "prod_mine"}), ("customer.created", {"id": "cus_mine"}),
           ("invoice.paid", {"id": "in_mine", "customer": "cus_mine"}),
           ("invoice.paid", {"id": "in_mine2", "customer": "cus_mine"})]

    def flow(self, since):
        fake = FakeEvents(world(self.OWN))
        f = run_flow.Flow(fake, "t")
        f.ids = {"product": "prod_mine", "customer": "cus_mine", "invoices": ["in_mine", "in_mine2"]}
        f.since = since
        return f, fake

    def test_counts_own_events_behind_other_flows(self):
        f, _ = self.flow(T)
        self.assertEqual(f.events(), {"customer.created": 1, "invoice.paid": 2, "product.created": 1})

    def test_asks_only_for_events_since_the_flow_began(self):
        f, fake = self.flow(T)
        f.events()
        self.assertTrue(all("created%5Bgte%5D=" + str(T) in p for p in fake.paths), fake.paths[:2])
        self.assertEqual(len(fake.paths), 13)  # 1,204 events since T: 13 pages, none of the 500 older ones

    def test_without_a_start_time_it_still_reads_to_the_end(self):
        f, fake = self.flow(None)
        self.assertEqual(f.events(), {"customer.created": 1, "invoice.paid": 2, "product.created": 1})
        self.assertNotIn("created", fake.paths[0])

    def test_product_sets_the_start_time(self):
        class Server(FakeEvents):
            def call(self, method, path, params=None, headers=None):
                if path == "/v1/products":
                    return 200, {"id": "prod_x", "created": T}
                if path == "/v1/prices":
                    return 200, {"id": "price_x", "unit_amount": 2000, "recurring": {"interval": "month"}}
                return super().call(method, path, params, headers)
        f = run_flow.Flow(Server([]), "t")
        f.product_and_price()
        self.assertEqual(f.since, T)


class PaymentsEvents(unittest.TestCase):
    def test_counts_own_events_behind_other_flows(self):
        own = [("customer.created", {"id": "cus_mine"}), ("charge.succeeded", {"id": "ch_mine", "customer": "cus_mine"}),
               ("charge.refunded", {"id": "ch_mine"})]
        fake = FakeEvents(world(own))
        p = run_payments.Payments(fake)
        p.ids = {"cus_mine", "ch_mine"}
        p.since = T
        self.assertEqual(p.events(), {"charge.refunded": 1, "charge.succeeded": 1, "customer.created": 1})
        self.assertTrue(all("created%5Bgte%5D=" + str(T) in q for q in fake.paths))


class ClockPolling(unittest.TestCase):
    """The advance step waits POLL_S seconds between clock checks; 1 by default."""

    class Clock:
        def __init__(self):
            self.reads = 0

        def call(self, method, path, params=None, headers=None):
            if method == "POST":
                return 200, {"status": "advancing", "frozen_time": int(params["frozen_time"])}
            self.reads += 1
            target = run_flow.START + 30 * 24 * 3600 + 2 * 3600
            return 200, {"status": "ready" if self.reads >= 2 else "advancing", "frozen_time": target}

        def take_sent(self):
            return []

    def flow(self):
        f = run_flow.Flow(self.Clock(), "t")
        f.ids = {"clock": "clock_x"}
        f.period_end = run_flow.START + 30 * 24 * 3600
        return f

    def test_default_is_one_second(self):
        self.assertEqual(run_flow.POLL_S, 1)

    def test_checks_every_poll_s(self):
        saved, run_flow.POLL_S = run_flow.POLL_S, 0.01
        try:
            t = time.monotonic()
            result = self.flow().advance()
            elapsed = time.monotonic() - t
        finally:
            run_flow.POLL_S = saved
        self.assertEqual((result["clock_status"], result["polls"], result["reached_target"]), ("ready", 2, True))
        self.assertLess(elapsed, 0.5)


if __name__ == "__main__":
    unittest.main()
