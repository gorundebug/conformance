ifneq ($(strip $(DEPENDENCY_PROXY_DIR)),)
DEPENDENCY_PROXY_HOST ?= localhost
DEPENDENCY_PROXY_DOCKER_HOST ?= host.docker.internal
DEPENDENCY_PROXY_PORT ?= 18081
DEPENDENCY_GIT_MIRROR_PORT ?= 18084
DEPENDENCY_PROXY_BASE := http://$(DEPENDENCY_PROXY_HOST):$(DEPENDENCY_PROXY_PORT)/repository
DEPENDENCY_GIT_MIRROR_BASE := http://$(DEPENDENCY_PROXY_HOST):$(DEPENDENCY_GIT_MIRROR_PORT)/cgi-bin/git
export GOPROXY := $(DEPENDENCY_PROXY_BASE)/go-proxy/
export GOSUMDB := off
export NPM_CONFIG_REGISTRY := $(DEPENDENCY_PROXY_BASE)/npm-proxy/
export PIP_INDEX_URL := $(DEPENDENCY_PROXY_BASE)/pypi-proxy/simple
export PIP_TRUSTED_HOST := $(DEPENDENCY_PROXY_HOST)
export UV_INDEX_URL := $(DEPENDENCY_PROXY_BASE)/pypi-proxy/simple
export CARGO_REGISTRIES_CRATES_IO_INDEX := sparse+$(DEPENDENCY_PROXY_BASE)/cargo-proxy/
export DEPENDENCY_GITHUB_RAW_URL := $(DEPENDENCY_PROXY_BASE)/github-raw
export DEPENDENCY_GITLAB_RAW_URL := $(DEPENDENCY_PROXY_BASE)/gitlab-raw
export DEPENDENCY_GIT_MIRROR_URL := $(DEPENDENCY_GIT_MIRROR_BASE)
export GIT_CONFIG_COUNT := 2
export GIT_CONFIG_KEY_0 := url.$(DEPENDENCY_GIT_MIRROR_BASE)/github.com/.insteadOf
export GIT_CONFIG_VALUE_0 := https://github.com/
export GIT_CONFIG_KEY_1 := url.$(DEPENDENCY_GIT_MIRROR_BASE)/gitlab.com/.insteadOf
export GIT_CONFIG_VALUE_1 := https://gitlab.com/
ifeq ($(origin DEPENDENCY_REAL_DOCKER),undefined)
DEPENDENCY_REAL_DOCKER := $(shell command -v docker)
endif
export DEPENDENCY_REAL_DOCKER
export PATH := $(CURDIR)/scripts/dependency-proxy-bin:$(PATH)
endif

LOCAL_DEPENDENCIES_DIR := $(abspath .dependencies)
ifneq ($(wildcard $(LOCAL_DEPENDENCIES_DIR)/.),)
DEPENDENCIES_DIR ?= $(LOCAL_DEPENDENCIES_DIR)
export DEPENDENCIES_DIR
endif

.PHONY: test-paths dependency-manifests tooling structure signatures config config-core config-schema \
	config-runtime config-runtime-core config-runtime-go \
	config-runtime-typescript dependencies pools operators serde transports \
	kafka temporal tracing metrics dashboards dashboards-core logging scenarios \
	call-semantics sanitizers standalone-components published-components kubernetes \
	generation profiling profiling-all profiling-tests \
	profiling-durable profiling-durable-quick \
	benchmarks benchmark benchmarks-quick benchmarks-durable benchmarks-durable-quick benchmarks-tests \
	fast integration release resume all clean dependency-source-cache-invalidate \
	cold-gates cold-gates-resume

.NOTPARALLEL: fast integration release resume all

# Every public verification target must reject stale module manifests before
# starting downloads, Docker builds or runtime scenarios. In one make process
# this phony prerequisite executes once even when several suites are selected.
MANIFEST_GATED_TARGETS := tooling structure signatures config-core config-schema \
	config-runtime-core config-runtime-go config-runtime-typescript dependencies \
	standalone-components published-components pools operators serde transports kafka temporal tracing \
	metrics dashboards-core logging scenarios call-semantics sanitizers generation kubernetes profiling benchmarks
$(MANIFEST_GATED_TARGETS): dependency-manifests

test-paths:
	python3 -m unittest test_paths

dependency-source-cache-invalidate:
	python3 cpp_source_cache.py invalidate

dependency-manifests:
	python3 run_suite.py dependency-manifests python3 dependency_manifests/run.py

tooling:
	python3 run_suite.py tooling python3 tooling/run.py

all: release

release: fast integration profiling
	python3 aggregate.py

fast: dependency-manifests tooling structure signatures config pools operators serde

integration: config-runtime dependencies standalone-components published-components transports kafka temporal tracing metrics dashboards logging scenarios call-semantics sanitizers generation kubernetes

resume:
	python3 resume.py

cold-gates:
	+MAKE_COMMAND="$(MAKE)" bash scripts/run-cold-gates.sh

cold-gates-resume:
	+MAKE_COMMAND="$(MAKE)" bash scripts/run-cold-gates.sh --resume

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

standalone-components-resume:
	python3 standalone_components/run.py --resume-failed

published-components:
	python3 run_suite.py published-components python3 published_components/run.py

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

dashboards: metrics kafka temporal transports dashboards-core

dashboards-core:
	python3 run_suite.py dashboards python3 dashboards/run.py

logging:
	python3 run_suite.py logging python3 logging/run.py

scenarios:
	python3 run_suite.py scenarios python3 scenarios/run.py

call-semantics:
	python3 run_suite.py call-semantics python3 call_semantics/run.py

sanitizers:
	python3 run_suite.py sanitizers python3 sanitizers/run.py

generation:
	python3 run_suite.py generation python3 generation/run.py

kubernetes:
	python3 run_suite.py kubernetes python3 kubernetes/run.py

profiling:
	python3 run_suite.py profiling python3 profiling/run.py

profiling-all:
	python3 profiling/examples/run.py \
		--graph-profile "$${EXAMPLE_PROFILE:-function-call}" \
		$(PROFILING_ARGS)

profiling-durable:
	python3 profiling/examples/durable.py $(PROFILING_ARGS)

profiling-durable-quick:
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
	python3 benchmarks/examples/durable.py $(BENCHMARK_ARGS)

benchmarks-durable-quick:
	python3 benchmarks/examples/durable.py --skip-build --jobs 10 \
		--warmup-jobs 1 --runs 1 $(BENCHMARK_ARGS)

benchmarks-tests:
	python3 -m unittest discover -s benchmarks/examples -p 'test_*.py' -v

clean:
	@for path in .artifacts benchmarks/examples/.artifacts profiling/examples/.artifacts; do \
		if [ -e "$$path" ]; then chmod -R u+w "$$path"; fi; \
	done
	rm -rf .artifacts benchmarks/examples/.artifacts profiling/examples/.artifacts
