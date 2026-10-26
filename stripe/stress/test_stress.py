"""Tests for the stress scripts' pure parts and their client, against a fake server on 127.0.0.1.

    python3 -m unittest test_stress
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import billing100
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
    """A server that answers each request from a queue of (status, headers, body), then 200 {}; None drops it."""

    def __init__(self, answers):
        self.answers, self.paths, self.idem, fake = list(answers), [], [], self

        class Handler(BaseHTTPRequestHandler):
            def handle_one(self):
                fake.paths.append((self.command, self.path, self.headers.get("Authorization")))
                fake.idem.append(self.headers.get("Idempotency-Key"))
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                answer = fake.answers.pop(0) if fake.answers else (200, {}, {})
                if answer is None:
                    self.close_connection = True
                    return
                status, headers, body = answer
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

    def test_a_refused_change_to_an_advancing_clock_isnt_sent_again(self):
        fake = Fake([(429, {}, {"error": {"message": client.ADVANCING + " - cannot perform modifications"}})])
        try:
            c = client.Client(fake.base, "sk_test_x")
            self.assertEqual(c.call("DELETE", "/v1/test_helpers/test_clocks/clock_1")[0], 429)
            self.assertEqual((c.requests, c.r429), (1, {"clock-advancing": 1}))
        finally:
            fake.close()

    def test_a_post_keeps_its_idempotency_key_across_retries(self):
        fake = Fake([(429, {}, {"error": {"code": "rate_limit"}}), (200, {}, {}), (200, {}, {})])
        try:
            c = client.Client(fake.base, "sk_test_x")
            c.call("POST", "/v1/customers", {"email": "a@example.com"})
            c.call("POST", "/v1/customers", {"email": "b@example.com"})
            c.call("GET", "/v1/customers")
            first, again, second, get = fake.idem
            self.assertTrue(first)
            self.assertEqual(first, again)
            self.assertNotIn(second, (None, first))
            self.assertIsNone(get)
        finally:
            fake.close()

    def test_a_429_body_that_isnt_an_object_is_sent_again(self):
        fake = Fake([(429, {}, ["x"]), (200, {}, {"id": "cus_1"})])
        try:
            c = client.Client(fake.base, "sk_test_x")
            self.assertEqual(c.call("GET", "/v1/customers/cus_1"), (200, {"id": "cus_1"}))
            self.assertEqual(c.r429, {"unlabelled": 1})
        finally:
            fake.close()

    def test_instance_ports_takes_running_instances_with_the_prefix(self):
        ps = {"data": {"instances": [{"name": "c01", "port": 9, "status": "running"},
                                     {"name": "c00", "port": 8, "status": "running"},
                                     {"name": "c02", "port": 7, "status": "stopped"},
                                     {"name": "x00", "port": 6, "status": "running"}]}}
        done = mock.Mock(stdout=json.dumps(ps))
        with mock.patch.object(client.subprocess, "run", return_value=done):
            self.assertEqual(client.instance_ports("c"), [8, 9])

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


def ids(prefix, n):
    return [(200, {}, {"id": "%s_%d" % (prefix, i)}) for i in range(n)]


BAD = (400, {}, {"error": {"message": "bad"}})


class Cleanup(unittest.TestCase):
    def test_billing100_deletes_its_clocks_after_an_error(self):
        fake = Fake(ids("prod", 1) + ids("price", 1) + ids("clock", 34) + [BAD])
        try:
            r = billing100.run_client(fake.base, "sk_test_x", 0, float("inf"), float("inf"), False,
                                      billing100.Progress())
        finally:
            fake.close()
        self.assertIn("customer: 400 bad", r["error"])
        deleted = [p for m, p, _ in fake.paths if m == "DELETE"]
        self.assertEqual(deleted, ["/v1/test_helpers/test_clocks/clock_%d" % i for i in range(34)])
        self.assertEqual((r["clocks_deleted"], r["clocks_not_deleted"]), (34, 0))
        self.assertIn("ended_s", r)

    def test_billing100_counts_a_clock_still_advancing_and_asks_once(self):
        refused = (429, {}, {"error": {"message": client.ADVANCING + " - cannot perform modifications"}})
        fake = Fake(ids("prod", 1) + ids("price", 1) + ids("clock", 34) + [BAD] + ids("ok", 33) + [refused])
        try:
            r = billing100.run_client(fake.base, "sk_test_x", 0, float("inf"), float("inf"), False,
                                      billing100.Progress())
        finally:
            fake.close()
        self.assertEqual((r["clocks_deleted"], r["clocks_not_deleted"]), (33, 1))
        self.assertEqual([p for m, p, _ in fake.paths].count("/v1/test_helpers/test_clocks/clock_33"), 1)

    def test_billing100_keeps_its_record_when_a_delete_fails(self):
        fake = Fake(ids("prod", 1) + ids("price", 1) + ids("clock", 34) + [BAD] + [None] * 4)
        try:
            r = billing100.run_client(fake.base, "sk_test_x", 0, float("inf"), float("inf"), False,
                                      billing100.Progress())
        finally:
            fake.close()
        self.assertIn("customer: 400 bad", r["error"])
        self.assertEqual((r["clocks_deleted"], r["clocks_not_deleted"]), (32, 2))
        self.assertIn("ended_s", r)

    def test_billing100_wall_time_ends_at_the_last_check_and_a_crashed_client_still_counts(self):
        def fake_run(base, key, i, *rest):
            if i == 1:
                raise RuntimeError("boom")
            return {"client": i, "checked_s": 5.0, "ended_s": 9.0, "setup_s": 1.0}
        out = os.path.join(tempfile.mkdtemp(), "out.json")
        argv = ["billing100.py", "--ports", "1,2", "--clients", "2", "--out", out]
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_x"}), \
                mock.patch.object(billing100, "run_client", fake_run), mock.patch("builtins.print"):
            billing100.main()
        with open(out) as f:
            got = json.load(f)
        self.assertEqual((got["wall_s"], got["wall_s_with_deletes"]), (5.0, 9.0))
        self.assertEqual(got["clients_set_up"], 1)
        self.assertIn("RuntimeError: boom", got["per_client"][1]["error"])

    def year(self, answers, *args):
        fake = Fake(answers)
        out = self.last_out = os.path.join(tempfile.mkdtemp(), "out.json")
        argv = ["billing_year.py", "--base-url", fake.base, "--customers", "1", "--months", "2",
                "--poll-s", "0", "--total-min", "0.05", "--out", out] + list(args)
        try:
            with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_x"}), \
                    mock.patch("builtins.print"):
                billing_year.main()
        finally:
            fake.close()
        with open(out) as f:
            return json.load(f), [(m, p) for m, p, _ in fake.paths]

    def test_billing_year_cleans_up_after_an_error(self):
        got, paths = self.year(ids("prod", 1) + ids("price", 1) + ids("clock", 1) + [BAD])
        self.assertIn("customer: 400 bad", got["error"])
        self.assertEqual(paths[-3:], [("DELETE", "/v1/test_helpers/test_clocks/clock_0"),
                                      ("POST", "/v1/prices/price_0"), ("POST", "/v1/products/prod_0")])
        self.assertEqual(got["clocks_deleted"], 1)

    def test_billing_year_cleans_up_when_a_delete_fails(self):
        got, paths = self.year(ids("prod", 1) + ids("price", 1) + ids("clock", 1) + [BAD, None, None])
        self.assertIn("customer: 400 bad", got["error"])
        self.assertEqual((got["clocks_deleted"], got["clocks_not_deleted"]), (0, 1))
        self.assertEqual(paths[-2:], [("POST", "/v1/prices/price_0"), ("POST", "/v1/products/prod_0")])

    def test_billing_year_saves_before_it_deletes(self):
        def interrupted(c, clocks):
            raise KeyboardInterrupt
        with mock.patch.object(billing_year.client, "delete_clocks", interrupted), self.assertRaises(KeyboardInterrupt):
            self.year(ids("prod", 1) + ids("price", 1) + ids("clock", 1) + [BAD])
        with open(self.last_out) as f:
            self.assertIn("customer: 400 bad", json.load(f)["error"])

    def test_billing_year_cleans_up_when_it_cant_save(self):
        fake = Fake(ids("prod", 1) + ids("price", 1) + ids("clock", 1) + ids("cus", 1) + ids("sub", 1))
        argv = ["billing_year.py", "--base-url", fake.base, "--customers", "1", "--out", "/nonexistent-dir/out.json"]
        try:
            with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_x"}), \
                    mock.patch("builtins.print"), self.assertRaises(OSError):
                billing_year.main()
        finally:
            fake.close()
        self.assertIn(("DELETE", "/v1/test_helpers/test_clocks/clock_0"), [(m, p) for m, p, _ in fake.paths])

    def test_billing_year_doesnt_wait_on_a_refused_advance(self):
        got, paths = self.year(ids("prod", 1) + ids("price", 1) + ids("clock", 1) + ids("cus", 1) + ids("sub", 1)
                               + [BAD])
        self.assertEqual(got["rounds"][0]["advance_error_count"], 1)
        self.assertIn("did not complete", got["stopped"])
        self.assertNotIn(("GET", "/v1/test_helpers/test_clocks/clock_0"), paths)
        self.assertEqual(paths[-3][0], "DELETE")


class ClockWait(unittest.TestCase):
    def wait(self, advance):
        class Flow:
            def __init__(self, c, tag):
                self.steps = [{}] * 4 + [{"ms": 1500, "result": advance}]

            def run(self):
                pass

            def cleanup(self):
                pass
        merged = {"agrees_with_stripe": {"x": 6}, "per_step": {}}
        tally = {"lock": threading.Lock(), "codes": {}}
        with mock.patch.object(concurrent_flows.run_flow, "Flow", Flow), \
                mock.patch.object(concurrent_flows.compare_payments, "compare", return_value=merged):
            return concurrent_flows.one("billing", "http://127.0.0.1:1", "sk_test_x", "t", tally, {"steps": [0] * 7})

    def test_an_advance_that_was_sent_has_a_wait(self):
        self.assertEqual(self.wait({"status": 200, "clock_status": "ready"})["clock_wait_s"], 1.5)

    def test_an_advance_refused_or_not_run_has_none(self):
        self.assertNotIn("clock_wait_s", self.wait({"status": 429}))
        self.assertNotIn("clock_wait_s", self.wait({"not_run": "an earlier step failed"}))


class Reference(unittest.TestCase):
    def test_the_recorded_stripe_runs_load(self):
        self.assertEqual(len(concurrent_flows.load_ref("billing")["steps"]), 7)
        self.assertEqual(len(concurrent_flows.load_ref("payments")["steps"]), 9)


if __name__ == "__main__":
    unittest.main()
