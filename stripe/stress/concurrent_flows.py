"""Run K copies of the billing or payments flow at once against one server, scoring each against Stripe's solo run.

Each copy is the harness's own flow (../harness/run_flow.py, run_payments.py) on its own connection, all started
at the same moment, and each is scored with compare_payments.compare against the 20 Oct 2026 Stripe run in
../results/2026-10-20/runs/. The client counts every response, so a refused request can be told apart from a wrong
answer. Stripe's SDKs don't retry a `rate_limit` 429; with --retries N each 429 is sent again up to N times, with
jittered backoff. The billing flow waits up to 900 s for its clock, not the harness's 120 s, so a slow advance is
measured rather than cut off; `would_time_out_in_harness` counts the flows whose clock took longer than 120 s.
Each billing flow deletes its test clock and archives its price and product at the end, as run_flow.py does.

    python3 concurrent_flows.py --flow billing --levels 1,10,50 --retries 10 \\
        --base-url https://api.stripe.com --key-file ~/stripe.env --out billing-stripe.json
    STRIPE_SECRET_KEY=sk_test_local python3 concurrent_flows.py --flow billing --levels 1,10,50 \\
        --poll-s 0.05 --reset-between --base-url http://127.0.0.1:PORT --out billing-rystic.json
"""
import argparse
import json
import os
import random
import statistics
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "harness"))
import compare_payments  # noqa: E402
import run_flow  # noqa: E402
import run_payments  # noqa: E402
import run_receipts  # noqa: E402

REF = os.path.join(HERE, "..", "results", "2026-10-20", "runs")
HARNESS_CLOCK_WAIT_S = run_flow.CLOCK_WAIT_S
run_flow.CLOCK_WAIT_S = 900
RETRIES = 0  # set by --retries


def load_ref(kind):
    return compare_payments.load(os.path.join(REF, "%s-stripe.json" % ("flow" if kind == "billing" else "payments")))


class Counting(run_receipts.Client):
    """The harness's client, counting each response by status, and retrying a 429 up to RETRIES times."""

    def __init__(self, base, key, tally):
        super().__init__(base, key)
        self.tally, self.mine = tally, {}

    def call(self, method, path, params=None, headers=None):
        for attempt in range(RETRIES + 1):
            s, body = super().call(method, path, params, headers)
            tag = s
            if s == 429:
                tag = "429 " + (((body or {}).get("error") or {}).get("code") or "rate_limit")
            for d in (self.mine, self.tally["codes"]):
                with self.tally["lock"]:
                    d[tag] = d.get(tag, 0) + 1
            if s != 429:
                break
            if attempt < RETRIES:  # Stripe acts on nothing it answers 429, so sending it again is safe
                with self.tally["lock"]:
                    self.tally["retries"] = self.tally.get("retries", 0) + 1
                time.sleep(min(8.0, 0.5 * 2 ** attempt) * (0.5 + random.random()))
            else:
                with self.tally["lock"]:
                    self.tally["gave_up"] = self.tally.get("gave_up", 0) + 1
        return s, body


def one(kind, base, key, tag, tally, ref):
    """Run one flow and score it: seconds, steps agreeing with Stripe, the steps that differ, responses."""
    c = Counting(base, key, tally)
    t = time.monotonic()
    if kind == "billing":
        f = run_flow.Flow(c, tag)
        try:
            f.run()
        finally:
            total = time.monotonic() - t
            f.cleanup()
        report = {"label": tag, "flow": "billing", "stripe_version": run_receipts.API_VERSION, "steps": f.steps}
    else:
        f = run_payments.Payments(c)
        f.run()
        total = time.monotonic() - t
        report = {"label": tag, "stripe_version": run_receipts.API_VERSION, "steps": f.steps}
    merged = compare_payments.compare(ref, [report])
    label = list(merged["agrees_with_stripe"])[0]
    differs = [n for n, v in merged["per_step"].items() if v[label]["verdict"] != "match"]
    out = {"seconds": round(total, 2), "agrees": merged["agrees_with_stripe"][label], "steps": len(ref["steps"]),
           "differs": differs, "responses": c.mine,
           "refused": sum(n for s, n in c.mine.items() if isinstance(s, str))}
    if kind == "billing":
        adv = f.steps[4]
        out["clock_wait_s"] = round(adv["ms"] / 1000, 1)
        out["clock_ready"] = adv["result"].get("clock_status") == "ready"
    else:
        out["bt_wait_s"] = f.steps[2].get("waited_s") if len(f.steps) > 2 else None
    return out


