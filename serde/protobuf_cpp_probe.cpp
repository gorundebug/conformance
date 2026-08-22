#include <proto/inventoryserviceapi/processorderitem/processorderitem.pb.h>

#include <google/protobuf/io/coded_stream.h>
#include <google/protobuf/io/zero_copy_stream_impl_lite.h>
#include <google/protobuf/message_lite.h>

#include <cstddef>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>

namespace {

void Emit(std::string_view name, std::string_view value) {
  std::cout << name << '=';
  for (const unsigned char byte : value) {
    std::cout << std::hex << std::setfill('0') << std::setw(2)
              << static_cast<unsigned>(byte);
  }
  std::cout << '\n';
}

std::string DeterministicSerialize(const google::protobuf::MessageLite& message) {
  std::string output;
  google::protobuf::io::StringOutputStream stream(&output);
  google::protobuf::io::CodedOutputStream coded(&stream);
  coded.SetSerializationDeterministic(true);
  if (!message.SerializeToCodedStream(&coded) || coded.HadError()) {
    throw std::runtime_error("deterministic protobuf serialization failed");
  }
  coded.Trim();
  return output;
}

template <typename Message>
Message Parse(std::string_view wire) {
  Message message;
  if (!message.ParseFromArray(wire.data(), static_cast<int>(wire.size()))) {
    throw std::runtime_error("protobuf parse failed");
  }
  return message;
}

}  // namespace

int main() {
  processorderitem::ProcessOrderItemRequest request;
  request.set_order_id(
      std::string{"order\0ид", sizeof("order\0ид") - 1});
  request.set_item_id("item-42");
  request.set_sku("sku/β");
  request.set_quantity(-7);
  const auto requestWire = DeterministicSerialize(request);
  Emit("protobuf_request_wire", requestWire);

  const auto decodedRequest =
      Parse<processorderitem::ProcessOrderItemRequest>(requestWire);
  Emit("protobuf_request_roundtrip_wire",
       DeterministicSerialize(decodedRequest));
  Emit("protobuf_request_order_id", decodedRequest.order_id());
  Emit("protobuf_request_item_id", decodedRequest.item_id());
  Emit("protobuf_request_sku", decodedRequest.sku());
  Emit("protobuf_request_quantity", std::to_string(decodedRequest.quantity()));

  processorderitem::ProcessOrderItemResponse response;
  response.set_available_qty(42);
  response.set_reserved(true);
  response.set_status(std::string{"зарезервирован\0ok",
                                  sizeof("зарезервирован\0ok") - 1});
  const auto responseWire = DeterministicSerialize(response);
  Emit("protobuf_response_wire", responseWire);
  const auto decodedResponse =
      Parse<processorderitem::ProcessOrderItemResponse>(responseWire);
  Emit("protobuf_response_roundtrip_wire",
       DeterministicSerialize(decodedResponse));
  Emit("protobuf_response_available_qty",
       std::to_string(decodedResponse.available_qty()));
  Emit("protobuf_response_reserved", decodedResponse.reserved() ? "true" : "false");
  Emit("protobuf_response_status", decodedResponse.status());

  Emit("protobuf_request_default_wire",
       DeterministicSerialize(processorderitem::ProcessOrderItemRequest{}));
  Emit("protobuf_response_default_wire",
       DeterministicSerialize(processorderitem::ProcessOrderItemResponse{}));

  auto unknownWire = requestWire;
  unknownWire.append("\x98\x06\x07", 3);
  const auto unknownRequest =
      Parse<processorderitem::ProcessOrderItemRequest>(unknownWire);
  Emit("protobuf_request_unknown_roundtrip_wire",
       DeterministicSerialize(unknownRequest));
}
