# ServiceLib cross-language conformance

This directory contains black-box semantic tests shared by all ServiceLib
language ports. A test describes observable behavior once, runs the equivalent
generated graph in Go, C++/userver, C++/Boost.Asio, Python, Rust and
TypeScript, and
compares normalized results.
Go is currently the reference for language-neutral black-box behavior.
`cppservicelib` and `cppexample` remain the authorities for C++ public APIs,
folder layout and C++-specific semantics.

## Quickstart

Only this repository needs to be cloned by hand. `quickstart.sh` clones every
repository it depends on into `.dependencies` (if missing) and runs the full
conformance matrix, so anyone can independently verify that
Go/C++/Python/Rust/TypeScript are semantically equivalent without a
pre-existing multi-repository workspace:

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
./quickstart.sh -- standalone-components
./quickstart.sh -- kubernetes
```

Use `./quickstart.sh --clone-only` to fetch the repositories without running
anything. An existing shared checkout can be reused explicitly:

```bash
./quickstart.sh --dependencies-dir /path/to/repos -- tracing
```

### Optional shared package proxy

Generated examples include one project-independent Nexus proxy for Go, npm,
PyPI, Cargo, Helm, Maven Central, Debian/Ubuntu APT, Docker Hub and immutable GitHub/GitLab
artifacts. It is enabled only
when the caller exports one global data directory; conformance never enables
it on its own:

```bash
./quickstart.sh --clone-only
export SERVICEGEN_DEPENDENCY_PROXY_DIR="$HOME/.servicegen/dependency-proxy"
make -C .dependencies/goexample SERVICEGEN_NEXUS_ACCEPT_EULA=true dependency-cache-up # first start only
./quickstart.sh
```

The quickstart derives portable host/container proxy variables automatically.
Docker Desktop resolves `host.docker.internal` natively; Linux uses Docker's
`host-gateway` mapping. The shared data survives `dependency-cache-down`.
Pinned C++ sources populate their separate versioned source cache from
immutable archives routed through Nexus. Without
`SERVICEGEN_DEPENDENCY_PROXY_DIR`, all downloads continue to use their normal
upstreams.

After changing any C++ dependency version or its population logic, invalidate
the prepared sources and CMake state explicitly:

```bash
make dependency-source-cache-invalidate
```

This keeps compiler ccache and Nexus downloads. The next build configures a
fresh dependency tree from the cached archives.

The runners read the same path from `CONFORMANCE_DEPENDENCIES_DIR`; when it is
unset, direct `make` and `python3 .../run.py` commands remain compatible with
the development workspace where repositories sit next to `conformance`.
For diagnosing a single language, call a runner directly (see below), e.g.
`python3 tracing/run.py --language cpp`.

Do not run benchmark, profiling and conformance concurrently. The full
conformance matrix already includes its own mandatory profiling gate.
`quickstart.sh` holds a shared tooling lock, and the other runners refuse a
concurrent run, preventing container/port collisions, invalid CPU measurements
and concurrent writes to C++ build trees.

## Distributed tracing

The first test starts all three services for each language, an isolated Jaeger
instance and Redpanda. It sends the same HTTP request with a sampled W3C
`traceparent`, waits until Analytics Service has consumed the resulting Kafka
event, fetches that exact distributed trace, and compares the ServiceLib span
trees. The trace contract includes the Kafka producer boundary; successful
consumer processing is verified through the Analytics Service runtime graph.

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

## Metrics and runtime execution graph

The metrics test starts the same five language examples with all three services
and Redpanda, sends one deterministic order request, waits until Analytics
Service consumes the resulting Kafka event, reads `/metrics` from all three
services and compares the ServiceLib metric contract against Go:

- metric names;
- the complete set of framework labels and their values;
- exact values for deterministic counters and idle gauges;
- presence and labels, but not values, for duration sums;
- histogram buckets are excluded because bucket representation belongs to the
  production metrics backend rather than to the ServiceLib contract.
- untouched zero counters and histograms are excluded because some Prometheus
  backends expose registered instruments eagerly while others omit them until
  the first observation; zero-valued gauges remain part of the contract.

The same runtime pass also reads `/status/data` from Order Service, Inventory
Service and Analytics Service after Kafka delivery. It compares the complete
normalized execution graph against Go for every language: nodes, edges, types,
coordinates, presentation fields and deterministic post-request call counters.
Node/edge array order and implementation-local numeric node IDs are normalized;
endpoints are matched by their complete display labels. No language-specific
graph field or value is ignored.

Only exporter-owned labels such as `otel_scope_*` are removed. Framework labels
are never hidden by normalization.

Run the full matrix:

```bash
make metrics
```

Run selected ports or reuse current images:

```bash
python3 metrics/run.py --language go --language cpp
python3 metrics/run.py --language go --language cppboost
python3 metrics/run.py --skip-build
```

Artifacts are written to `.artifacts/metrics/`, including the raw Prometheus
responses, raw status graph responses, normalized `<language>.runtime-graph.json`
files, comparison diffs and a summary.

## Structured logging

The logging gate executes the same four severity levels and typed
string/int64/float64/bool/error field contract in Go, canonical userver C++,
Boost C++, Python and Rust. It checks message/field preservation and reset or
clear behavior; the C++ builds always use unrestricted CMake parallelism.
Python runs in the generated example's development image, so a clean machine
does not need a separately prepared `pyservicelib/.venv`.

```bash
make logging
python3 logging/run.py --skip-build
```

The machine-readable result is `.artifacts/logging/summary.json`.

Run all conformance suites:

```bash
make all
```

The existing fine-grained targets remain available, and three aggregate levels
cover the common workflows:

```bash
make fast         # structure, signatures, config, pools, operators and serde
make integration  # runtime, standalone builds, transports, Kafka, telemetry and scenarios
make release      # fast + integration + mandatory profiling + aggregate report
make resume       # run only failed or missing leaf suites, then aggregate
```

`make all` is a compatibility alias for `make release`. `make resume` reads the
existing aggregate and per-suite summaries, treats the per-suite files as the
current source of truth after an interrupted run, and preserves already passing
results. The aggregate groups are explicitly non-parallel because several
runtime suites share Docker ports, containers and build caches.

The same targets are available from a clean checkout through quickstart:

```bash
./quickstart.sh -- fast
./quickstart.sh -- integration
./quickstart.sh -- resume
```

After every suite passes, `make release` writes a single 24-suite result matrix to
`.artifacts/summary.json`. The aggregate step also rejects a partial
Go/C++/Python/Rust metrics, tracing or logging run. It prints the complete
PASS/FAIL suite matrix, the final passed count and the report path to the
terminal as well.

## Standalone component builds

The `standalone-components` gate proves that every generated service and
schema/API module can be consumed as an independent project. For each of Go,
userver C++, Boost C++, Python, Rust and TypeScript it creates seven separate
temporary filesystem trees and builds/tests `analyticsservice`,
`automationservice`, `inventoryservice`, `orderservice`,
`inventory_service_api`, `model` and `order_service_api` one at a time. The
Automation Service is native Go/Python/TypeScript; C++/Boost/Rust project
variants build their generated Go fallback. A service tree contains only that
service, its declared generated modules and its local framework checkout; a
root workspace build does not satisfy the gate.

Go protobuf and OpenAPI outputs are regenerated inside a pinned cached
toolchain image before compilation. This keeps the test valid for disposable
generated workspaces, where generated module outputs have not previously been
published or checked out.

Rust OpenAPI bindings follow the same rule: the isolated package runs its
generated `make generate` entrypoint with the pinned OpenAPI Generator before
Cargo resolves or compiles it.

The temporary trees deliberately contain no `.git` metadata. Local path and
workspace overrides replace published dependency coordinates only for this
development check, so neither services nor generated modules need to be pushed
or tagged first:

This gate deliberately does not start services. Runtime readiness, transports,
Kafka and metrics require the complete environment and are verified by their
dedicated integration suites.

```bash
make standalone-components
python3 standalone_components/run.py \
  --local-root /path/to/local/repositories
