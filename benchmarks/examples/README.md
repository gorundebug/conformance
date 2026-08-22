# Cross-language example benchmark

This embedded conformance toolkit measures the same Order Service → Inventory Service request path
in the generated Go, userver C++, Boost C++, Python and Rust examples, plus hand-written native
baselines for every language that do not use ServiceLib.

Each language is measured separately. Both service containers receive the same
CPU quota and the k6 load generator has its own quota, so lack of client CPU is
less likely to be mistaken for a server limit.

## Reproducibility contract

- production/release compiler settings are used: normal optimized Go builds,
  userver C++ `Release` with LTO, Boost C++ `RelWithDebInfo`/generated release,
  Python `-OO`, and Cargo `--release`;
- OTLP export is disabled and k6 sends neither `X-Trace` nor a sampled remote
  trace context, so framework spans are not created; metrics remain active;
- per-request access and ServiceLib logging is disabled for every runtime;
- Go and Go-native `GOMAXPROCS`, C++ main task-processor workers, Rust Tokio workers and all
  generated ServiceLib task pools are set to the requested service core count
  (Python remains a single event loop outside its ServiceLib task pools);
- Inventory Service starts before Order Service;
- every language receives the same JSON request, warm-up, VU count, duration,
  repetitions and per-service CPU quota;
- languages run sequentially to avoid competing with one another;
- every request uses a missing SKU, keeping the business path stable instead
  of exhausting the examples' in-memory stock;
- for repeated runs, the attempt with the highest requests/second is selected;
  all reported throughput, error and latency values come from that same attempt.

Docker's `cpus` setting is a quota, not CPU pinning. On Docker Desktop the
virtual machine must have at least `2 × service cores + load-generator cores`
available, otherwise the host/VM becomes the shared bottleneck. Avoid running
other heavy workloads during a comparison.

The normal benchmark never changes host-wide sysctls. On a dedicated machine
running the userver variant at very high concurrency, an exhausted coroutine
mapping limit can be raised explicitly with `MAX_MAP_COUNT=1048576` (Make) or
`--max-map-count 1048576` (Python). This changes `vm.max_map_count` for the
whole host or Docker VM and therefore defaults to `0` (leave untouched).

## HTTP transport isolation

```bash
make http-transport CORES=2 LOADGEN_CORES=6 VUS=256 DURATION=20s RUNS=3
```

This benchmarks the real Boost Order service listeners through an unknown GET
route. Framework and native return the same `404` status and `not found\n`
body. The path includes TCP accept, Beast HTTP parsing, routing, response
serialization and keep-alive, but cannot enter generated JSON handling, the
ServiceLib graph or gRPC. Artifacts use the `http-transport.*` prefix and do
not overwrite the main comparison.

To add the production HTTP route and JSON failure handling while still
stopping before graph collection and gRPC, run:

```bash
make http-json-reject CORES=2 LOADGEN_CORES=6 VUS=256 DURATION=20s RUNS=3
```

This deliberately sends malformed JSON and expects HTTP 400. It measures the
request admission/generated-handler/error path, not successful serde, and its
`http-json-reject.*` artifacts must not be interpreted as normal business-path
throughput. Logging is disabled for this comparison: otherwise every expected
parse failure would synchronously format and flush an error in the Boost
service while the native baseline remains silent.

The canonical TypeScript Order graph can also be isolated from both HTTP and
gRPC transport:

```bash
make graph-without-grpc CORES=2 VUS=256 DURATION=20s
```

This builds the normal production runtime image, constructs the generated
Order graph, and replaces only the Inventory sink consumer with an immediate
in-memory result after graph construction. It does not start the service
lifecycle or any network transport and does not add a diagnostic branch to
production code. Results are written to
`.artifacts/typescript-graph-without-grpc.{json,md}`. Use
`graph-without-grpc-quick` to reuse the current image.

The two remaining TypeScript isolation boundaries are explicit Make targets:

```bash
make typescript-http-only CORES=2 LOADGEN_CORES=6 VUS=256 DURATION=20s RUNS=3
make typescript-kafka-disabled CORES=2 LOADGEN_CORES=6 VUS=256 DURATION=20s RUNS=3
```

`typescript-http-only` drives an unknown route and therefore measures only
HTTP accept/read/route/write. `typescript-kafka-disabled` keeps the complete
HTTP → graph → gRPC → graph response path, while the generated Kafka endpoint
remains constructed but disabled through `ORDER_PROCESSED_ENABLED=false` and
Redpanda is not started. Both commands have `-quick` variants that reuse the
current images.

## Run

Missing native projects are cloned automatically at their pinned revisions.
To fetch all four explicitly without building or running benchmarks:

```bash
make setup-native
```

The runner never modifies an existing local checkout. This keeps local changes
safe; remove or move a stale native directory if you want it cloned again at
the pinned revision.

The default is three 20-second measurements after a 5-second warm-up:

```bash
cd benchmarks/examples
make run CORES=2 VUS=32 DURATION=20s WARMUP=5s RUNS=3
```

The requested number of cores applies independently to `orderservice` and
`inventoryservice`. The load generator defaults to six cores and can be changed
directly:

Run benchmark, profiling and conformance sequentially. They share service
ports and some Docker build resources, while profiling also requires exclusive
CPU measurements. A shared tooling lock rejects a concurrent run with a clear
error instead of allowing container collisions or corrupted build state.

