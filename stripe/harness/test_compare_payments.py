"""Tests for compare_payments.py.  Run: python3 -m unittest test_compare_payments"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from compare_payments import compare, compare_step, load, same, table

HERE = os.path.dirname(os.path.abspath(__file__))


def fixture(name):
    return load(os.path.join(HERE, "testdata", name))


class ComparePayments(unittest.TestCase):
    def merged(self, *servers):
        return compare(fixture("stripe.json"), [fixture(s) for s in servers])

    def test_match_ignores_timing_requests_and_masked_ids(self):
        m = self.merged("match.json")
        self.assertEqual(m["agrees_with_stripe"], {"same_server": 3})
        for n in ("1", "2", "3"):
            self.assertEqual(m["per_step"][n]["same_server"]["verdict"], "match")

    def test_field_mismatch_names_each_differing_field(self):
        m = self.merged("mismatch.json")
        self.assertEqual(m["agrees_with_stripe"], {"field_mismatch": 1})
        step2 = m["per_step"]["2"]["field_mismatch"]
        self.assertEqual(step2["verdict"], "differs")
        self.assertEqual(step2["fields"], ["bt.fee"])
        step3 = m["per_step"]["3"]["field_mismatch"]
        self.assertEqual(step3["fields"], ["event_types.charge.updated"])

    def test_missing_step_is_not_a_match(self):
        m = self.merged("missing.json")
        self.assertEqual(m["per_step"]["3"]["missing_step"]["verdict"], "missing")
        self.assertEqual(m["agrees_with_stripe"], {"missing_step": 2})

    def test_error_where_stripe_succeeded(self):
        m = self.merged("error.json")
        step1 = m["per_step"]["1"]["error_server"]
        self.assertEqual(step1["verdict"], "differs")
        self.assertEqual(step1["fields"], ["error.type", "status"])
        self.assertEqual(step1["note"], "error where Stripe succeeded")
        self.assertEqual(m["agrees_with_stripe"], {"error_server": 2})

    def test_several_servers_and_table(self):
        m = self.merged("match.json", "mismatch.json", "missing.json", "error.json")
        self.assertEqual(list(m["servers"]), ["stripe", "same_server", "field_mismatch", "missing_step", "error_server"])
        text = table(m)
        self.assertIn("agrees with Stripe", text)
        self.assertIn("3/3", text)
        self.assertIn("missing", text)

    def test_duplicate_labels_get_distinct_keys(self):
        m = self.merged("match.json", "match.json")
        self.assertEqual(list(m["agrees_with_stripe"]), ["same_server", "same_server_2"])

    def test_receipts_results_use_calls(self):
        m = compare(fixture("receipts-stripe.json"), [fixture("receipts-server.json")])
        self.assertIn("per_call", m)
        self.assertEqual(m["per_call"]["1"]["receipts_server"]["fields"], ["email"])
        self.assertEqual(m["agrees_with_stripe"], {"receipts_server": 1})

    def test_cli_writes_merged_json(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "merged.json")
            run = subprocess.run([sys.executable, os.path.join(HERE, "compare_payments.py"),
                                  os.path.join(HERE, "testdata", "stripe.json"),
                                  os.path.join(HERE, "testdata", "mismatch.json"), "--out", out],
                                 capture_output=True, text=True, check=True)
            self.assertIn("Field Mismatch", run.stdout)
            with open(out) as f:
                self.assertEqual(json.load(f)["agrees_with_stripe"], {"field_mismatch": 1})


class CompareStep(unittest.TestCase):
    def verdict(self, want, got):
        return compare_step({"result": want}, {"result": got})

    def test_empty_dict_is_not_an_absent_field(self):
        self.assertEqual(self.verdict({}, {})["verdict"], "match")
        self.assertEqual(self.verdict({"a": {}}, {})["fields"], ["a"])
        self.assertEqual(self.verdict({"a": {}}, {"a": None})["fields"], ["a"])
        self.assertEqual(self.verdict({"status": 400, "error": {}}, {"status": 400})["fields"], ["error"])

    def test_unreachable_server_is_an_error(self):
        v = self.verdict({"status": 200}, {"status": 0})
        self.assertEqual(v["note"], "error where Stripe succeeded")

    def test_a_list_of_statuses_succeeds_only_when_each_does(self):
        v = self.verdict({"status": [200, 200]}, {"status": [200, 404]})
        self.assertEqual(v["fields"], ["status"])
        self.assertEqual(v["note"], "error where Stripe succeeded")

    def test_only_masked_object_ids_match_any_id(self):
        self.assertTrue(same("ch_\u2026", "ch_3AbC"))
        self.assertFalse(same("ch_\u2026", "pi_3AbC"))
        self.assertFalse(same("resource_\u2026", "resource_missing"))
        self.assertFalse(same("ch_a", "ch_b"))

    def test_runs_of_different_scripts_are_refused(self):
        with self.assertRaises(ValueError):
            compare(fixture("stripe.json"), [fixture("receipts-server.json")])


class CompareBillingFlow(unittest.TestCase):
    def merged(self, *servers):
        return compare(fixture("flow-stripe.json"), [fixture(s) for s in servers])

    def test_agreeing_flow_matches_without_comparing_ids_times_or_polls(self):
        m = self.merged("flow-match.json")
        self.assertEqual(m["kind"], "billing flow")
        self.assertEqual(m["agrees_with_stripe"], {"same_flow": 7})
        self.assertEqual(m["per_step"]["7"]["same_flow"]["event_types_sent"], "6 of 6")

    def test_periods_are_scored_against_the_servers_own_clock(self):
        m = self.merged("flow-shifted.json")
        self.assertEqual(m["per_step"]["2"]["shifted_clock"]["fields"], ["frozen_time_from_requested"])
        self.assertEqual(m["per_step"]["4"]["shifted_clock"]["verdict"], "match")

    def test_card_and_expand_gaps_name_their_fields(self):
        m = self.merged("flow-partial.json")
        self.assertEqual(m["per_step"]["3"]["partial_flow"]["fields"], ["card_brand", "cards_attached"])
        self.assertEqual(m["per_step"]["4"]["partial_flow"]["fields"],
                         ["first_invoice.amount_paid", "first_invoice.billing_reason", "first_invoice.status",
                          "latest_invoice_embedded"])

    def test_event_types_report_sent_missing_and_extra(self):
        v = self.merged("flow-partial.json")["per_step"]["7"]["partial_flow"]
        self.assertEqual(v["verdict"], "differs")
        self.assertEqual(v["event_types_sent"], "4 of 6")
        self.assertEqual(v["missing_types"], ["test_helpers.test_clock.created", "test_helpers.test_clock.ready"])
        self.assertEqual(v["extra_types"], ["payment_method.attached"])
        self.assertEqual(v["count_differs"], ["invoice.paid"])

    def test_step_not_run_scores_as_not_matching_with_its_reason(self):
        m = self.merged("flow-notrun.json")
        v = m["per_step"]["4"]["not_run"]
        self.assertEqual(v["verdict"], "not run")
        self.assertIn("step 3", v["reason"])
        self.assertEqual(m["per_step"]["3"]["not_run"]["note"], "error where Stripe succeeded")
        self.assertIn("error.type", m["per_step"]["3"]["not_run"]["fields"])
        self.assertEqual(m["per_step"]["5"]["not_run"]["verdict"], "match")
        self.assertEqual(m["agrees_with_stripe"], {"not_run": 3})

    def test_old_run_that_stopped_early_has_missing_steps(self):
        m = self.merged("flow-short.json")
        self.assertEqual(m["per_step"]["5"]["short_run"]["verdict"], "missing")
        self.assertEqual(m["agrees_with_stripe"], {"short_run": 2})

    def test_table_shows_event_counts_and_not_run(self):
        text = table(self.merged("flow-match.json", "flow-partial.json", "flow-notrun.json"))
        self.assertIn("sent 4 of 6 Stripe event types", text)
        self.assertIn("missing test_helpers.test_clock.created", text)
        self.assertIn("extra payment_method.attached", text)
        self.assertIn("not run: needs the customer from step 3", text)
        self.assertIn("7/7", text)

    def test_billing_and_payments_runs_are_not_mixed(self):
        with self.assertRaises(ValueError):
            compare(fixture("flow-stripe.json"), [fixture("match.json")])
        with self.assertRaises(ValueError):
            compare(fixture("stripe.json"), [fixture("flow-match.json")])


START = 1793491200
FAKE_KEY = "sk_test_FAKEkeyFORtestsONLY1234"


class FakeStripe(BaseHTTPRequestHandler):
    """A tiny Stripe-shaped server; `fail` names routes that answer 400."""
    fail = set()
    seen = []

    def log_message(self, *args):
        pass

    def answer(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def handle_any(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        form = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode())) if n else {}
        path = self.path.split("?")[0]
        type(self).seen.append((method, self.path, self.headers.get("Authorization")))
        route = method + " " + path
        if route in self.fail:
            return self.answer(400, {"error": {"type": "invalid_request_error", "code": "parameter_unknown",
                                               "message": "no"}})
        period = {"current_period_start": START, "current_period_end": START + 30 * 86400}
        renewed = {"current_period_start": START + 30 * 86400, "current_period_end": START + 61 * 86400}
        table = {
            "POST /v1/products": {"id": "prod_1"},
            "POST /v1/prices": {"id": "price_1", "unit_amount": 2000, "recurring": {"interval": "month"}},
            "POST /v1/test_helpers/test_clocks": {"id": "clock_1", "frozen_time": START, "status": "ready"},
            "POST /v1/customers": {"id": "cus_1", "test_clock": "clock_1"},
            "GET /v1/customers/cus_1/payment_methods": {"data": []},
            "POST /v1/subscriptions": {"id": "sub_1", "status": "active", "items": {"data": [period]},
                                       "latest_invoice": "in_1"},
            "POST /v1/test_helpers/test_clocks/clock_1/advance": {"id": "clock_1", "status": "ready",
                                                                  "frozen_time": int(form.get("frozen_time", 0))},
            "GET /v1/subscriptions/sub_1": {"id": "sub_1", "status": "active", "items": {"data": [renewed]}},
            "GET /v1/invoices": {"data": []},
            "GET /v1/events": {"data": [{"id": "evt_1", "type": "customer.created",
                                         "data": {"object": {"id": "cus_1"}}}], "has_more": False},
            "DELETE /v1/test_helpers/test_clocks/clock_1": {"id": "clock_1", "deleted": True},
            "POST /v1/prices/price_1": {"id": "price_1"},
            "POST /v1/products/prod_1": {"id": "prod_1"},
        }
        if route in table:
            return self.answer(200, table[route])
        return self.answer(404, {"error": {"type": "invalid_request_error", "code": "resource_missing"}})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_DELETE(self):
        self.handle_any("DELETE")


class FakeServerCase(unittest.TestCase):
    def setUp(self):
        FakeStripe.fail, FakeStripe.seen = set(), []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeStripe)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def flow(self):
        from run_flow import Flow
        from run_receipts import Client
        f = Flow(Client(self.base, "sk_test_x"), "t")
        f.run()
        f.cleanup()
        return f


class RunFlowRunsEveryStep(FakeServerCase):
    def test_failed_customer_create_still_runs_the_steps_it_can(self):
        FakeStripe.fail = {"POST /v1/customers"}
        steps = self.flow().steps
        self.assertEqual([s["step"] for s in steps], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(steps[2]["result"]["status"], 400)
        self.assertEqual(steps[2]["result"]["error"]["code"], "parameter_unknown")
        self.assertIn("step 3", steps[3]["result"]["not_run"])
        self.assertEqual(steps[4]["result"]["clock_status"], "ready")
        self.assertTrue(steps[4]["result"]["reached_target"])
        self.assertIn("step 4", steps[5]["result"]["not_run"])
        self.assertIn("event_types", steps[6]["result"])
        self.assertIn(("DELETE", "/v1/test_helpers/test_clocks/clock_1"), [s[:2] for s in FakeStripe.seen])

    def test_unexpanded_invoice_and_no_card_are_recorded_as_written(self):
        steps = self.flow().steps
        self.assertEqual(steps[2]["result"]["cards_attached"], 0)
        sub = steps[3]["result"]
        self.assertEqual(sub["latest_invoice_type"], "string")
        self.assertEqual(sub["first_invoice"], {"billing_reason": None, "status": None, "amount_paid": None})
        self.assertEqual(sub["period_end"], START + 30 * 86400)
        sent = [s[1] for s in FakeStripe.seen]
        self.assertFalse(any("attach" in p for p in sent))
        self.assertFalse(any(p.startswith("/v1/invoices/") for p in sent))
        self.assertTrue(steps[5]["result"]["period_moved_one_month"])

    def test_failed_clock_skips_the_advance_and_needs_step_2(self):
        FakeStripe.fail = {"POST /v1/test_helpers/test_clocks"}
        steps = self.flow().steps
        self.assertEqual(len(steps), 7)
        self.assertIn("step 2", steps[2]["result"]["not_run"])
        self.assertIn("step 2", steps[4]["result"]["not_run"])
        self.assertNotIn("DELETE", [s[0] for s in FakeStripe.seen])

    def test_recorded_requests_mask_object_ids(self):
        steps = self.flow().steps
        text = json.dumps(steps)
        for raw in ("clock_1", "sub_1", "cus_1"):
            self.assertNotIn(raw, text)


class KeyFile(FakeServerCase):
    def write(self, text):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as f:
            f.write(text)
        self.addCleanup(os.remove, path)
        return path

    def test_reads_stripe_secret_key_first(self):
        from run_receipts import read_key_file
        path = self.write("# test\nOTHER=sk_test_other\nexport STRIPE_SECRET_KEY=\"%s\"\n" % FAKE_KEY)
        self.assertEqual(read_key_file(path), FAKE_KEY)

    def test_falls_back_to_the_first_test_key_value(self):
        from run_receipts import read_key_file
        path = self.write("A=hello\nRESTRICTED='rk_test_abc'\nB=sk_test_def\n")
        self.assertEqual(read_key_file(path), "rk_test_abc")

    def test_file_without_a_key_names_the_file_not_its_values(self):
        from run_receipts import read_key_file
        path = self.write("STRIPE_PUBLISHABLE=pk_test_secretish\nX=sk_live_nope\n")
        with self.assertRaises(ValueError) as cm:
            read_key_file(path)
        self.assertIn(path, str(cm.exception))
        self.assertNotIn("pk_test_secretish", str(cm.exception))
        self.assertNotIn("sk_live_nope", str(cm.exception))

    def test_live_key_is_refused_without_echoing_it(self):
        from run_receipts import read_key_file
        path = self.write("STRIPE_SECRET_KEY=sk_live_REALLYsecret\n")
        with self.assertRaises(ValueError) as cm:
            read_key_file(path)
        self.assertNotIn("sk_live_REALLYsecret", str(cm.exception))

    def run_script(self, script, args, env_key=None, timeout=60):
        """Run a script with STRIPE_SECRET_KEY set to env_key, or unset when None."""
        env = {k: v for k, v in os.environ.items() if k != "STRIPE_SECRET_KEY"}
        if env_key is not None:
            env["STRIPE_SECRET_KEY"] = env_key
        return subprocess.run([sys.executable, os.path.join(HERE, script), "--base-url", self.base,
                               "--label", "fake"] + list(args),
                              capture_output=True, text=True, timeout=timeout, env=env)

    def run_cli(self, script, via="file", env_key=None):
        out = os.path.join(tempfile.mkdtemp(), "out.json")
        args = ["--out", out]
        if via == "file":
            args += ["--key-file", self.write("STRIPE_SECRET_KEY=%s\n" % FAKE_KEY)]
        else:
            env_key = FAKE_KEY
        run = self.run_script(script, args, env_key)
        with open(out) as f:
            recorded = f.read()
        return run, recorded

    def assert_key_never_recorded(self, script, via="file", env_key=None):
        run, recorded = self.run_cli(script, via, env_key)
        self.assertEqual(run.returncode, 0, run.stderr)
        for text in (run.stdout, run.stderr, recorded):
            self.assertNotIn(FAKE_KEY, text)
            self.assertNotIn(base64.b64encode((FAKE_KEY + ":").encode()).decode(), text)
        auth = "Basic " + base64.b64encode((FAKE_KEY + ":").encode()).decode()
        self.assertTrue(FakeStripe.seen)
        self.assertTrue(all(a == auth for _, _, a in FakeStripe.seen))

    def test_run_flow_key_file_is_used_and_never_recorded(self):
        self.assert_key_never_recorded("run_flow.py")

    def test_run_payments_key_file_is_used_and_never_recorded(self):
        self.assert_key_never_recorded("run_payments.py")

    def test_run_receipts_key_file_is_used_and_never_recorded(self):
        self.assert_key_never_recorded("run_receipts.py")

    def test_run_flow_env_key_is_used_and_never_recorded(self):
        self.assert_key_never_recorded("run_flow.py", via="env")

    def test_run_payments_env_key_is_used_and_never_recorded(self):
        self.assert_key_never_recorded("run_payments.py", via="env")

    def test_run_receipts_env_key_is_used_and_never_recorded(self):
        self.assert_key_never_recorded("run_receipts.py", via="env")

    def test_key_file_wins_over_the_env_var(self):
        self.assert_key_never_recorded("run_receipts.py", via="file", env_key="sk_test_other")

    def test_live_env_key_is_refused_without_echoing_it(self):
        run = self.run_script("run_flow.py", [], env_key="sk_live_REALLYsecret", timeout=30)
        self.assertEqual(run.returncode, 2)
        self.assertNotIn("sk_live_REALLYsecret", run.stdout + run.stderr)
        self.assertNotIn("Traceback", run.stderr)
        self.assertEqual(FakeStripe.seen, [])

    def test_no_key_names_both_sources_without_a_traceback(self):
        run = self.run_script("run_flow.py", [], env_key=None, timeout=30)
        self.assertEqual(run.returncode, 2)
        self.assertIn("STRIPE_SECRET_KEY", run.stderr)
        self.assertIn("--key-file", run.stderr)
        self.assertNotIn("Traceback", run.stderr)
        self.assertEqual(FakeStripe.seen, [])

    def test_a_key_on_the_command_line_is_not_accepted(self):
        run = self.run_script("run_flow.py", ["--key", "sk_test_x"], env_key=FAKE_KEY, timeout=30)
        self.assertEqual(run.returncode, 2)
        self.assertEqual(FakeStripe.seen, [])

    def test_missing_key_file_fails_without_a_traceback(self):
        run = self.run_script("run_flow.py", ["--key-file", "/nonexistent/.env"], timeout=30)
        self.assertEqual(run.returncode, 2)
        self.assertNotIn("Traceback", run.stderr)


if __name__ == "__main__":
    unittest.main()
