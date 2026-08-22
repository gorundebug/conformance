PYTHON ?= python3
MAKEFILE_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
BENCHMARK_DEPENDENCIES_DIR ?= $(if $(CONFORMANCE_DEPENDENCIES_DIR),$(CONFORMANCE_DEPENDENCIES_DIR),$(abspath $(MAKEFILE_DIR)/../../..))
export BENCHMARK_DEPENDENCIES_DIR

CORES ?= 2
VUS ?= 256
DURATION ?= 20s
WARMUP ?= 5s
RUNS ?= 3
START_VUS ?= 32
VUS_STEP ?= 32
MAX_VUS ?= 4096
MAX_P95_MS ?= 100
MAX_P99_MS ?= 200
MIN_RPS_GAIN_PERCENT ?= 5
CAPACITY_ATTEMPTS ?= 3
CAPACITY_DURATION ?= 20s
CAPACITY_WARMUP ?= 5s
CAPACITY_LANGUAGES ?=
CAPACITY_LANGUAGE_ARGS = $(foreach language,$(CAPACITY_LANGUAGES),--language $(language))
LOADGEN_CORES ?= 6
RATE ?= 50000
MAX_MAP_COUNT ?= 0

.PHONY: setup-native run quick call-semantics http-transport http-transport-quick http-json-reject http-json-reject-quick typescript-http-only typescript-http-only-quick graph-without-grpc graph-without-grpc-quick typescript-kafka-disabled typescript-kafka-disabled-quick capacity overload overload-quick build clean help

setup-native: ## Fetch missing pinned native example projects
	$(PYTHON) run.py --fetch-native --language go-native --language cpp-native \
		--language cpp-boost-native --language python-native --language rust-native \
		--language typescript-native

run: ## Build and benchmark all framework and native examples
	$(PYTHON) run.py --cores $(CORES) --loadgen-cores $(LOADGEN_CORES) --vus $(VUS) \
		--duration $(DURATION) --warmup $(WARMUP) --runs $(RUNS) \
		--max-map-count $(MAX_MAP_COUNT)

quick: ## Reuse current images and run one short measurement
	$(PYTHON) run.py --skip-build --cores $(CORES) --loadgen-cores $(LOADGEN_CORES) --vus $(VUS) \
		--duration 5s --warmup 2s --runs 1 --max-map-count $(MAX_MAP_COUNT)

call-semantics: ## Benchmark generated framework examples with TaskPool, PriorityTaskPool and ParallelCall
	$(PYTHON) call_semantics.py --cores $(CORES) --loadgen-cores $(LOADGEN_CORES) \
		--vus $(VUS) --duration $(DURATION) --warmup $(WARMUP) --runs $(RUNS) \
		--max-map-count $(MAX_MAP_COUNT)

http-transport: ## Compare only HTTP accept/read/route/write; no serde, graph or gRPC
	$(PYTHON) run.py --cores $(CORES) --loadgen-cores $(LOADGEN_CORES) --vus $(VUS) \
		--duration $(DURATION) --warmup $(WARMUP) --runs $(RUNS) \
		--language cpp-boost --language cpp-boost-native --max-map-count 0 \
		--scenario http_transport_only --method GET --expected-status 404 \
		--target http://orderservice:9091/__servicelib_transport_probe \
		--result-prefix http-transport

http-transport-quick: ## Reuse images for a short transport-only comparison
	$(PYTHON) run.py --skip-build --cores $(CORES) --loadgen-cores $(LOADGEN_CORES) --vus $(VUS) \
		--duration 5s --warmup 2s --runs 1 \
		--language cpp-boost --language cpp-boost-native --max-map-count 0 \
		--scenario http_transport_only --method GET --expected-status 404 \
		--target http://orderservice:9091/__servicelib_transport_probe \
		--result-prefix http-transport

http-json-reject: ## Add generated/native HTTP handler and JSON rejection; no graph or gRPC
	$(PYTHON) run.py --cores $(CORES) --loadgen-cores $(LOADGEN_CORES) --vus $(VUS) \
		--duration $(DURATION) --warmup $(WARMUP) --runs $(RUNS) \
		--language cpp-boost --language cpp-boost-native --max-map-count 0 \
		--scenario http_json_reject --method POST --payload-mode invalid-json \
		--expected-status 400 --target http://orderservice:9091/v1/processorder \
		--result-prefix http-json-reject