```bash
python3 run.py \
  --cores 4 \
  --loadgen-cores 4 \
  --vus 128 \
  --duration 30s \
  --warmup 10s \
  --runs 5
```

Run one language while tuning:

```bash
python3 run.py --language cpp --cores 4 --vus 64
```

Compare only the framework-backed and hand-written Go implementations:

```bash
python3 run.py --language go --language go-native --cores 4 --vus 64
```

The equivalent pairs are `cpp`/`cpp-native`,
`cpp-boost`/`cpp-boost-native`, `python`/`python-native` and
`rust`/`rust-native`. C++ native uses userver directly, preserving the runtime
under the generated ServiceLib implementation; Python native uses aiohttp and
grpc.aio; Rust native uses Axum and Tonic. Boost native uses Beast and
asio-grpc directly and does not link ServiceLib.

Reuse already built release images:

```bash
python3 run.py --skip-build --cores 2 --vus 32
```

## Call-semantics performance

The normal comparison uses the canonical `FunctionCall` profile. To measure
the same business scenario with the generated `current` profile, run:

```bash
make call-semantics CORES=2 LOADGEN_CORES=6 VUS=256 DURATION=20s WARMUP=5s RUNS=3
```

This generates disposable examples containing one `TaskPool` link, one
`PriorityTaskPool` link and three `ParallelCall` links, then benchmarks the six
framework implementations. Native variants are intentionally excluded because
they do not contain the ServiceLib graph. The canonical checkouts are not
modified. Results use the `call-semantics.*` prefix under `.artifacts/`, while
generation and merge diagnostics are stored in `.artifacts/call-semantics/`.

A short smoke benchmark is available as:

```bash
make quick CORES=1 VUS=8
```

Before applying load, the runner checks each generated service graph. When it
uses `TaskPool` or `PriorityTaskPool`, the runner reads the effective runtime
graph from `/status/graph` and verifies that every configured pool size equals
`CORES`. This check is skipped automatically for `FunctionCall`-only graphs and
for every `*-native` variant. Benchmark telemetry remains noop, so this
verification does not enable Prometheus collection or add metrics overhead to
the framework result.

## Increase load by virtual users

The capacity scenario uses the same fixed-VU workload as the normal benchmark.
It starts at `START_VUS` and adds `VUS_STEP` after every successful run. A run
passes when requests complete, its HTTP/check error rate is at most 0.1%, and
its p95 and p99 latencies are at most `MAX_P95_MS` and `MAX_P99_MS`. If any
limit is exceeded, or RPS grows by less than `MIN_RPS_GAIN_PERCENT` relative
to the previous accepted level, the same VU count is measured three times.
The medians of those attempts make the final decision. If the median still
violates an SLA limit or shows insufficient RPS growth, the ramp stops. There
is no binary search.

```bash
make capacity \
  CAPACITY_LANGUAGES="go go-native" \
  CORES=2 \
  LOADGEN_CORES=6 \
  START_VUS=32 \
  VUS_STEP=32 \
  MAX_VUS=1024 \
  CAPACITY_DURATION=20s \
  CAPACITY_ATTEMPTS=3 \
  MAX_P95_MS=100 \
  MAX_P99_MS=200 \
  MIN_RPS_GAIN_PERCENT=5
```

All implementations run sequentially when `CAPACITY_LANGUAGES` is omitted.
Valid names are `go`, `go-native`, `cpp`, `cpp-native`, `cpp-boost`,
`cpp-boost-native`, `python`, `python-native`, `rust`, `rust-native`,
`typescript`, and `typescript-native`.

The detailed result is written to `.artifacts/capacity.json`; the summary is
written to `.artifacts/capacity.md`. It reports the last unsaturated VU count,
its RPS, p95 and p99, the level at which the ramp stopped, and the exact stop
reason. Raw results and CPU samples are retained for every attempt.

## Compare p99 under a fixed overload

The overload benchmark requests the same fixed arrival rate from every
language and ranks the median p99 of completed requests:

```bash
make overload \
  CORES=2 \
  LOADGEN_CORES=6 \
  RATE=50000 \
  DURATION=15s \
  WARMUP=3s \
  RUNS=3
```

Each run gets fresh service containers. This test deliberately does not require
the target rate to be sustainable. Its table therefore always reports completed
RPS, scheduled percentage and dropped percentage next to p99. Comparing p99
alone would reward an implementation when the load generator was unable to
start most of its intended requests.

Use `make overload-quick CORES=2 LOADGEN_CORES=6 RATE=50000` to validate the
test mechanics with existing images. Results are written to
`.artifacts/overload.json` and `.artifacts/overload.md`; raw k6 summaries are
stored as `overload.<language>.<run>.json`.

## Results

The runner writes:

- `.artifacts/results.md` — human-readable comparison table;
- `.artifacts/results.csv` — table for spreadsheets and plotting;
- `.artifacts/results.json` — results plus host and run metadata;
- `.artifacts/<language>.run-<n>.json` — raw summary for every measured run;
- `.artifacts/<language>.warmup.json` — warm-up summary.

The table includes requests/second, error rate and average/p50/p95/p99/max
latency from the run with the highest throughput. Throughput across different
machines, Docker Desktop resource allocations or CPU architectures must not be
compared as if it came from the same experiment.