python3 standalone_components/run.py \
  --language rust --component orderservice
```

`CONFORMANCE_STANDALONE_COMPONENTS_ROOT` provides the same local-root setting.
When it is absent, the runner uses `CONFORMANCE_DEPENDENCIES_DIR`, including
the directory selected by `quickstart.sh --dependencies-dir`. Results are
written to `.artifacts/standalone-components/summary.json`. A filtered
language/component invocation is diagnostic and writes
`.artifacts/standalone-components/diagnostic-summary.json`, so it cannot
replace the last authoritative full-matrix result.

## Kubernetes and Helm

The `kubernetes` gate validates the project-level k3s/registry Compose file and
independently lints and renders every service Helm chart for Go, userver C++,
Boost C++, Python, Rust and TypeScript. The static contract covers workloads,
Services, health probes, ConfigMap and Secret wiring, and ServiceMonitor
resources for runtimes whose metrics are collected with the Prometheus pull
model.

The gate then performs one complete Go runtime probe. It builds the existing
minimal service images, pushes them to the temporary local registry, installs
the pinned official Redpanda chart when Kafka is present, and rolls out all
three services. The local environment also contains Prometheus, Grafana,
Jaeger, Loki and the OpenTelemetry Collector. The probe sends a real
HTTP-to-gRPC order, waits for the authenticated Kafka event to reach Analytics
Service, and verifies Kubernetes health, metrics, dashboards, traces and logs.

Both metric paths are part of the generated contract: ServiceMonitor scrapes
the C++ and TypeScript `/metrics` endpoints, while Go, Python and Rust export
OTLP metrics through the collector. Collector relabeling normalizes both paths
to the same canonical `service` and `job` labels and removes temporary
`exported_service` and `exported_job` labels before Prometheus stores them.
Generated Grafana dashboards therefore use the same service identity in the
ordinary Docker Compose environment and in Kubernetes.

The real end-to-end Kubernetes rollout currently uses Go as the runtime probe;
all six framework implementations are independently Helm-linted and rendered.
The disposable k3s cluster, registry data and workload volumes are removed even
when the gate fails. Downloaded containerd image layers are deliberately kept
in the architecture-specific external
`servicegen-kubernetes-image-cache-v1-<arch>` volume, so a clean subsequent run
does not download the complete infrastructure again.

```bash
make kubernetes
bash ./quickstart.sh -- kubernetes
```

## Generated example merge conformance

The generation gate creates the canonical Boost C++ archive from the graph,
specification and configuration inputs and merges it into a disposable copy of
`cppboostexample`. It hashes every existing user-owned file before and after
the merge; an overwrite, removal or mode change fails the gate. New files from
the archive are reported separately, because adding a previously absent file
is part of the merge contract. Local `.servicegen` state is excluded. The gate
then performs a clean Docker Release unit build and the generated Debug
integration suite.

```bash
make generation
python3 generation/run.py --skip-docker  # generation/merge diagnosis only
```

The release artifacts in `.artifacts/generation/` contain the before/after
manifests, merge diff, archive digest and complete build/test logs.

## Kafka endpoint lifecycle

The Kafka gate starts a real Redpanda broker with automatic topic creation
disabled, then exercises the generated Go, C++/userver, C++/Boost, Python and
Rust examples. For every language it verifies that the enabled source creates
the configured topic and partition count, the sink accepts that topic already
existing, and one order event is published, consumed and committed by the
Analytics service.

```bash
make kafka
python3 kafka/run.py --language go
python3 kafka/run.py --skip-build
```

Consumer-group snapshots and the matrix result are written to
`.artifacts/kafka/`.

## Temporal scheduling and durable execution

The Temporal gate runs the supported Go, Python and TypeScript Automation
Service implementations against the pinned real Temporal Server and PostgreSQL
infrastructure. For each language it creates and validates the generated
Schedule, stops the Worker, queues more executions than the configured Activity
capacity, restarts the same service, and verifies that the durable backlog is
pulled by available Worker slots. The observed graph must include the scheduled
input, symmetric Temporal endpoint Workflow, `DurableCall` Workflow/Activity,
the unchanged target Map node, and its normal result link. It also waits for one
process-local cron firing and therefore distinguishes local scheduling from
Temporal-owned recovery.

```bash
make temporal
python3 temporal/run.py --language go
python3 temporal/run.py --skip-build
```

Workflow listings and `/status/data` snapshots are stored below
`.artifacts/temporal/`. C++ and Rust are deliberately absent from this runtime
matrix: there is no production-supported official Temporal SDK for those
frameworks, while their generated projects use the Go Automation Service
fallback.

## C++ gRPC and Kafka transport conformance

The transport gate executes the canonical C++ endpoint contract and the real
Boost.Asio/asio-grpc network runtime for unary, client-streaming,
server-streaming and bidirectional-streaming calls. It requires accepted-call
cancellation to reach the server `MessageContext`, runs the Boost suite in
Release and ASan+UBSan configurations, and clean-builds the generated workspace
containing all four RPC method types:

The same gate executes canonical userver and Boost Kafka adapters with an
identical topic/key/value/partition/offset/commit fixture. The Boost test also
performs produce, consume and commit through librdkafka's real mock-broker
protocol. Kafka broker envelopes are library-owned and not deterministic
ServiceLib bytes; the application payload and transport fields are the
field-for-field contract.

```bash
make transports
python3 transports/run.py --skip-build
```

The machine-readable result is `.artifacts/transports/summary.json`. Bare
`--parallel` is used for every CMake build; a passing local Release executable
without the sanitizer and generator gates does not satisfy this suite.
The first run prepares one C++ dependency source cache per host
architecture. Release, sanitizer, gRPC and Kafka builds keep independent CMake
build trees but reuse those sources without downloading them again; the cache
must be invalidated with `make dependency-source-cache-invalidate` whenever a
pinned dependency or its population setup changes. The generated streaming and Kafka recovery fixtures receive the same
cache as a read-only mount, so their disposable Docker build volumes do not
trigger another dependency download.

## Profiling conformance and profiling toolkit

The profiling gate runs `cppboostexample` and `cppboostnativeexample` with the
same scenario, VUs, duration, warm-up and service/load-generator CPU quotas. It
profiles both Order and Inventory, rejects errors or dropped iterations, and
validates the schema and non-empty contents of every flamegraph, folded stack,
top-frame and load artifact. Framework runs additionally require complete
timestamped Asio worker/event-loop/gRPC CompletionQueue runtime metrics.
The runner reads the actual generated CMake cache and rejects a stale Debug
volume; `--skip-build` is accepted only when the reused framework binary is a
Release build.

```bash
make profiling
python3 profiling/run.py --skip-build
python3 profiling/run.py --skip-run  # validate existing artifacts only
```

The machine-readable result is `.artifacts/profiling/summary.json`. This is a
mandatory part of `make all`; build-only or process-start smoke tests do not
satisfy it.

The complete profiler now lives in `profiling/examples/` in this repository;
there is no runtime dependency on a separate `profiling` checkout. The
mandatory gate deliberately keeps the expensive Release CPU, allocation,
scheduler and off-CPU matrix focused on the Boost framework/native pair. The
same toolkit retains profiling for every framework/native implementation,
TypeScript Inspector CPU/heap profiles and runtime diagnostics, failure-path
scenarios, host preparation switches and all derived flamegraph artifacts:

```bash
make profiling-all
make profiling-all PROFILING_ARGS="--language rust --duration 20s"
make profiling-all PROFILING_ARGS="--language typescript --scenario timeout"
make profiling-durable PROFILING_ARGS="--language go --language python --language typescript"
make profiling-durable-quick PROFILING_ARGS="--language python"
make profiling-tests
```

With `quickstart.sh`, `--profile current` runs the same profiler against the
disposable pooled graph; the default remains the canonical FunctionCall graph.

## Comparative benchmarks

The former standalone benchmark toolkit now lives in `benchmarks/examples/`.
The conformance target runs the complete twelve-variant framework/native
matrix sequentially with 2 service cores, 6 load-generator cores, 256 VUs and
three 20-second measurements. For each implementation the complete row comes
from the single run with the highest throughput; percentiles are never mixed
between runs.

```bash
bash ./quickstart.sh -- benchmarks
bash ./quickstart.sh --profile current -- benchmarks
make benchmarks BENCHMARK_ARGS="--duration 10s --warmup 3s --runs 1"
make benchmarks-quick
make benchmarks-durable BENCHMARK_ARGS="--language go --language python --language typescript"
make benchmarks-durable-quick BENCHMARK_ARGS="--language go"
make benchmarks-tests
```

The conformance wrapper validates the exact language matrix, zero errors,
positive throughput and requested workload, then writes
`.artifacts/benchmarks/summary.json`. Raw JSON, CSV, Markdown and per-run k6
artifacts remain under `benchmarks/examples/.artifacts/`. Benchmarks are an
explicit target rather than part of `make all`: throughput is host-dependent
and running it after the semantic and profiling suites would add a second long
load matrix without increasing semantic coverage.

Normal benchmark and profiling targets keep local cron, Temporal and the
Automation Service disabled. The `*-durable` targets are the explicit opt-in
path for supported Go, Python and TypeScript implementations. They start real
Temporal/PostgreSQL infrastructure and measure or profile Schedule admission →
endpoint Activity → ordinary graph node → `DurableCall` Activity → result.
Their artifacts are isolated below `benchmarks/examples/.artifacts/durable/`
and `profiling/examples/.artifacts/durable/`.

## C++ structural conformance

The structural suite compares `cppboostservicelib` directly with the
canonical `cppservicelib`, and `cppboostexample` directly with `cppexample`.
It fails on every unrecorded or stale public-path difference, every newly
changed shared public file and every generated-example layout difference. It
also compares all user-authored service/function interface headers: nine must
remain byte-identical, while the sole HTTP transport-boundary implementation
must retain its canonical public member contract.
The only accepted differences live in `structure/deviations.json` and must be
an explicit userver boundary or its Boost replacement.

```bash
make structure
python3 structure/run.py
```

The machine-readable result is written to
`.artifacts/structure/summary.json`. This suite covers paths, shared-file
identity and example layout; signature and behavioral comparisons are
separate required conformance layers and are not implied by a structural pass.

## Configuration conformance

`python3 config/run.py` compares the canonical generated Go configuration with
the Boost C++ configuration for both example services. The base and values
files must be byte-identical. Every generated `$variable` is then checked in
both typed adapters: Boost must write the real canonical C++ member from YAML
and environment input (never hide it in `properties`), and Go must write the
corresponding typed member through the same environment name. The runner emits
`.artifacts/config/summary.json` and is part of `make all`.

The separate runtime gate starts the generated Boost service with a temporary
bind-mounted `overrides.yaml`, observes a valid reload, injects malformed YAML,
verifies the previous snapshot remains served while the canonical error metric
increments, then restores a valid file and verifies recovery. It also checks
that generated lifecycle wiring publishes an ownership-safe runtime snapshot
and stops polling before service shutdown:

```bash
make config-runtime
python3 config/runtime.py --skip-build
```

Artifacts are written to `.artifacts/config-runtime/`. Both the static and
runtime configuration gates are part of `make all`.

## C++/Boost dependency conformance

`python3 dependencies/run.py` rejects userver includes, packages and link
targets in the Boost framework, examples and generator build inputs. It then
builds the generated example in the canonical Docker Release environment and
inspects the actual Order and Inventory dynamic dependency sets with `ldd`.
Both the static manifests and linked binaries must remain userver-free:

```bash
make dependencies
python3 dependencies/run.py --skip-build
```

The machine-readable result is `.artifacts/dependencies/summary.json`. Use
`--static-only` only for a fast local diagnostic; the mandatory `make all`
target always performs the linked-binary check.

## Pool behavior and lifecycle conformance

The pool suite executes the authoritative Go pool behavior tests, the current
canonical userver C++ `TaskPool`/`PriorityTaskPool`/`DelayPool` tests, and the
corresponding Boost tests. Its source matrix additionally makes removal or
silent skipping of a required FIFO/priority/deadline/cancellation/resize/
lifecycle-race case fail before execution.

```bash
make pools
python3 pools/run.py --skip-build
```

The normal target rebuilds both C++ implementations using unrestricted build
parallelism. `--skip-build` reuses the current Docker build trees. The
machine-readable result, commands and output tails are written to
`.artifacts/pools/summary.json`.

## Core operator and topology conformance

The operator suite executes the complete canonical Go operator service tests,
the canonical C++ operator contract, and the Boost operator, branch, cycle,
join and multi-join topology tests. A checked source matrix prevents silently
removing required map/filter/flat-map/process/split/merge/case/delay/key-by or
inner/left/right/outer/multi-join cases.

```bash
make operators
python3 operators/run.py --skip-build
```

The machine-readable result is `.artifacts/operators/summary.json`. Connector
error paths and transport behavior remain separate mandatory suites; this
target does not claim them from the core operator pass.

## C++ serde wire-format conformance

The serde suite requires the canonical and Boost public serde headers to stay
byte-identical, preserves all ten canonical primitive/container/error test
cases, and executes both C++ implementations. It also runs public-API probes
for Go, canonical C++ and Boost C++ and compares 24 deterministic primitive,
array, string/bytes and key/value encodings byte-for-byte. The Go compiler and
both C++ probes run in pinned Docker build environments. Normal checks run in
Release as well as Debug because the Boost test uses real failure checks rather
than `assert`.

```bash
make serde
python3 serde/run.py --skip-build
```

The machine-readable result is `.artifacts/serde/summary.json`, including the
exact shared fixture bytes. The same target also executes the generated Order/OrderItem/
OrderItemResult/OrderState JSON serdes in canonical userver C++ and Boost C++,
checks nested round trips, parses their output, and compares the four values
field-for-field (JSON number spelling and object key order are intentionally
not byte-level contracts). The gate also uses the real generated
`ProcessOrderItemRequest/Response` classes in Go, canonical userver C++ and
Boost C++, comparing 14 deterministic protobuf wire/round-trip/decoded-field
fixtures byte-for-byte. These include defaults, negative `int32`, UTF-8,
embedded NUL and unknown-field preservation. HTTP and the remaining live
framework/native streaming gRPC transcript comparison remain separate gates;
Kafka application payload interoperability is covered by `make transports`.

## Cross-language framework/native scenarios

The scenario suite runs the Go, userver C++, Boost C++, Python, Rust and
TypeScript framework examples together with every corresponding native example
through the same confirmed, out-of-stock, mixed partial-confirmation, delayed-
dependency, invalid-input and forced-timeout requests. Go is the semantic
reference; every other implementation must produce the same observable
contract.
It verifies HTTP status and JSON content type, retains each raw response body
as UTF-8 and hex, checks protobuf-backed inventory behavior, and compares
cross-language field semantics (excluding only nondeterministic
`processed_at`). Raw JSON bytes are evidence, not an equality requirement,
because object member order and numeric spelling are not JSON contracts.
The same gate runs a generated Go client directly against the Inventory unary
gRPC endpoint. It compares the generated protobuf response and status,
client-visible readiness deadline/cancellation, and a successful call after
service recovery. The delayed-dependency case pauses
Inventory after both services are ready, resumes it after a controlled delay,
requires the accepted HTTP request to complete successfully and records the
observed latency. This does not claim streaming-RPC or already-accepted-call
cancellation coverage.

```bash
make scenarios
python3 scenarios/run.py --skip-build
```

The raw observations are written to
`.artifacts/scenarios/<implementation>.json`; the complete machine-readable
matrix is written to `.artifacts/scenarios/summary.json`.

## Call-semantics integration

The call-semantics gate follows the profile selected by `quickstart.sh`. With
the default `function-call` profile it requires all six links to use
`FunctionCall` and runs the real HTTP/gRPC success, timeout, cancellation,
delayed recovery and shutdown scenarios without enabling pool assertions:

```bash
bash ./quickstart.sh -- call-semantics
```

With the `current` profile, quickstart first generates and merges disposable
examples. The same gate then requires exactly one `TaskPool`, one
`PriorityTaskPool` and three `ParallelCall` links in every graph. It also
scrapes live Order and Inventory metrics and fails unless the configured
ordinary and priority pools processed tasks:

```bash
bash ./quickstart.sh --profile current -- call-semantics
```

Native examples are excluded because they do not contain the generated
ServiceLib graph. Results are written under `.artifacts/call-semantics/`.

To run the entire conformance matrix against the same generated pooled graph,
select the profile at quickstart level:

```bash
bash ./quickstart.sh --profile current
bash ./quickstart.sh --profile current -- tracing
bash ./quickstart.sh --profile current -- metrics
```

Without `--profile`, quickstart uses the canonical `function-call` examples,
including in the call-semantics gate; it never creates a pooled workspace.
The `current` run prepares disposable copies, so managed example checkouts are
never modified. Artifacts from different profiles are not mixed: switching the
profile clears previous suite summaries before `resume` can reuse them.

## Grafana dashboard conformance

The dashboard suite runs after the live metrics scenario. It checks every
generated HTTP server, gRPC server/client and language/runtime dashboard. It
extracts every panel query and checks every referenced metric family against
the corresponding service's live `/metrics` response. Rust runtime panels use
only the stable metrics from the official
`tokio-metrics` collector; metrics requiring `tokio_unstable` are excluded. The
canonical graph currently has no HTTP-client edge, so HTTP-client
query families are checked structurally and reported separately rather than
being presented as live-traffic evidence.

This suite validates the ordinary Docker Compose deployment for every language.
The Kubernetes gate separately provisions the same generated dashboard set in
Grafana and verifies that its Prometheus data source sees the normalized
Kubernetes metrics. Dashboard service selectors accept the canonical compact,
hyphenated and display-name forms (for example `orderservice`,
`order-service` and `Order Service`) without relying on environment-specific
label spelling.

```bash
make dashboards
```

The machine-readable result is `.artifacts/dashboards/summary.json`.
