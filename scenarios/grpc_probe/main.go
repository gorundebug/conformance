package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	inventoryserviceapi "github.com/gorundebug/inventory_service_api/pkg/generated/proto/inventoryserviceapi"
	processorderitem "github.com/gorundebug/inventory_service_api/pkg/generated/proto/inventoryserviceapi/processorderitem"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

type output struct {
	Mode     string              `json:"mode"`
	Code     string              `json:"code"`
	Message  string              `json:"message"`
	Header   map[string][]string `json:"headers"`
	Trailer  map[string][]string `json:"trailers"`
	Response *response           `json:"response,omitempty"`
}

type response struct {
	AvailableQty int32  `json:"available_qty"`
	Reserved     bool   `json:"reserved"`
	Status       string `json:"status"`
}

func normalizedMetadata(value metadata.MD) map[string][]string {
	result := make(map[string][]string, len(value))
	for key, values := range value {
		result[key] = append([]string(nil), values...)
	}
	return result
}

func main() {
	address := flag.String("address", "inventoryservice:9202", "gRPC server address")
	mode := flag.String("mode", "success", "success, deadline or cancel")
	itemID := flag.String("item-id", "grpc-conformance", "request item ID")
	flag.Parse()

	connection, err := grpc.NewClient(*address, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer connection.Close()

	ctx := context.Background()
	cancel := func() {}
	switch *mode {
	case "success":
		ctx, cancel = context.WithTimeout(ctx, 5*time.Second)
	case "deadline":
		ctx, cancel = context.WithTimeout(ctx, 250*time.Millisecond)
	case "cancel":
		var cancelContext context.CancelFunc
		ctx, cancelContext = context.WithCancel(ctx)
		cancel = cancelContext
		time.AfterFunc(100*time.Millisecond, cancelContext)
	default:
		fmt.Fprintf(os.Stderr, "unknown mode %q\n", *mode)
		os.Exit(2)
	}
	defer cancel()

	var header metadata.MD
	var trailer metadata.MD
	reply, callErr := inventoryserviceapi.NewInventoryServiceApiClient(connection).
		ProcessOrderItem(
			ctx,
			&processorderitem.ProcessOrderItemRequest{
				OrderId:  "grpc-conformance",
				ItemId:   *itemID,
				Sku:      "SKU-003",
				Quantity: 2,
			},
			grpc.Header(&header),
			grpc.Trailer(&trailer),
			grpc.WaitForReady(true),
		)

	result := output{
		Mode:    *mode,
		Code:    status.Code(callErr).String(),
		Header:  normalizedMetadata(header),
		Trailer: normalizedMetadata(trailer),
	}
	if callErr != nil {
		result.Message = status.Convert(callErr).Message()
	} else {
		result.Response = &response{
			AvailableQty: reply.GetAvailableQty(),
			Reserved:     reply.GetReserved(),
			Status:       reply.GetStatus(),
		}
	}

	expected := codes.OK
	if *mode == "deadline" {
		expected = codes.DeadlineExceeded
	} else if *mode == "cancel" {
		expected = codes.Canceled
	}
	if status.Code(callErr) != expected {
		fmt.Fprintf(os.Stderr, "%s returned %s, expected %s\n", *mode, status.Code(callErr), expected)
		os.Exit(1)
	}
	if expected == codes.OK && (reply.GetAvailableQty() != 2 || !reply.GetReserved() || reply.GetStatus() != "CONFIRMED") {
		fmt.Fprintf(os.Stderr, "unexpected success response: %+v\n", reply)
		os.Exit(1)
	}

	encoded, err := json.Marshal(result)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	fmt.Println(string(encoded))
}
