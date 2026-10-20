#!/usr/bin/env bash
# Per-call latency of each local target, with the Rystic targets pinned to v0.0.18 and v0.0.17.
# Needs in this directory: bench (go build bench.go), stripe-mock (go install github.com/stripe/stripe-mock@v0.206.0)
# and the bare simulator builds stripe-sim-018 and stripe-sim-017; the rystic run targets need only the rystic CLI.
set -euo pipefail
cd "$(dirname "$0")"
N=${N:-5000}
OUT=${OUT:-.}
wait_up() { for _ in $(seq 1 500); do curl -sf -H 'Authorization: Bearer sk_test_x' "$1/v1/customers?limit=1" >/dev/null && return; sleep 0.02; done; echo "not up: $1"; exit 1; }

./stripe-mock -http-port 12301 >/dev/null 2>&1 & P=$!
wait_up http://127.0.0.1:12301; ./bench -base-url http://127.0.0.1:12301 -n $N -label "stripe-mock v0.206.0 native" > $OUT/latency-mock-native.json; kill $P; wait $P 2>/dev/null || true

docker run --rm -d --name hometurf-lat -p 12302:12111 stripe/stripe-mock:v0.206.0 >/dev/null
wait_up http://127.0.0.1:12302; ./bench -base-url http://127.0.0.1:12302 -n $N -label "stripe-mock v0.206.0 docker" > $OUT/latency-mock-docker.json; docker rm -f hometurf-lat >/dev/null

for v in 018 017; do
  RYSTIC_ADDR=127.0.0.1:12303 ./stripe-sim-$v >/dev/null 2>&1 & P=$!
  wait_up http://127.0.0.1:12303; ./bench -base-url http://127.0.0.1:12303 -n $N -label "stripe sim, source build at v0.0.${v#0} release commit" > $OUT/latency-rystic-raw-0.0.${v#0}.json; kill $P; wait $P 2>/dev/null || true
done

for v in 0.0.18 0.0.17; do
  URL=$(cd /tmp && rystic run -version $v -d --port 0 -name hometurf-lat stripe-twin | grep -o 'http://localhost:[0-9]*')
  wait_up "$URL"; ./bench -base-url "$URL" -n $N -label "stripe-twin v$v via rystic run" > $OUT/latency-rystic-run-$v.json
  (cd /tmp && rystic stop hometurf-lat >/dev/null && rystic rm hometurf-lat >/dev/null)
done
