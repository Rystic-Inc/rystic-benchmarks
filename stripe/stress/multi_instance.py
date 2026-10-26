"""K flows at once, each against its own simulator instance, scored as concurrent_flows.py scores them.

Start the instances first, e.g. 50 of them:

    for i in $(seq -w 0 49); do rystic run -version 0.0.18 -name c$i -d --port 0 stripe-twin; done

then run one flow on each; the ports come from `rystic ps --json`:

    STRIPE_SECRET_KEY=sk_test_local python3 multi_instance.py --flow billing --prefix c --poll-s 0.05 --out own.json

and stop them with `rystic stop c00 c01 ...`.
"""
import argparse
import json
import subprocess

import concurrent_flows as cf


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--flow", choices=("billing", "payments"), default="billing")
    ap.add_argument("--prefix", required=True, help="run one flow on each running instance whose name starts with this")
    cf.run_receipts.add_key_args(ap)
    ap.add_argument("--poll-s", type=float, default=cf.run_flow.POLL_S,
                    help="seconds between clock checks in the billing flow (default: the harness's 1)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    key = cf.run_receipts.key_from_args(ap, a)
    cf.run_flow.POLL_S = a.poll_s
    ps = json.loads(subprocess.run(["rystic", "ps", "--json"], capture_output=True, text=True, check=True).stdout)
    ports = sorted(i["port"] for i in ps["data"]["instances"]
                   if i["name"].startswith(a.prefix) and i["status"] == "running")
    if not ports:
        ap.error("no running instance whose name starts with %r" % a.prefix)
    bases = ["http://127.0.0.1:%d" % p for p in ports]
    r = cf.run_clients(a.flow, bases, key, cf.load_ref(a.flow), "%s-own" % a.flow)
    out = {"flow": a.flow, "instances": len(ports), "poll_s": a.poll_s, **r}
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps({k: v for k, v in out.items() if k != "per_flow"}))


if __name__ == "__main__":
    main()
