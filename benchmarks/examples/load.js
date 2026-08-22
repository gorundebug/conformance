import http from "k6/http";
import { check } from "k6";

const duration = __ENV.BENCHMARK_DURATION || "20s";
const durationSeconds = Number.parseFloat(
  __ENV.BENCHMARK_DURATION_SECONDS || "20",
);
const resultFile = __ENV.BENCHMARK_RESULT_FILE || "/results/k6.json";
const target = __ENV.BENCHMARK_TARGET ||
  "http://orderservice:9091/v1/processorder";
const method = (__ENV.BENCHMARK_METHOD || "POST").toUpperCase();
const expectedStatus = Number.parseInt(
  __ENV.BENCHMARK_EXPECTED_STATUS || "200",
  10,
);
const scenario = __ENV.BENCHMARK_SCENARIO || "process_order_out_of_stock";
const payloadMode = __ENV.BENCHMARK_PAYLOAD_MODE || "normal";
// k6 treats every 4xx/5xx as http_req_failed unless the expected-response
// callback is configured independently from checks. Isolation scenarios use
// an intentional 404 and must still report a zero transport error rate.
http.setResponseCallback(http.expectedStatuses(expectedStatus));
const vus = Number.parseInt(__ENV.BENCHMARK_VUS || "32", 10);
const mode = __ENV.BENCHMARK_MODE || "closed";
const targetRate = Number.parseInt(__ENV.BENCHMARK_RATE || "0", 10);
const preAllocatedVUs = Number.parseInt(
  __ENV.BENCHMARK_PRE_ALLOCATED_VUS || "128",
  10,
);
const maxVUs = Number.parseInt(__ENV.BENCHMARK_MAX_VUS || "4096", 10);

const commonOptions = {
  discardResponseBodies: true,
  noConnectionReuse: false,
  noVUConnectionReuse: false,
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

export const options = mode === "arrival-rate"
  ? {
      ...commonOptions,
      scenarios: {
        capacity: {
          executor: "constant-arrival-rate",
          rate: targetRate,
          timeUnit: "1s",
          duration,
          preAllocatedVUs,
          maxVUs,
          gracefulStop: "5s",
        },
      },
    }
  : {
      ...commonOptions,
      vus,
      duration,
    };

const normalPayload = JSON.stringify({
  customer_id: "benchmark-customer",
  items: [
    {
      item_id: "benchmark-item",
      // A missing SKU keeps every request on the same business path. Using
      // SKU-001 would exhaust the in-memory stock during the benchmark.
      sku: "BENCHMARK-MISSING-SKU",
      quantity: 1,
      unit_price: 799.0,
    },
  ],
});
const payload = payloadMode === "invalid-json" ? "{" : normalPayload;

const params = {
  headers: {
    "Content-Type": "application/json",
  },
  tags: {
    scenario,
  },
};

export default function () {
  const response = method === "GET"
    ? http.get(target, params)
    : http.post(target, payload, params);
  check(response, {
    "HTTP status is expected": (value) => value.status === expectedStatus,
  });
}

export function handleSummary(data) {
  const requests = data.metrics.http_reqs?.values || {};
  const durationValues = data.metrics.http_req_duration?.values || {};
  const failed = data.metrics.http_req_failed?.values || {};
  const checks = data.metrics.checks?.values || {};
  const dropped = data.metrics.dropped_iterations?.values || {};
  const iterations = data.metrics.iterations?.values || {};
  const scheduledIterations =
    (iterations.count || requests.count || 0) + (dropped.count || 0);
  const summary = {
    scenario,
    mode,
    target_rate: targetRate,
    request_count: requests.count || 0,
    // k6's built-in rate includes time spent waiting for iterations during
    // gracefulStop. That makes a single late request under-report throughput
    // for the fixed measurement window. Count only the configured duration.
    requests_per_second: durationSeconds > 0
      ? (requests.count || 0) / durationSeconds
      : 0,
    iteration_count: iterations.count || 0,
    dropped_iterations: dropped.count || 0,
    dropped_rate: scheduledIterations > 0
      ? (dropped.count || 0) / scheduledIterations
      : 0,
    error_rate: Math.max(failed.rate || 0, 1 - (checks.rate ?? 1)),
    latency_ms: {
      avg: durationValues.avg || 0,
      p50: durationValues.med || 0,
      p90: durationValues["p(90)"] || 0,
      p95: durationValues["p(95)"] || 0,
      p99: durationValues["p(99)"] || 0,
      max: durationValues.max || 0,
    },
  };
  return {
    [resultFile]: JSON.stringify(summary, null, 2) + "\n",
    stdout:
      `requests=${summary.request_count} ` +
      `rate=${summary.requests_per_second.toFixed(2)}/s ` +
      `target=${summary.target_rate}/s ` +
      `dropped=${summary.dropped_iterations} ` +
      `p95=${summary.latency_ms.p95.toFixed(2)}ms ` +
      `p99=${summary.latency_ms.p99.toFixed(2)}ms ` +
      `errors=${(summary.error_rate * 100).toFixed(4)}%\n`,
  };
}
