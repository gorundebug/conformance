#include <servicelib/runtime/serde/serdeimpl.hpp>

#include <bit>
#include <cstddef>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <limits>
#include <memory>
#include <string>
#include <utility>
#include <vector>

namespace {

void Emit(const char* name, const servicelib::serde::SerdeData& data) {
  std::cout << name << '=';
  for (const auto value : data) {
    std::cout << std::hex << std::setfill('0') << std::setw(2)
              << static_cast<unsigned>(std::to_integer<std::uint8_t>(value));
  }
  std::cout << '\n';
}

}  // namespace

int main() {
  using namespace servicelib::serde;
  Emit("bool_false", BoolSerde{}.Serialize(false));
  Emit("bool_true", BoolSerde{}.Serialize(true));
  Emit("int8_negative", Int8Serde{}.Serialize(-1));
  Emit("int16_min", Int16Serde{}.Serialize(std::numeric_limits<std::int16_t>::min()));
  Emit("int16_negative", Int16Serde{}.Serialize(-1));
  Emit("int16_zero", Int16Serde{}.Serialize(0));
  Emit("int16_max", Int16Serde{}.Serialize(std::numeric_limits<std::int16_t>::max()));
  Emit("int32_negative", Int32Serde{}.Serialize(-1));
  Emit("int32_zero", Int32Serde{}.Serialize(0));
  Emit("int64_negative", Int64Serde{}.Serialize(-1));
  Emit("int64_zero", Int64Serde{}.Serialize(0));
  Emit("uint16", UInt16Serde{}.Serialize(0x0102));
  Emit("uint32", UInt32Serde{}.Serialize(0x01020304));
  Emit("uint64", UInt64Serde{}.Serialize(0x0102030405060708ull));
  Emit("rune", RuneSerde{}.Serialize(U'Ж'));
  Emit("float32", Float32Serde{}.Serialize(
                      std::bit_cast<float>(std::uint32_t{0x7fc01234u})));
  Emit("float64", Float64Serde{}.Serialize(
                      std::bit_cast<double>(std::uint64_t{0x7ff8000000001234ull})));
  Emit("string", StringSerde{}.Serialize(std::string{"A\0B", 3}));
  Emit("bytes", UInt8ArraySerde{}.Serialize({0x00, 0x7f, 0xff}));
  Emit("int16_array", Int16ArraySerde{}.Serialize(
                          {std::numeric_limits<std::int16_t>::min(), -1, 0,
                           std::numeric_limits<std::int16_t>::max()}));
  Emit("int32_array", Int32ArraySerde{}.Serialize({-1, 0, 1}));
  Emit("int64_array", Int64ArraySerde{}.Serialize({-1, 0, 1}));
  Emit("string_array", StringArraySerde{}.Serialize({"one", "", "three"}));
  using KeyValue = std::pair<std::int32_t, std::string>;
  auto keyValueSerde = MakeStreamKeyValueSerde<std::int32_t, std::string, KeyValue>(
      std::make_shared<Int32Serde>(), std::make_shared<StringSerde>());
  Emit("key_value", keyValueSerde->Serialize(KeyValue{-7, "seven"}));
}