http-json-reject-quick: ## Reuse images for a short JSON-rejection comparison
	$(PYTHON) run.py --skip-build --cores $(CORES) --loadgen-cores $(LOADGEN_CORES) --vus $(VUS) \
		--duration 5s --warmup 2s --runs 1 \
		--language cpp-boost --language cpp-boost-native --max-map-count 0 \
		--scenario http_json_reject --method POST --payload-mode invalid-json \
		--expected-status 400 --target http://orderservice:9091/v1/processorder \
		--result-prefix http-json-reject

typescript-http-only: ## Compare TypeScript framework/native HTTP transport without serde, graph or gRPC
	$(PYTHON) run.py --cores $(CORES) --loadgen-cores $(LOADGEN_CORES) --vus $(VUS) \
		--duration $(DURATION) --warmup $(WARMUP) --runs $(RUNS) \
		--language typescript --language typescript-native --max-map-count 0 \
		--scenario http_transport_only --method GET --expected-status 404 \
		--target http://orderservice:9091/__servicelib_transport_probe \
		--result-prefix typescript-http-only

typescript-http-only-quick: ## Reuse images for a short TypeScript HTTP transport comparison
	$(PYTHON) run.py --skip-build --cores $(CORES) --loadgen-cores $(LOADGEN_CORES) --vus $(VUS) \
		--duration 5s --warmup 2s --runs 1 \
		--language typescript --language typescript-native --max-map-count 0 \
		--scenario http_transport_only --method GET --expected-status 404 \
		--target http://orderservice:9091/__servicelib_transport_probe \
		--result-prefix typescript-http-only

graph-without-grpc: ## Run the canonical TypeScript Order graph with an in-memory Inventory result
	$(PYTHON) graph_only.py --cores $(CORES) --vus $(VUS) --duration $(DURATION)

graph-without-grpc-quick: ## Reuse the TypeScript runtime image for a short graph-only diagnostic
	$(PYTHON) graph_only.py --skip-build --cores $(CORES) --vus $(VUS) --duration 5s

typescript-kafka-disabled: ## Compare the full TypeScript request path with Kafka explicitly disabled
	$(PYTHON) run.py --cores $(CORES) --loadgen-cores $(LOADGEN_CORES) --vus $(VUS) \
		--duration $(DURATION) --warmup $(WARMUP) --runs $(RUNS) \
		--language typescript --language typescript-native \
		--scenario process_order_out_of_stock --result-prefix typescript-kafka-disabled

typescript-kafka-disabled-quick: ## Reuse images for a short Kafka-disabled TypeScript comparison
	$(PYTHON) run.py --skip-build --cores $(CORES) --loadgen-cores $(LOADGEN_CORES) --vus $(VUS) \
		--duration 5s --warmup 2s --runs 1 \
		--language typescript --language typescript-native \
		--scenario process_order_out_of_stock --result-prefix typescript-kafka-disabled

capacity: ## Increase virtual users until three attempts fail
	$(PYTHON) capacity.py --cores $(CORES) \
		--loadgen-cores $(LOADGEN_CORES) --start-vus $(START_VUS) \
		--vus-step $(VUS_STEP) --max-vus $(MAX_VUS) \
		--attempts $(CAPACITY_ATTEMPTS) --duration $(CAPACITY_DURATION) \
		--warmup $(CAPACITY_WARMUP) --max-p95-ms $(MAX_P95_MS) \
		--max-p99-ms $(MAX_P99_MS) \
		--min-rps-gain-percent $(MIN_RPS_GAIN_PERCENT) \
		$(CAPACITY_LANGUAGE_ARGS) \
		--max-map-count $(MAX_MAP_COUNT)

overload: ## Compare p99 latency at the same fixed arrival rate
	$(PYTHON) overload.py --cores $(CORES) \
		--loadgen-cores $(LOADGEN_CORES) --rate $(RATE) \
		--duration $(DURATION) --warmup $(WARMUP) --runs $(RUNS) \
		--max-map-count $(MAX_MAP_COUNT)

overload-quick: ## Smoke-test overload comparison using existing images
	$(PYTHON) overload.py --skip-build --cores $(CORES) \
		--loadgen-cores $(LOADGEN_CORES) --rate $(RATE) \
		--duration 3s --warmup 1s --runs 1 --max-map-count $(MAX_MAP_COUNT)

build: ## Build release images/binaries without running load
	$(PYTHON) run.py --build-only

clean: ## Stop benchmark containers and remove generated results
	$(PYTHON) run.py --clean

help: ## Show benchmark commands and parameters
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'
