"""Count an account's test clocks by status once a minute until none is advancing. Read-only.

Run it as a stress run ends to see how long the clock advances it sent keep going after it has stopped.

    python3 watch_clocks.py --base-url https://api.stripe.com --key-file ~/stripe.env --out clocks.json
"""
import argparse
import collections
import json
import time
from datetime import datetime

import client


def list_clocks(c):
    out, after = [], None
    while True:
        s, page = c.call("GET", "/v1/test_helpers/test_clocks?limit=100" + ("&starting_after=" + after if after else ""))
        if s != 200:
            raise RuntimeError("listing test clocks: %s %s" % (s, ((page or {}).get("error") or {}).get("message")))
        data = page.get("data") or []
        out += data
        if not page.get("has_more") or not data:
            return out
        after = data[-1]["id"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default="https://api.stripe.com")
    client.add_key_args(ap)
    ap.add_argument("--every-s", type=float, default=60)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    c = client.Client(a.base_url, client.key_from_args(ap, a))
    log = {"timeline": []}
    while True:
        clocks = list_clocks(c)
        status = collections.Counter(k.get("status") for k in clocks)
        row = {"local_time": datetime.now().astimezone().isoformat(timespec="seconds"), "clocks": len(clocks), **status}
        log["timeline"].append(row)
        with open(a.out, "w") as f:
            json.dump(log, f, indent=1)
        print(json.dumps(row), flush=True)
        if not status.get("advancing"):
            return
        time.sleep(a.every_s)


if __name__ == "__main__":
    main()
