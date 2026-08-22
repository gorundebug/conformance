# ServiceLib benchmarks

Cross-language performance benchmarks for all generated framework and native
example services.

## Quickstart

The benchmark toolkit is part of the conformance repository. Its quickstart
clones every managed framework/native dependency into `.dependencies/`, then
the explicit benchmark target runs with 256 virtual users by default:

```bash
git clone https://github.com/gorundebug/conformance.git
cd conformance
bash ./quickstart.sh -- benchmarks
```

Requires `git`, `docker` (with the `compose` plugin) and `python3`:

```bash
make benchmarks BENCHMARK_ARGS="--cores 4 --vus 64 --duration 10s --warmup 3s --runs 1"
```

The transport fan-out and the explicit dedicated-host mapping limit remain
configurable through the same target:

```bash
make benchmarks BENCHMARK_ARGS="--grpc-connections 4 --max-map-count 1048576"
```

`--max-map-count` defaults to `0`, so normal and clean-machine runs never
change this host-wide setting implicitly.

To benchmark the framework implementations with the generated mixed
`TaskPool`/`PriorityTaskPool`/`ParallelCall` profile instead of the canonical
`FunctionCall` profile:

```bash
bash ./quickstart.sh --profile current -- benchmarks
```

This uses disposable generated examples and writes separately prefixed
artifacts; it does not modify the managed canonical checkouts.

The comparative path does not start Redpanda. Its generated framework
configurations explicitly disable the `orderProcessed` Kafka endpoint; native
implementations contain no Kafka branch and receive no Kafka-specific flag.

Framework examples use the current managed conformance workspace. Native
baselines are kept separately under `.dependencies/performance-native` and are
checked out at the exact tags recorded in the runner. This prevents the
semantic conformance copies on `main` from silently changing benchmark input.

Use `bash ./quickstart.sh --clone-only` to fetch dependencies without running
anything. To keep them elsewhere, pass an explicit directory:

```bash
bash ./quickstart.sh --dependencies-dir /path/to/benchmark-repositories -- benchmarks
```

The same directory can be used with direct Make invocations:

```bash
make benchmarks BENCHMARK_ARGS="--duration 10s --runs 1"
```

See [examples/README.md](examples/README.md) for the reproducibility contract,
benchmark modes and run instructions.
