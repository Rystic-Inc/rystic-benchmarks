"""Compare recorded runs against Stripe's, step by step, and merge them into one JSON.

Takes the Stripe run plus one or more server runs written by run_payments.py,
run_flow.py or run_receipts.py, compares each step's result field by field,
prints a table and optionally writes the merged JSON with a verdict per step and
server. Timings, recorded requests and masked object ids are not compared. For
the billing flow, times are compared as offsets from the clock (periods from the
server's own clock time, the clock from the 1 Nov 2026 it was asked for), a step
that was not run scores as not matching, and the events step reports which of
Stripe's event types the server sent.

    python3 compare_payments.py results-stripe.json results-mock.json results-rystic.json --out results.json
"""
import argparse
import json
import re
from datetime import datetime, timezone

IGNORED = {"ms", "request", "waited_s"}
FLOW_START = 1793491200  # the frozen_time run_flow.py asks for: 1 Nov 2026 00:00 UTC
FLOW_IGNORED = {"polls", "latest_invoice_type"}
ID_PREFIXES = "cus|ch|pi|pm|py|re|txn|evt"  # the kinds run_receipts.py masks
MASKED = re.compile(r"^(%s)_…$" % ID_PREFIXES)
ID = re.compile(r"^(%s)_[A-Za-z0-9]+$" % ID_PREFIXES)


def load(path):
    with open(path) as f:
        return json.load(f)


def unit(report):
    """Return (word, list key, number key) for a payments or a receipts run."""
    return ("step", "steps", "step") if "steps" in report else ("call", "calls", "id")


def kind(report):
    if "steps" not in report:
        return "receipts"
    if report.get("flow") == "billing" or any("test clock" in (e.get("name") or "") for e in report["steps"]):
        return "billing flow"
    return "payments flow"


def entries(report):
    _, items, num = unit(report)
    return {str(e[num]): e for e in report.get(items, [])}


def flatten(value, prefix=""):
    if not isinstance(value, dict) or not value and prefix:
        return {prefix: value}
    out = {}
    for key, inner in value.items():
        if key in IGNORED:
            continue
        out.update(flatten(inner, prefix + "." + key if prefix else key))
    return out


def same(want, got):
    if isinstance(want, str) and isinstance(got, str):
        if MASKED.match(want) and ID.match(got) or MASKED.match(got) and ID.match(want):
            return want.split("_", 1)[0] == got.split("_", 1)[0]
    return want == got


def succeeded(result):
    status = result.get("status") if isinstance(result, dict) else None
    statuses = status if isinstance(status, list) else [status]
    return all(s is None or isinstance(s, int) and 200 <= s < 300 for s in statuses)


def compare_step(ref, got, views=(flatten, flatten)):
    if got is None:
        return {"verdict": "missing"}
    want, have = views[0](ref.get("result", {})), views[1](got.get("result", {}))
    missing = object()
    fields = sorted(k for k in set(want) | set(have) if not same(want.get(k, missing), have.get(k, missing)))
    verdict = {"verdict": "differs" if fields else "match"}
    if fields:
        verdict["fields"] = fields
        ok_ref, ok_got = succeeded(ref.get("result")), succeeded(got.get("result"))
        if ok_ref and not ok_got:
            verdict["note"] = "error where Stripe succeeded"
        elif ok_got and not ok_ref:
            verdict["note"] = "succeeded where Stripe returned an error"
    return verdict


def clock_time(report):
    """The frozen_time the run's clock answered with, else the one it asked for."""
    frozen = ((entries(report).get("2") or {}).get("result") or {}).get("frozen_time")
    return frozen if isinstance(frozen, int) else FLOW_START


def embedded(result):
    """Whether latest_invoice came back as an object; inferred for runs that did not record its type."""
    if "latest_invoice_type" in result:
        return result["latest_invoice_type"] == "object"
    if any(v is not None for v in (result.get("first_invoice") or {}).values()):
        return True
    return None


def offset(value, base):
    return value - base if isinstance(value, int) and not isinstance(value, bool) else value


def flow_view(frozen):
    """A billing-flow result as its observable answer: no ids, no polls, times as offsets."""
    def view(result):
        out = {}
        for key, value in flatten(result).items():
            if key in FLOW_IGNORED or key.endswith(".message"):
                continue
            if key in ("period_start", "period_end"):
                out[key + "_from_clock"] = offset(value, frozen)
            elif key == "frozen_time":
                out["frozen_time_from_requested"] = offset(value, FLOW_START)
            else:
                out[key] = value
        if "first_invoice" in result:
            out["latest_invoice_embedded"] = embedded(result)
        return out
    return view


