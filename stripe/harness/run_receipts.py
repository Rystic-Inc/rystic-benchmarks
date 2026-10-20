"""Send the same six Stripe API calls to one server and record what came back.

Covers retrieve-after-create, list pagination, a declined card, idempotency-key
reuse, retrieve-after-delete and a missing customer, written as comparable JSON.

    STRIPE_SECRET_KEY=sk_test_x python3 run_receipts.py --base-url http://127.0.0.1:12111 --label stripe-mock
    STRIPE_SECRET_KEY=sk_test_x python3 run_receipts.py --base-url http://127.0.0.1:PORT --label rystic
    python3 run_receipts.py --base-url https://api.stripe.com --key-file .env --label stripe

The key comes from --key-file, else the STRIPE_SECRET_KEY environment variable,
and must be a test-mode key. Against Stripe, use your own test-mode secret key:
the run creates customers and a declined charge in that account, and deletes the
first customer.
"""
import argparse
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

API_VERSION = "2026-06-24.dahlia"
# Object ids differ per server and run; recorded paths keep their shape only.
ID_IN_PATH = re.compile(r"([/=])(cus|ch|pi|pm|sub|in|clock|price|prod|evt|re|si)_(?!doesnotexist)[A-Za-z0-9]+")
TEST_KEY = re.compile(r"^(sk|rk)_test_[A-Za-z0-9_]+$")


def read_key_file(path):
    """Return STRIPE_SECRET_KEY, else the first sk_test_/rk_test_ value, from a dotenv-style file.

    Errors name the file, never a value in it, and a live key is refused.
    """
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError as e:
        raise ValueError("cannot read key file %s (%s)" % (path, e.strerror))
    named, first = None, None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if name == "STRIPE_SECRET_KEY":
            named = value
            break
        if first is None and TEST_KEY.match(value):
            first = value
    key = named or first
    if not key:
        raise ValueError("no STRIPE_SECRET_KEY or sk_test_/rk_test_ value in %s" % path)
    if not TEST_KEY.match(key):
        raise ValueError("the key in %s is not a test-mode sk_test_/rk_test_ key; refusing it" % path)
    return key


def add_key_args(parser):
    parser.add_argument("--key-file", help="dotenv-style file holding STRIPE_SECRET_KEY "
                                           "(default: the STRIPE_SECRET_KEY environment variable); never printed")


def key_from_args(parser, args):
    """The test-mode key from --key-file, else $STRIPE_SECRET_KEY.

    A missing, unreadable or live key exits with an error that omits every value.
    """
    if args.key_file:
        try:
            return read_key_file(args.key_file)
        except ValueError as e:
            parser.error(str(e))
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        parser.error("no key: set STRIPE_SECRET_KEY or pass --key-file <path>")
    if not TEST_KEY.match(key):
        parser.error("STRIPE_SECRET_KEY is not a test-mode sk_test_/rk_test_ key; refusing it")
    return key


class Client:
    def __init__(self, base_url, key, version=API_VERSION):
        self.base_url = base_url.rstrip("/")
        self.version = version
        token = base64.b64encode((key + ":").encode()).decode()
        self.auth = "Basic " + token
        self.sent = []

    def call(self, method, path, params=None, headers=None):
        """Send one request; return (status, parsed JSON body or None)."""
        self.sent.append(method + " " + ID_IN_PATH.sub(r"\1\2_…", path))
        data = urllib.parse.urlencode(params).encode() if params else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Authorization", self.auth)
        req.add_header("Stripe-Version", self.version)
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as err:
            status, raw = err.code, err.read()
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, None

    def take_sent(self):
        sent, self.sent = self.sent, []
        return sent


def error_of(body):
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return body["error"]
    return None


def field(body, name):
    return body.get(name) if isinstance(body, dict) else None


def run(client):
    calls = []

    def record(name, result):
        calls.append({"id": len(calls) + 1, "name": name,
                      "request": client.take_sent(), "result": result})

    # 1. Create a customer, then read it back.
    _, created = client.call("POST", "/v1/customers",
                             {"email": "jenny.rosen@example.com", "name": "Jenny Rosen"})
    customer_id = field(created, "id") or "cus_missing"
    status, body = client.call("GET", "/v1/customers/" + customer_id)
    record("retrieve a customer after creating it",
           {"status": status, "email": field(body, "email")})

    # 2. Create four more customers, then list three.
    for letter in "abcd":
        client.call("POST", "/v1/customers", {"email": letter + "@example.com"})
    status, body = client.call("GET", "/v1/customers?limit=3")
    data = field(body, "data")
    record("list customers with limit=3",
           {"status": status, "data_count": len(data) if isinstance(data, list) else None,
            "has_more": field(body, "has_more")})

    # 3. Charge a card token that Stripe always declines.
    status, body = client.call("POST", "/v1/charges",
                               {"amount": "2000", "currency": "usd",
                                "source": "tok_chargeDeclined"})
    err = error_of(body)
    if err:
        result = {"status": status, "error_code": err.get("code"),
                  "decline_code": err.get("decline_code")}
    else:
        result = {"status": status, "charge_status": field(body, "status")}
    record("charge a card that is declined", result)

    # 4. Reuse one idempotency key with a different request body.
    key = {"Idempotency-Key": str(uuid.uuid4())}
    client.call("POST", "/v1/customers", {"email": "first@example.com"}, key)
    status, body = client.call("POST", "/v1/customers", {"email": "second@example.com"}, key)
    err = error_of(body)
    if err:
        result = {"status": status, "error_type": err.get("type")}
    else:
        result = {"status": status, "email": field(body, "email")}
    record("reuse an idempotency key with different parameters", result)

    # 5. Delete the first customer, then read it back.
    client.call("DELETE", "/v1/customers/" + customer_id)
    status, body = client.call("GET", "/v1/customers/" + customer_id)
    record("retrieve a customer after deleting it",
           {"status": status, "deleted": field(body, "deleted"),
            "email": field(body, "email")})

    # 6. Retrieve a customer that was never created.
    status, body = client.call("GET", "/v1/customers/cus_doesnotexist")
    err = error_of(body)
    if err:
        result = {"status": status, "error_code": err.get("code")}
    else:
        result = {"status": status, "id": field(body, "id")}
    record("retrieve a customer that does not exist", result)
    return calls


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True)
    add_key_args(parser)
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", help="output file (default: stdout)")
    parser.add_argument("--stripe-version", default=API_VERSION, help="Stripe-Version header sent on every call")
    args = parser.parse_args()

    key = key_from_args(parser, args)
    run_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    calls = run(Client(args.base_url, key, args.stripe_version))
    report = {"label": args.label, "base_url": args.base_url, "stripe_version": args.stripe_version,
              "run_at": run_at, "calls": calls}
    text = json.dumps(report, indent=2) + "\n"
    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
        print("wrote " + args.out)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
