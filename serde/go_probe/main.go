package main

import (
	"encoding/hex"
	"fmt"
	"math"

	"github.com/gorundebug/servicelib/runtime/datastruct"
	"github.com/gorundebug/servicelib/runtime/serde"
)

func emit(name string, data []byte, err error) {
	if err != nil {
		panic(fmt.Sprintf("%s: %v", name, err))
	}
	fmt.Printf("%s=%s\n", name, hex.EncodeToString(data))
}

func serialize[T any](name string, serializer serde.Serde[T], value T) {
	data, err := serializer.Serialize(value, nil)
	emit(name, data, err)
}

func main() {
	serialize("bool_false", &serde.BoolSerde{}, false)
	serialize("bool_true", &serde.BoolSerde{}, true)
	serialize("int8_negative", &serde.Int8Serde{}, int8(-1))
	serialize("int16_min", &serde.Int16Serde{}, int16(math.MinInt16))
	serialize("int16_negative", &serde.Int16Serde{}, int16(-1))
	serialize("int16_zero", &serde.Int16Serde{}, int16(0))
	serialize("int16_max", &serde.Int16Serde{}, int16(math.MaxInt16))
	serialize("int32_negative", &serde.Int32Serde{}, int32(-1))
	serialize("int32_zero", &serde.Int32Serde{}, int32(0))
	serialize("int64_negative", &serde.Int64Serde{}, int64(-1))
	serialize("int64_zero", &serde.Int64Serde{}, int64(0))
	serialize("uint16", &serde.UInt16Serde{}, uint16(0x0102))
	serialize("uint32", &serde.UInt32Serde{}, uint32(0x01020304))
	serialize("uint64", &serde.UInt64Serde{}, uint64(0x0102030405060708))
	serialize("rune", &serde.RuneSerde{}, rune('Ж'))
	serialize("float32", &serde.Float32Serde{}, math.Float32frombits(0x7fc01234))
	serialize("float64", &serde.Float64Serde{}, math.Float64frombits(0x7ff8000000001234))
	serialize("string", &serde.StringSerde{}, "A\x00B")
	serialize("bytes", &serde.BytesSerde{}, []byte{0x00, 0x7f, 0xff})
	serialize("int16_array", &serde.Int16ArraySerde{}, []int16{math.MinInt16, -1, 0, math.MaxInt16})
	serialize("int32_array", &serde.Int32ArraySerde{}, []int32{-1, 0, 1})
	serialize("int64_array", &serde.Int64ArraySerde{}, []int64{-1, 0, 1})
	serialize("string_array", &serde.StringArraySerde{}, []string{"one", "", "three"})
	kvSerde := serde.MakeStreamKeyValueSerde[int32, string](
		&serde.Int32Serde{}, &serde.StringSerde{})
	data, err := kvSerde.Serialize(datastruct.KeyValue[int32, string]{Key: -7, Value: "seven"})
	emit("key_value", data, err)
}
