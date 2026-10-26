"""Tests for the stress scripts' pure parts and their client, against a fake server on 127.0.0.1.

    python3 -m unittest test_stress
"""
import json
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import billing_year
import client
import concurrent_flows
import watch_clocks


def at(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


class Targets(unittest.TestCase):
    def test_month_zero_is_the_clock_start(self):
        self.assertEqual(billing_year.month(0), client.START)

    def test_a_year_is_six_two_month_advances_then_two_hours(self):
        self.assertEqual([at(t) for t in billing_year.targets(12)],
                         ["2027-01-01 00:00", "2027-03-01 00:00", "2027-05-01 00:00", "2027-07-01 00:00",
                          "2027-09-01 00:00", "2027-11-01 00:00", "2027-11-01 02:00"])

    def test_one_month_goes_straight_past_the_renewal(self):
        self.assertEqual([at(t) for t in billing_year.targets(1)], ["2026-12-01 02:00"])

    def test_an_odd_count_ends_with_a_one_month_step(self):
        self.assertEqual([at(t) for t in billing_year.targets(3)], ["2027-01-01 00:00", "2027-02-01 02:00"])

    def test_no_step_is_more_than_two_months(self):
        prev = client.START
        for t in billing_year.targets(12):
            self.assertLessEqual(t - prev, 62 * 24 * 3600 + 2 * 3600)
            prev = t


class Fake:
    """A server that answers each request from a queue of (status, headers, body), then 200 {}."""

    def __init__(self, answers):
        self.answers, self.paths, fake = list(answers), [], self

        class Handler(BaseHTTPRequestHandler):
            def handle_one(self):
                fake.paths.append((self.command, self.path, self.headers.get("Authorization")))
                status, headers, body = fake.answers.pop(0) if fake.answers else (200, {}, {})
                data = json.dumps(body).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_DELETE = handle_one

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


class ClientRetries(unittest.TestCase):
    def test_a_429_is_sent_again_and_counted_by_reason(self):
        fake = Fake([(429, {"Stripe-Rate-Limited-Reason": "endpoint-rate"}, {"error": {"code": "rate_limit"}}),
                     (200, {}, {"id": "cus_1"})])
        try:
            c = client.Client(fake.base, "sk_test_x")
            self.assertEqual(c.call("GET", "/v1/customers/cus_1"), (200, {"id": "cus_1"}))
            self.assertEqual((c.requests, c.r429), (2, {"endpoint-rate": 1}))
            self.assertEqual(fake.paths[0][2], "Bearer sk_test_x")
        finally:
            fake.close()

    def test_gives_up_after_its_tries(self):
        fake = Fake([(429, {}, {"error": {"code": "rate_limit"}})] * 3)
        try:
            c = client.Client(fake.base, "sk_test_x")
            self.assertEqual(c.call("GET", "/v1/customers", tries=2)[0], 429)
            self.assertEqual(c.requests, 2)
        finally:
            fake.close()

    def test_need_names_the_step_and_stripes_message(self):
        with self.assertRaisesRegex(RuntimeError, r"advance: 404 No such clock"):
            client.need(404, {"error": {"message": "No such clock"}}, "advance")


class WatchClocks(unittest.TestCase):
    def test_lists_every_page(self):
        page1 = {"data": [{"id": "clock_a", "status": "advancing"}], "has_more": True}
        page2 = {"data": [{"id": "clock_b", "status": "ready"}], "has_more": False}
        fake = Fake([(200, {}, page1), (200, {}, page2)])
        try:
            got = watch_clocks.list_clocks(client.Client(fake.base, "sk_test_x"))
            self.assertEqual([k["id"] for k in got], ["clock_a", "clock_b"])
            self.assertIn("starting_after=clock_a", fake.paths[1][1])
        finally:
            fake.close()


class Reference(unittest.TestCase):
    def test_the_recorded_stripe_runs_load(self):
        self.assertEqual(len(concurrent_flows.load_ref("billing")["steps"]), 7)
        self.assertEqual(len(concurrent_flows.load_ref("payments")["steps"]), 9)


if __name__ == "__main__":
    unittest.main()
