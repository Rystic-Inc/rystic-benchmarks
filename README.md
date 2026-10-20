# rystic-benchmarks

Reproducible comparisons of [Rystic](https://www.rystic.ai)'s API simulators against the real APIs they simulate, and against other mocks of those APIs.

Each comparison sends the same calls to the real service, to Rystic's simulator and to the alternatives, with the real service as referee: every answer is scored against the real API's answer. Each comparison lives in its own folder and holds:

- the harness that ran it, with its tests;
- the results, dated, with object ids masked;
- a README listing the pinned versions, how to reproduce the run and the caveats.

## Comparisons

| Folder | API | Compared | Recorded |
|---|---|---|---|
| [`stripe/`](stripe/) | Stripe | Stripe test mode (referee), Rystic stripe-twin v0.0.18 and v0.0.17, stripe-mock v0.206.0 | 20 Oct 2026 |

## License

MIT; see [LICENSE](LICENSE).
