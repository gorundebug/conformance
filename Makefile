LOCAL_DEPENDENCIES_DIR := $(abspath .dependencies)
ifneq ($(wildcard $(LOCAL_DEPENDENCIES_DIR)/.),)
CONFORMANCE_DEPENDENCIES_DIR ?= $(LOCAL_DEPENDENCIES_DIR)
export CONFORMANCE_DEPENDENCIES_DIR
endif

.PHONY: test-paths tooling structure signatures config config-core config-schema \
	config-runtime config-runtime-core config-runtime-go \
	config-runtime-typescript dependencies pools operators serde transports \
	kafka temporal tracing metrics dashboards dashboards-core logging scenarios \
	call-semantics standalone-components kubernetes \
	generation profiling profiling-all profiling-tests \
	profiling-durable profiling-durable-quick \
	benchmarks benchmark benchmarks-quick benchmarks-durable benchmarks-durable-quick benchmarks-tests \
	fast integration release resume all clean dependency-source-cache-invalidate

.NOTPARALLEL: fast integration release resume all

test-paths:
	python3 -m unittest test_paths

dependency-source-cache-invalidate:
	python3 cpp_source_cache.py invalidate

tooling:
	python3 run_suite.py tooling python3 tooling/run.py

all: release

release: fast integration profiling
	python3 aggregate.py

fast: tooling structure signatures config pools operators serde

integration: config-runtime dependencies standalone-components transports kafka temporal tracing metrics dashboards logging scenarios call-semantics generation kubernetes

resume:
	python3 resume.py

structure:
	python3 run_suite.py structure python3 structure/run.py

signatures:
	python3 run_suite.py signatures python3 signatures/run.py

config: config-core config-schema

config-core:
	python3 run_suite.py config python3 config/run.py

config-schema:
	python3 run_suite.py config-schema python3 config/schema.py

config-runtime: config-runtime-core config-runtime-go config-runtime-typescript

config-runtime-core:
	python3 run_suite.py config-runtime python3 config/runtime.py

config-runtime-go:
	python3 run_suite.py config-runtime-go python3 config/runtime_go.py

config-runtime-typescript:
	python3 run_suite.py config-runtime-typescript python3 config/runtime_typescript.py

dependencies:
	python3 run_suite.py dependencies python3 dependencies/run.py

standalone-components:
	python3 run_suite.py standalone-components python3 standalone_components/run.py

pools:
	python3 run_suite.py pools python3 pools/run.py

operators:
	python3 run_suite.py operators python3 operators/run.py

serde:
	python3 run_suite.py serde python3 serde/run.py

transports:
	python3 run_suite.py transports python3 transports/run.py

kafka:
	python3 run_suite.py kafka python3 kafka/run.py

temporal:
	python3 run_suite.py temporal python3 temporal/run.py

tracing:
	python3 run_suite.py tracing python3 tracing/run.py

metrics:
	python3 run_suite.py metrics python3 metrics/run.py

dashboards: metrics kafka transports dashboards-core

dashboards-core:
	python3 run_suite.py dashboards python3 dashboards/run.py

logging:
	python3 run_suite.py logging python3 logging/run.py

scenarios:
	python3 run_suite.py scenarios python3 scenarios/run.py

call-semantics:
	python3 run_suite.py call-semantics python3 call_semantics/run.py

generation:
	python3 run_suite.py generation python3 generation/run.py

kubernetes:
	python3 run_suite.py kubernetes python3 kubernetes/run.py

profiling:
	python3 run_suite.py profiling python3 profiling/run.py

profiling-all:
	python3 profiling/examples/run.py \
		--graph-profile "$${CONFORMANCE_EXAMPLE_PROFILE:-function-call}" \
		$(PROFILING_ARGS)

profiling-durable:
	PROFILING_DEPENDENCIES_DIR="$(CONFORMANCE_DEPENDENCIES_DIR)" \
		python3 profiling/examples/durable.py $(PROFILING_ARGS)

profiling-durable-quick:
	PROFILING_DEPENDENCIES_DIR="$(CONFORMANCE_DEPENDENCIES_DIR)" \
		python3 profiling/examples/durable.py --skip-build --duration 5 --jobs 100 \
		$(PROFILING_ARGS)

profiling-tests:
	python3 -m unittest discover -s profiling/examples -p 'test_*.py' -v

benchmarks benchmark:
	python3 run_suite.py benchmarks python3 benchmarks/run.py $(BENCHMARK_ARGS)

benchmarks-quick:
	python3 run_suite.py benchmarks python3 benchmarks/run.py \
		--skip-build --vus 256 --duration 5s --warmup 2s --runs 1

benchmarks-durable:
	BENCHMARK_DEPENDENCIES_DIR="$(CONFORMANCE_DEPENDENCIES_DIR)" \
		python3 benchmarks/examples/durable.py $(BENCHMARK_ARGS)

benchmarks-durable-quick:
	BENCHMARK_DEPENDENCIES_DIR="$(CONFORMANCE_DEPENDENCIES_DIR)" \
		python3 benchmarks/examples/durable.py --skip-build --jobs 10 \
		--warmup-jobs 1 --runs 1 $(BENCHMARK_ARGS)

benchmarks-tests:
	python3 -m unittest discover -s benchmarks/examples -p 'test_*.py' -v

clean:
	rm -rf .artifacts benchmarks/examples/.artifacts profiling/examples/.artifacts
