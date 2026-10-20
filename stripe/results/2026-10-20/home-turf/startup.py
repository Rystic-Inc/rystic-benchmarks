"""Measure startup: process start to the first 200 from GET /v1/customers.

Each trial starts one fresh server, polls every 2 ms until it answers 200,
records the elapsed time, then stops it. Prints JSON per target.

    python3 startup.py --trials 10 --targets mock-docker,mock-native,rystic-run-0.0.18,rystic-run-0.0.17

mock-native and the rystic-raw-* targets need ./stripe-mock and the bare
simulator builds ./stripe-sim-018 and ./stripe-sim-017 next to this file.
"""
import argparse
import json
import os
import re
import socket
import statistics
import subprocess
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGE = "stripe/stripe-mock:v0.206.0"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def ready(base):
    req = urllib.request.Request(base + "/v1/customers?limit=1", headers={"Authorization": "Bearer sk_test_hometurf"})
    try:
        with urllib.request.urlopen(req, timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def wait(base, t0, limit=60):
    while time.monotonic() - t0 < limit:
        if ready(base):
            return time.monotonic() - t0
        time.sleep(0.002)
    raise RuntimeError("never ready: " + base)


def mock_docker(i):
    port = free_port()
    name = "hometurf-mock-%d" % i
    t0 = time.monotonic()
    subprocess.run(["docker", "run", "--rm", "-d", "--name", name, "-p", "%d:12111" % port, IMAGE],
                   check=True, stdout=subprocess.DEVNULL)
    try:
        return wait("http://127.0.0.1:%d" % port, t0)
    finally:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def native(cmd, env=None):
    port = free_port()
    argv = [a.replace("{port}", str(port)) for a in cmd]
    e = dict(os.environ, **{k: v.replace("{port}", str(port)) for k, v in (env or {}).items()})
    t0 = time.monotonic()
    p = subprocess.Popen(argv, env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return wait("http://127.0.0.1:%d" % port, t0)
    finally:
        p.terminate()
        p.wait()


def mock_native(i):
    return native([os.path.join(HERE, "stripe-mock"), "-http-port", "{port}"])


def rystic_raw(binary):
    return lambda i: native([os.path.join(HERE, binary)], {"RYSTIC_ADDR": "127.0.0.1:{port}"})


def rystic_run(version):
    return lambda i: _rystic_run(version, i)


def _rystic_run(version, i):
    name = "hometurf-%d" % i
    t0 = time.monotonic()
    out = subprocess.run(["rystic", "run", "-version", version, "-d", "--port", "0", "-name", name, "stripe-twin"],
                         check=True, capture_output=True, text=True, cwd="/tmp").stdout
    try:
        base = re.search(r"http://localhost:\d+", out).group(0)
        return wait(base, t0)
    finally:
        subprocess.run(["rystic", "stop", name], cwd="/tmp", capture_output=True)
        subprocess.run(["rystic", "rm", name], cwd="/tmp", capture_output=True)


TARGETS = {"mock-docker": mock_docker, "mock-native": mock_native,
           "rystic-run-0.0.18": rystic_run("0.0.18"), "rystic-run-0.0.17": rystic_run("0.0.17"),
           "rystic-raw-0.0.18": rystic_raw("stripe-sim-018"), "rystic-raw-0.0.17": rystic_raw("stripe-sim-017")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--targets", default=",".join(TARGETS))
    args = ap.parse_args()
    out = {}
    for t in args.targets.split(","):
        runs = [round(TARGETS[t](i) * 1000, 1) for i in range(args.trials)]
        out[t] = {"trials_ms": runs, "median_ms": statistics.median(runs), "min_ms": min(runs), "max_ms": max(runs)}
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