def compare_events(want, have):
    have = have or {}
    sent = [t for t in want if t in have]
    verdict = {"event_types_sent": "%d of %d" % (len(sent), len(want))}
    for name, types in (("missing_types", [t for t in want if t not in have]),
                        ("extra_types", [t for t in have if t not in want]),
                        ("count_differs", [t for t in sent if want[t] != have[t]])):
        if types:
            verdict[name] = sorted(types)
    verdict["verdict"] = "differs" if len(verdict) > 1 else "match"
    return verdict


def compare_flow_step(ref, got, ref_frozen, got_frozen):
    if got is None:
        return {"verdict": "missing"}
    result = got.get("result") or {}
    if "not_run" in result:
        return {"verdict": "not run", "reason": result["not_run"]}
    want = ref.get("result") or {}
    if "event_types" in want:
        return compare_events(want["event_types"], result.get("event_types"))
    return compare_step(ref, got, (flow_view(ref_frozen), flow_view(got_frozen)))


def slug(label, taken):
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "server"
    key, n = base, 2
    while key in taken:
        key, n = "%s_%d" % (base, n), n + 1
    return key


def compare(stripe, servers):
    word, what = unit(stripe)[0], kind(stripe)
    keyed = {"stripe": stripe}
    for report in servers:
        if kind(report) != what:
            raise ValueError("%s is not a run of the same script as the Stripe run" % report.get("label"))
        keyed[slug(report.get("label", ""), set(keyed) | {"name"})] = report
    ref = entries(stripe)
    per, agrees = {}, {}
    for key, report in list(keyed.items())[1:]:
        got = entries(report)
        agrees[key] = 0
        for n, step in ref.items():
            if what == "billing flow":
                verdict = compare_flow_step(step, got.get(n), clock_time(stripe), clock_time(report))
            else:
                verdict = compare_step(step, got.get(n))
            per.setdefault(n, {"name": step.get("name")})[key] = verdict
            agrees[key] += verdict["verdict"] == "match"
    return {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "kind": what, "stripe_version": stripe.get("stripe_version"),
            "note": "Each %s's result is compared field by field with Stripe's; timings, recorded requests "
                    "and masked object ids are not compared." % word + (
                        " Ids are never compared and times only as offsets from the clock." if what == "billing flow" else ""),
            word + "s": len(ref), "agrees_with_stripe": agrees, "per_" + word: per, "servers": keyed}


def table(merged):
    word = "step" if "per_step" in merged else "call"
    per, keys = merged["per_" + word], list(merged["agrees_with_stripe"])
    labels = [merged["servers"][k].get("label", k) for k in keys]
    rows = [[word, "name"] + labels]
    for n, step in per.items():
        cells = []
        for k in keys:
            cells.append(cell(step[k]))
        rows.append([n, step["name"] or ""] + cells)
    total = merged[word + "s"]
    rows.append(["", "agrees with Stripe"] + ["%d/%d" % (merged["agrees_with_stripe"][k], total) for k in keys])
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows) + "\n"


def cell(v):
    if v["verdict"] == "not run":
        return "not run: " + v["reason"]
    if "event_types_sent" in v:
        if v["verdict"] == "match":
            return "match (%s)" % v["event_types_sent"]
        parts = ["sent %s Stripe event types" % v["event_types_sent"]]
        for name, label in (("missing_types", "missing"), ("extra_types", "extra"), ("count_differs", "counts differ")):
            if v.get(name):
                parts.append(label + " " + ", ".join(v[name]))
        return "differs: " + "; ".join(parts)
    return v["verdict"] if v["verdict"] != "differs" else "differs: " + ", ".join(v["fields"])


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("stripe", help="the Stripe run's results JSON")
    parser.add_argument("servers", nargs="+", help="one or more server runs' results JSON")
    parser.add_argument("--out", help="write the merged JSON here")
    args = parser.parse_args()
    try:
        merged = compare(load(args.stripe), [load(p) for p in args.servers])
    except ValueError as e:
        parser.error(str(e))
    print(table(merged), end="")
    if args.out:
        with open(args.out, "w") as f:
            f.write(json.dumps(merged, indent=2) + "\n")
        print("wrote " + args.out)


if __name__ == "__main__":
    main()
