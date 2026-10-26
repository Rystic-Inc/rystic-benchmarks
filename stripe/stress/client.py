"""The HTTP client the stress scripts share: one keep-alive connection per thread, 429s counted and retried.

Keys are loaded the harness's way (../harness/run_receipts.py): --key-file or $STRIPE_SECRET_KEY, test mode only,
never printed.
"""
import http.client
import json
import os
import random
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "harness"))
from run_receipts import API_VERSION, add_key_args, key_from_args  # noqa: E402,F401

START = int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp())  # every test clock starts here, as in run_flow.py
PER_CLOCK = 3  # Stripe's limit of customers per test clock
TRIES = 10  # attempts per request: a 429 is sent again up to nine times


class Client:
    """One keep-alive connection per thread; counts every request and every 429 by Stripe's stated reason."""

    def __init__(self, base, key):
        u = urllib.parse.urlparse(base)
        self.https, self.host, self.port = u.scheme == "https", u.hostname, u.port
        self.key = key
        self.local = threading.local()
        self.lock = threading.Lock()
        self.r429 = {}
        self.requests = 0

    def conn(self):
        c = getattr(self.local, "c", None)
        if c is None:
            cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
            c = self.local.c = cls(self.host, self.port, timeout=60)
        return c

    def raw(self, method, path, params=None):
        body = urllib.parse.urlencode(params, doseq=True) if params else None
        headers = {"Authorization": "Bearer " + self.key, "Stripe-Version": API_VERSION}
        if body:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        for attempt in range(2):
            try:
                c = self.conn()
                c.request(method, path, body=body, headers=headers)
                r = c.getresponse()
                data = r.read()
                break
            except (http.client.HTTPException, OSError):
                self.local.c = None
                if attempt:
                    raise
        with self.lock:
            self.requests += 1
            if r.status == 429:
                reason = r.getheader("Stripe-Rate-Limited-Reason") or (
                    "lock_timeout" if b"lock_timeout" in data else "unlabelled")
                self.r429[reason] = self.r429.get(reason, 0) + 1
        try:
            js = json.loads(data) if data else {}
        except ValueError:
            js = {}
        return r.status, js

    def call(self, method, path, params=None, tries=TRIES):
        """Send a request, sending it again after a 429 with jittered backoff, up to `tries` times in all."""
        for i in range(tries):
            s, js = self.raw(method, path, params)
            if s != 429:
                return s, js
            if i < tries - 1:
                time.sleep(min(4.0, 0.25 * 2 ** i) * (0.5 + random.random()))
        return s, js


def ok(s):
    return isinstance(s, int) and 200 <= s < 300


def need(s, js, what):
    """The body of a 2xx answer, else a RuntimeError naming the step, the status and Stripe's message."""
    if not ok(s):
        raise RuntimeError("%s: %s %s" % (what, s, ((js or {}).get("error") or {}).get("message")))
    return js
