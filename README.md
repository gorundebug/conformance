# ServiceLib cross-language conformance

This directory contains black-box semantic tests shared by all ServiceLib
language ports. A test describes observable behavior once, runs the equivalent
generated graph in Go, C++, Python and Rust, and compares normalized results.
Go is currently the reference implementation.

## Quickstart

Only this repository needs to be cloned by hand. `quickstart.sh` clones the
sibling repos it depends on (if missing) and runs the full conformance
matrix, so anyone can independently verify that Go/C++/Python/Rust are
semantically equivalent:

```bash
git clone https://github.com/gorundebug/conformance.git
cd conformance
./quickstart.sh
```

Requires `git`, `docker` (with the `compose` plugin) and `python3`. Run only
one suite by forwarding it after `--`:

```bash
./quickstart.sh -- tracing
./quickstart.sh -- metrics
```

Use `./quickstart.sh --clone-only` to just fetch the sibling repos without
running anything. For diagnosing a single language, skip this script and call
the runners directly (see below), e.g. `python3 tracing/run.py --language cpp`.

## Distributed tracing

The first test starts each language example and an isolated Jaeger instance,
sends the same HTTP request with a sampled W3C `traceparent`, fetches that exact
distributed trace, and compares the ServiceLib span trees.

The comparison deliberately removes random IDs, timestamps, durations and
runtime-specific transport spans. The semantic contract includes:

- ServiceLib operation names;
- parent/child relationships, including the cross-service HTTP → gRPC trace;
- service ownership;
- observable attributes such as `stream`, `from`, `to`, `type` and `endpoint`.

Run the complete matrix from this directory:

```bash
make tracing
```

Run one or more ports while diagnosing a failure:

```bash
python3 tracing/run.py --language cpp
python3 tracing/run.py --language go --language python
```

Skip image compilation when the example images are already current:

```bash
python3 tracing/run.py --skip-build
```

Keep containers after the test:

```bash
python3 tracing/run.py --keep
```

The runner continues after an individual language fails, then exits non-zero if
any port failed to start, export a trace, or match Go. Diagnostic files are
written to `.artifacts/tracing/`:

- `<language>.trace.raw.json` — the Jaeger response;
- `<language>.trace.json` — the normalized semantic tree;
- `comparison.diff` — cross-language difference;
- `summary.json` — machine-readable result for the complete run.

The artifacts are intentionally ignored by Git. A failing comparison is the
expected way for this suite to expose a semantic gap; it must not be hidden by
adding language-specific normalization.

## Metrics

The metrics test starts the same four examples, sends one deterministic order
request, reads `/metrics` from both services and compares the ServiceLib metric
contract against Go:

- metric names;
- the complete set of framework labels and their values;
- exact values for deterministic counters and idle gauges;
- presence and labels, but not values, for duration sums;
- histogram buckets are excluded because bucket representation belongs to the
  production metrics backend rather than to the ServiceLib contract.
- untouched zero counters and histograms are excluded because some Prometheus
  backends expose registered instruments eagerly while others omit them until
  the first observation; zero-valued gauges remain part of the contract.

Only exporter-owned labels such as `otel_scope_*` are removed. Framework labels
are never hidden by normalization.

Run the full matrix:

```bash
make metrics
```

Run selected ports or reuse current images:

```bash
python3 metrics/run.py --language go --language cpp
python3 metrics/run.py --skip-build
```

Artifacts are written to `.artifacts/metrics/`, including the raw Prometheus
responses, normalized JSON for each language, a comparison diff and a summary.

Run both conformance suites:

```bash
make all
```
