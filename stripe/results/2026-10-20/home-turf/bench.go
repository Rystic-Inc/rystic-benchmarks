// bench measures per-call latency of one Stripe-shaped server: a GET of one
// customer and a POST that creates one, each sent sequentially over one
// keep-alive connection, after a warmup. Prints JSON with median, p95 and mean.
//
//	go run bench.go -base-url http://127.0.0.1:12111 -n 5000 -label stripe-mock
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strings"
	"time"
)

const version = "2026-06-24.dahlia"

var client = &http.Client{Transport: &http.Transport{MaxIdleConnsPerHost: 1, DisableCompression: true}}

func call(base, method, path string, form url.Values) (int, []byte, time.Duration) {
	var body io.Reader
	if form != nil {
		body = strings.NewReader(form.Encode())
	}
	req, _ := http.NewRequest(method, base+path, body)
	req.Header.Set("Authorization", "Bearer sk_test_hometurf")
	req.Header.Set("Stripe-Version", version)
	if form != nil {
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	}
	t := time.Now()
	resp, err := client.Do(req)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	b, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	return resp.StatusCode, b, time.Since(t)
}

type stats struct {
	N        int     `json:"n"`
	Status   int     `json:"status"`
	MedianMS float64 `json:"median_ms"`
	P95MS    float64 `json:"p95_ms"`
	P99MS    float64 `json:"p99_ms"`
	MeanMS   float64 `json:"mean_ms"`
	TotalS   float64 `json:"total_s"`
}

func measure(n, warmup int, f func() (int, time.Duration)) stats {
	for i := 0; i < warmup; i++ {
		f()
	}
	d := make([]float64, n)
	var sum float64
	status := 0
	start := time.Now()
	for i := range d {
		s, t := f()
		status = s
		d[i] = float64(t.Nanoseconds()) / 1e6
		sum += d[i]
	}
	total := time.Since(start).Seconds()
	sort.Float64s(d)
	q := func(p float64) float64 { return round(d[int(p*float64(n-1))]) }
	return stats{N: n, Status: status, MedianMS: q(0.5), P95MS: q(0.95), P99MS: q(0.99), MeanMS: round(sum / float64(n)), TotalS: round(total)}
}

func round(x float64) float64 { return float64(int(x*1000+0.5)) / 1000 }

func main() {
	base := flag.String("base-url", "", "server base URL")
	n := flag.Int("n", 5000, "measured calls per operation")
	warmup := flag.Int("warmup", 500, "unmeasured calls first")
	label := flag.String("label", "", "label for the output")
	flag.Parse()

	s, b, _ := call(*base, "POST", "/v1/customers", url.Values{"email": {"jenny.rosen@example.com"}})
	var cus struct{ ID string }
	json.Unmarshal(b, &cus)
	if s != 200 || cus.ID == "" {
		fmt.Fprintf(os.Stderr, "setup failed: %d %s\n", s, b)
		os.Exit(1)
	}
	get := measure(*n, *warmup, func() (int, time.Duration) {
		s, _, t := call(*base, "GET", "/v1/customers/"+cus.ID, nil)
		return s, t
	})
	post := measure(*n, *warmup, func() (int, time.Duration) {
		s, _, t := call(*base, "POST", "/v1/customers", url.Values{"email": {"jenny.rosen@example.com"}, "name": {"Jenny Rosen"}})
		return s, t
	})
	out, _ := json.MarshalIndent(map[string]any{
		"label": *label, "base_url": *base, "at": time.Now().UTC().Format(time.RFC3339),
		"get_customer": get, "post_customer": post,
	}, "", "  ")
	fmt.Println(string(out))
}
