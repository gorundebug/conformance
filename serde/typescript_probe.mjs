#!/usr/bin/env node
import { pathToFileURL } from "node:url";
import path from "node:path";

const root = process.env.TSSERVICELIB_ROOT;
if (root === undefined || root.length === 0) {
  throw new Error("TSSERVICELIB_ROOT is required");
}

const serde = await import(pathToFileURL(path.join(root, "dist/runtime/serde/index.js")).href);

function emit(name, serializer, value) {
  console.log(`${name}=${Buffer.from(serializer.serialize(value)).toString("hex")}`);
}

const float32Bytes = Buffer.from("7fc01234", "hex");
const f32 = new DataView(
  float32Bytes.buffer,
  float32Bytes.byteOffset,
  float32Bytes.byteLength
).getFloat32(0, false);
const float64Bytes = Buffer.from("7ff8000000001234", "hex");
const f64 = new DataView(
  float64Bytes.buffer,
  float64Bytes.byteOffset,
  float64Bytes.byteLength
).getFloat64(0, false);

emit("bool_false", new serde.BoolSerde(), false);
emit("bool_true", new serde.BoolSerde(), true);
emit("int8_negative", new serde.Int8Serde(), -1);
emit("int16_min", new serde.Int16Serde(), -(2 ** 15));
emit("int16_negative", new serde.Int16Serde(), -1);
emit("int16_zero", new serde.Int16Serde(), 0);
emit("int16_max", new serde.Int16Serde(), 2 ** 15 - 1);
emit("int32_negative", new serde.Int32Serde(), -1);
emit("int32_zero", new serde.Int32Serde(), 0);
emit("int64_negative", new serde.Int64Serde(), -1n);
emit("int64_zero", new serde.Int64Serde(), 0n);
emit("uint16", new serde.UInt16Serde(), 0x0102);
emit("uint32", new serde.UInt32Serde(), 0x01020304);
emit("uint64", new serde.UInt64Serde(), 0x0102030405060708n);
emit("rune", new serde.RuneSerde(), "Ж".codePointAt(0));
emit("float32", new serde.Float32Serde(), f32);
emit("float64", new serde.Float64Serde(), f64);
emit("string", new serde.StringSerde(), "A\0B");
emit("bytes", new serde.BytesSerde(), Uint8Array.from([0x00, 0x7f, 0xff]));
emit("int16_array", new serde.Int16ArraySerde(), [-(2 ** 15), -1, 0, 2 ** 15 - 1]);
emit("int32_array", new serde.Int32ArraySerde(), [-1, 0, 1]);
emit("int64_array", new serde.Int64ArraySerde(), [-1n, 0n, 1n]);
emit("string_array", new serde.StringArraySerde(), ["one", "", "three"]);
emit(
  "key_value",
  new serde.StreamKeyValueSerde(new serde.Int32Serde(), new serde.StringSerde()),
  { key: -7, value: "seven" }
);
