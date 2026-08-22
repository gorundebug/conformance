import { performance } from "node:perf_hooks";

import {
  DelayPool,
  JsonSerde,
  MessageContext,
  RuntimeConfigStore,
  SerdeType,
  ServiceEnvironment,
  errorSerdeType,
  makeDefaultSerdeRegistry,
  makeStreamSerde,
} from "@gorundebug/tsservicelib/runtime";

import { buildStreamGraph } from "/app/dist/internal/app/service.generated.js";
import { Config } from "/app/dist/internal/config/config.js";
import {
  ServiceIds,
  StreamIds,
} from "/app/dist/internal/config/config.generated.js";
import {
  makeMapOrderItemResultToOrderState,
  makeMapToOrderProcessed,
  makeMapToOrderState,
  makeOrderProcessedEndpoint,
  makeProcessOrder,
  makeProcessOrderItem,
  makeProcessOrderItems,
  makeSoftDeadline,
} from "/app/dist/internal/functions/index.generated.js";

function positiveInteger(name, fallback) {
  const raw = process.env[name];
  if (raw === undefined || raw.trim() === "") return fallback;
  const value = Number.parseInt(raw, 10);
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new Error(`${name} must be a positive integer`);
  }
  return value;
}

function isRecord(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function makeRegistry() {
  const registry = makeDefaultSerdeRegistry();
  const types = {
    order: new SerdeType("Order", isRecord),
    orderItem: new SerdeType("OrderItem", isRecord),
    orderItemResult: new SerdeType("OrderItemResult", isRecord),
    orderProcessed: new SerdeType("OrderProcessed", isRecord),
    orderState: new SerdeType("OrderState", isRecord),
  };
  for (const type of Object.values(types)) {
    registry.register(type, makeStreamSerde(new JsonSerde(type)));
  }
  registry.registerStreamValueType(StreamIds.PROCESS_ORDER, types.order);
  registry.registerStreamErrorType(StreamIds.PROCESS_ORDER, errorSerdeType);
  registry.registerStreamValueType(StreamIds.SPLIT_PIPELINE, types.order);
  registry.registerStreamValueType(
    StreamIds.PROCESS_ORDER_ITEMS,
    types.orderItem,
  );
  registry.registerStreamValueType(
    StreamIds.PROCESS_ORDER_ITEM,
    types.orderItemResult,
  );
  registry.registerStreamErrorType(
    StreamIds.PROCESS_ORDER_ITEM,
    types.orderState,
  );
  registry.registerStreamValueType(
    StreamIds.MAP_ORDER_ITEM_RESULT_TO_ORDER_STATE,
    types.orderState,
  );
  registry.registerStreamValueType(StreamIds.SOFT_DEADLINE, types.order);
  registry.registerStreamValueType(
    StreamIds.MAP_TO_ORDER_STATE,
    types.orderState,
  );
  registry.registerStreamValueType(StreamIds.MERGE_RESULTS, types.orderState);
  registry.registerStreamValueType(
    StreamIds.SPLIT_ORDER_RESULT,
    types.orderState,
  );
  registry.registerStreamValueType(
    StreamIds.MAP_TO_ORDER_PROCESSED,
    types.orderProcessed,
  );
  registry.registerStreamValueType(
    StreamIds.PUBLISH_ORDER_PROCESSED,
    types.orderProcessed,
  );
  registry.registerStreamErrorType(
    StreamIds.PUBLISH_ORDER_PROCESSED,
    errorSerdeType,
  );
  return registry;
}

const config = await Config.load([
  "--config",
  "/app/config/config.yaml",
  "--values",
  "/app/config/docker_overrides.yaml",
]);
const delayPool = new DelayPool();
const environment = new ServiceEnvironment(
  new RuntimeConfigStore(config.runtime),
  ServiceIds.ORDER_SERVICE,
  undefined,
  delayPool,
  makeRegistry(),
);
const graph = buildStreamGraph(new MessageContext(), config, environment, {
  mapOrderItemResultToOrderState: makeMapOrderItemResultToOrderState,
  mapToOrderProcessed: makeMapToOrderProcessed,
  mapToOrderState: makeMapToOrderState,
  orderProcessedEndpoint: makeOrderProcessedEndpoint,
  processOrder: makeProcessOrder,
  processOrderItem: makeProcessOrderItem,
  processOrderItems: makeProcessOrderItems,
  softDeadline: makeSoftDeadline,
});
await environment.buildRuntimeStreams();
environment.validateRuntimeTopology();

graph.streams.processOrderItem.setSinkConsumer({
  consume(context, item) {
    return graph.streams.processOrderItem.consumeResult(context, {
      orderId: item.orderId,
      itemId: item.itemId,
      sku: item.sku,
      requestedQty: item.quantity,
      availableQty: item.quantity,
      reserved: true,
      status: "CONFIRMED",
      unitPrice: item.unitPrice,
      error: "",
    });
  },
});
graph.streams.publishOrderProcessed.setSinkConsumer({
  consume() {},
});

const pending = new Map();
graph.streams.processOrder.setResultConsumer({
  consume(_context, state) {
    const request = pending.get(state.orderId);
    if (request === undefined) return;
    pending.delete(state.orderId);
    request.abort.abort();
    request.resolve();
  },
});

const vus = positiveInteger("BENCHMARK_VUS", 256);
const durationSeconds = positiveInteger("BENCHMARK_DURATION_SECONDS", 20);
const deadline = performance.now() + durationSeconds * 1_000;
let requests = 0;

async function virtualUser(index) {
  let sequence = 0;
  while (performance.now() < deadline) {
    const orderId = `${String(index)}-${String(sequence)}`;
    sequence += 1;
    const abort = new AbortController();
    const response = new Promise((resolve) => {
      pending.set(orderId, { abort, resolve });
    });
    const context = new MessageContext(abort.signal);
    try {
      const completion = graph.streams.processOrder.consume(context, {
        id: orderId,
        customerId: "benchmark-customer",
        items: [
          {
            orderId,
            itemId: `${orderId}-item`,
            sku: "missing-sku",
            quantity: 1,
            unitPrice: 10,
          },
        ],
        totalAmount: 10,
        createdAt: new Date(),
        traceId: "",
      });
      if (completion !== undefined) await completion;
      await response;
      requests += 1;
    } catch (error) {
      pending.delete(orderId);
      abort.abort(error);
      throw error;
    }
  }
}

const started = performance.now();
await Promise.all(
  Array.from({ length: vus }, (_, index) => virtualUser(index)),
);
const elapsedSeconds = (performance.now() - started) / 1_000;
if (pending.size !== 0) {
  throw new Error(
    `graph benchmark leaked ${String(pending.size)} pending requests`,
  );
}
const result = {
  scenario: "typescript_graph_without_grpc",
  vus,
  duration_seconds: durationSeconds,
  elapsed_seconds: elapsedSeconds,
  requests,
  requests_per_second: requests / elapsedSeconds,
};
console.log(JSON.stringify(result));