def summarize(kind, k, res, tally, wall, nsteps):
    secs = sorted(r["seconds"] for r in res if r.get("seconds") is not None)
    out = {"flows": k, "wall_s": round(wall, 1), "retries": tally.get("retries", 0), "gave_up": tally.get("gave_up", 0),
           "flow_s_median": round(statistics.median(secs), 2) if secs else None,
           "flow_s_max": round(secs[-1], 2) if secs else None,
           "all_steps_match": sum(1 for r in res if r["agrees"] == nsteps),
           "agrees_hist": {str(a): sum(1 for r in res if r["agrees"] == a) for a in sorted({r["agrees"] for r in res})},
           "differing_steps": {}, "flows_with_refusals": sum(1 for r in res if r.get("refused")),
           "responses": {str(s): n for s, n in sorted(tally["codes"].items(), key=lambda x: str(x[0]))},
           "errors": [r["error"] for r in res if r.get("error")]}
    for r in res:
        for n in r["differs"]:
            out["differing_steps"][n] = out["differing_steps"].get(n, 0) + 1
    if kind == "billing":
        waits = sorted(r["clock_wait_s"] for r in res if r.get("clock_wait_s") is not None)
        out["clock_wait_s_median"] = round(statistics.median(waits), 1) if waits else None
        out["clock_wait_s_max"] = waits[-1] if waits else None
        out["would_time_out_in_harness"] = sum(1 for w in waits if w > HARNESS_CLOCK_WAIT_S)
    else:
        bts = sorted(r["bt_wait_s"] for r in res if r.get("bt_wait_s") is not None)
        out["bt_wait_s_median"] = statistics.median(bts) if bts else None
    out["per_flow"] = res
    return out


def run_clients(kind, bases, key, ref, tag):
    """Start one flow per base URL at the same moment and wait for all of them."""
    tally = {"lock": threading.Lock(), "codes": {}}
    res = [None] * len(bases)
    go = threading.Barrier(len(bases))

    def run(i):
        go.wait()
        try:
            res[i] = one(kind, bases[i], key, "%s-%02d" % (tag, i), tally, ref)
        except Exception as e:  # a flow that crashed still counts
            res[i] = {"error": "%s: %s" % (type(e).__name__, e), "agrees": 0, "steps": len(ref["steps"]),
                      "seconds": None, "differs": ["crashed"], "refused": 0}

    t0 = time.monotonic()
    th = [threading.Thread(target=run, args=(i,)) for i in range(len(bases))]
    [x.start() for x in th]
    [x.join() for x in th]
    return summarize(kind, len(bases), res, tally, time.monotonic() - t0, len(ref["steps"]))


def reset(base, key):
    """POST /_rystic/reset: the simulator's own call that empties it. Stripe has none."""
    req = urllib.request.Request(base.rstrip("/") + "/_rystic/reset", data=b"", method="POST",
                                 headers={"Authorization": "Bearer " + key})
    urllib.request.urlopen(req, timeout=30).read()


def main():
    global RETRIES
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--flow", choices=("billing", "payments"), required=True)
    ap.add_argument("--base-url", required=True)
    run_receipts.add_key_args(ap)
    ap.add_argument("--levels", default="1,10,50", help="how many flows to start at once, comma-separated")
    ap.add_argument("--retries", type=int, default=0, help="send each 429 again up to N times, with backoff")
    ap.add_argument("--poll-s", type=float, default=run_flow.POLL_S,
                    help="seconds between clock checks in the billing flow (default: the harness's 1)")
    ap.add_argument("--reset-between", action="store_true",
                    help="simulator only: POST /_rystic/reset before each level, so each starts empty")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    key = run_receipts.key_from_args(ap, a)
    RETRIES = a.retries
    run_flow.POLL_S = a.poll_s
    ref = load_ref(a.flow)
    out = {"flow": a.flow, "base_url": a.base_url, "retries_per_429": a.retries, "poll_s": a.poll_s,
           "reset_between": a.reset_between, "reference": "results/2026-10-20/runs (Stripe, 20 Oct 2026)", "levels": []}
    for k in (int(x) for x in a.levels.split(",")):
        if a.reset_between:
            reset(a.base_url, key)
        r = run_clients(a.flow, [a.base_url] * k, key, ref, "%s-k%d" % (a.flow, k))
        out["levels"].append(r)
        with open(a.out, "w") as f:
            json.dump(out, f, indent=1)
        print(json.dumps({x: y for x, y in r.items() if x != "per_flow"}), flush=True)


if __name__ == "__main__":
    main()
