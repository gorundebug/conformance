package main

import (
	"encoding/hex"
	"fmt"
	"strconv"

	processorderitem "github.com/gorundebug/inventory_service_api/pkg/generated/proto/inventoryserviceapi/processorderitem"
	"google.golang.org/protobuf/proto"
)

var deterministic = proto.MarshalOptions{Deterministic: true}

func emit(name string, value []byte) {
	fmt.Printf("%s=%s\n", name, hex.EncodeToString(value))
}

func marshal(message proto.Message) []byte {
	data, err := deterministic.Marshal(message)
	if err != nil {
		panic(err)
	}
	return data
}

func main() {
	request := &processorderitem.ProcessOrderItemRequest{
		OrderId:  "order\x00ид",
		ItemId:   "item-42",
		Sku:      "sku/β",
		Quantity: -7,
	}
	requestWire := marshal(request)
	emit("protobuf_request_wire", requestWire)

	decodedRequest := &processorderitem.ProcessOrderItemRequest{}
	if err := proto.Unmarshal(requestWire, decodedRequest); err != nil {
		panic(err)
	}
	emit("protobuf_request_roundtrip_wire", marshal(decodedRequest))
	emit("protobuf_request_order_id", []byte(decodedRequest.GetOrderId()))
	emit("protobuf_request_item_id", []byte(decodedRequest.GetItemId()))
	emit("protobuf_request_sku", []byte(decodedRequest.GetSku()))
	emit("protobuf_request_quantity", []byte(strconv.FormatInt(int64(decodedRequest.GetQuantity()), 10)))

	response := &processorderitem.ProcessOrderItemResponse{
		AvailableQty: 42,
		Reserved:     true,
		Status:       "зарезервирован\x00ok",
	}
	responseWire := marshal(response)
	emit("protobuf_response_wire", responseWire)
	decodedResponse := &processorderitem.ProcessOrderItemResponse{}
	if err := proto.Unmarshal(responseWire, decodedResponse); err != nil {
		panic(err)
	}
	emit("protobuf_response_roundtrip_wire", marshal(decodedResponse))
	emit("protobuf_response_available_qty", []byte(strconv.FormatInt(int64(decodedResponse.GetAvailableQty()), 10)))
	emit("protobuf_response_reserved", []byte(strconv.FormatBool(decodedResponse.GetReserved())))
	emit("protobuf_response_status", []byte(decodedResponse.GetStatus()))

	emit("protobuf_request_default_wire", marshal(&processorderitem.ProcessOrderItemRequest{}))
	emit("protobuf_response_default_wire", marshal(&processorderitem.ProcessOrderItemResponse{}))

	// Unknown field 99, wire type varint, value 7. Both protobuf runtimes must
	// preserve it when parsing and deterministically serializing the message.
	unknownWire := append(append([]byte{}, requestWire...), 0x98, 0x06, 0x07)
	unknownRequest := &processorderitem.ProcessOrderItemRequest{}
	if err := proto.Unmarshal(unknownWire, unknownRequest); err != nil {
		panic(err)
	}
	emit("protobuf_request_unknown_roundtrip_wire", marshal(unknownRequest))
}
