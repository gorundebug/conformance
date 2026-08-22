module servicelib-conformance-protobuf-probe

go 1.25.4

require (
	github.com/gorundebug/inventory_service_api v0.0.0
	google.golang.org/protobuf v1.36.11
)

replace github.com/gorundebug/inventory_service_api => ../../../goexample/inventory_service_api
