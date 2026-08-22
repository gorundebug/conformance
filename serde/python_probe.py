#!/usr/bin/env python3
"""Emit the canonical deterministic serde fixture matrix."""

from __future__ import annotations

import struct

from pyservicelib_gorundebug.runtime.datastruct import KeyValue
from pyservicelib_gorundebug.runtime.serde import (
    BoolSerde,
    BytesSerde,
    FloatSerde,
    IntListSerde,
    IntSerde,
    RuneSerde,
    StreamKeyValueSerde,
    StringListSerde,
    StringSerde,
)


def emit(name: str, serde: object, value: object) -> None:
    encoded = serde.serialize(value, bytearray())  # type: ignore[attr-defined]
    print(f"{name}={bytes(encoded).hex()}")


def main() -> None:
    emit("bool_false", BoolSerde("bool"), False)
    emit("bool_true", BoolSerde("bool"), True)
    emit("int8_negative", IntSerde("int8"), -1)
    emit("int16_min", IntSerde("int16"), -(2**15))
    emit("int16_negative", IntSerde("int16"), -1)
    emit("int16_zero", IntSerde("int16"), 0)
    emit("int16_max", IntSerde("int16"), 2**15 - 1)
    emit("int32_negative", IntSerde("int32"), -1)
    emit("int32_zero", IntSerde("int32"), 0)
    emit("int64_negative", IntSerde("int64"), -1)
    emit("int64_zero", IntSerde("int64"), 0)
    emit("uint16", IntSerde("uint16"), 0x0102)
    emit("uint32", IntSerde("uint32"), 0x01020304)
    emit("uint64", IntSerde("uint64"), 0x0102030405060708)
    emit("rune", RuneSerde(), ord("Ж"))
    emit("float32", FloatSerde("float32"), struct.unpack(">f", bytes.fromhex("7fc01234"))[0])
    emit(
        "float64",
        FloatSerde("float64"),
        struct.unpack(">d", bytes.fromhex("7ff8000000001234"))[0],
    )
    emit("string", StringSerde("str"), "A\0B")
    emit("bytes", BytesSerde("bytes"), bytes((0x00, 0x7F, 0xFF)))
    emit("int16_array", IntListSerde("[]int16"), [-(2**15), -1, 0, 2**15 - 1])
    emit("int32_array", IntListSerde("[]int32"), [-1, 0, 1])
    emit("int64_array", IntListSerde("[]int64"), [-1, 0, 1])
    emit("string_array", StringListSerde("[]string"), ["one", "", "three"])
    key_value = KeyValue(key=-7, value="seven")
    key_value_serde = StreamKeyValueSerde(IntSerde("int32"), StringSerde("str"))
    print(f"key_value={bytes(key_value_serde.serialize(key_value)).hex()}")


if __name__ == "__main__":
    main()
