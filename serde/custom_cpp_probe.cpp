#if defined(SERVICELIB_CUSTOM_SERDE_BOOST)
#include <model_cpp/include/example/model/serdes/order_item_result_serde.hpp>
#include <model_cpp/include/example/model/serdes/order_item_serde.hpp>
#include <orderservice/internal/serdes/order_serde.hpp>
#include <orderservice/internal/serdes/order_state_serde.hpp>
#elif defined(SERVICELIB_CUSTOM_SERDE_CANONICAL)
#include <model_cpp/include/example/model/serdes/order_item_result_serde.hpp>
#include <model_cpp/include/example/model/serdes/order_item_serde.hpp>
#include <orderservice/internal/serdes/order_serde.hpp>
#include <orderservice/internal/serdes/order_state_serde.hpp>
#else
#error "custom serde runtime is not selected"
#endif
#include <orderservice/internal/serdes/serde_registration.generated.hpp>

#include <any>
#include <cstddef>
#include <iostream>
#include <stdexcept>
#include <string>

namespace {

void Require(bool condition, const char* message) {
  if (!condition) {
    throw std::runtime_error(message);
  }
}

std::string Text(const servicelib::serde::SerdeData& data) {
  return {reinterpret_cast<const char*>(data.data()), data.size()};
}

void Emit(const char* name, const servicelib::serde::SerdeData& data) {
  std::cout << name << '=' << Text(data) << '\n';
}

template <typename T>
void ValidateGeneratedTypeErasure(const T& value, const char* name) {
  const auto typed = servicelib::serde::MakeDefaultSerde<T>();
  Require(typed != nullptr, "generated default serde is null");
  const servicelib::serde::Serializer& erased = *typed;
  const auto encoded = erased.SerializeObj(std::any{value});
  const auto decoded = std::any_cast<T>(erased.DeserializeObj(encoded));
  Require(erased.SerializeObj(std::any{decoded}) == encoded,
          "generated type-erased serde round trip");
  bool rejected = false;
  try {
    static_cast<void>(erased.SerializeObj(std::any{std::string{name}}));
  } catch (const std::invalid_argument&) {
    rejected = true;
  }
  Require(rejected, "generated type-erased serde accepted wrong object type");
}

}  // namespace

int main() {
  using example::model::types::OrderItem;
  using example::model::types::OrderItemResult;
  using example::order_service::types::Order;
  using example::order_service::types::OrderState;

  const OrderItem item{"order-1", "item-1", "SKU-1", 2, 12.5};
  const example::model::types::serde::OrderItemSerde itemSerde;
  const auto itemEncoded = itemSerde.Serialize(item);
  const auto itemDecoded = itemSerde.Deserialize(itemEncoded);
  Require(itemDecoded.order_id == item.order_id, "OrderItem order_id");
  Require(itemDecoded.item_id == item.item_id, "OrderItem item_id");
  Require(itemDecoded.sku == item.sku, "OrderItem sku");
  Require(itemDecoded.quantity == item.quantity, "OrderItem quantity");
  Require(itemDecoded.unit_price == item.unit_price, "OrderItem unit_price");
  ValidateGeneratedTypeErasure(item, "order_item");
  Emit("order_item", itemEncoded);

  const OrderItemResult itemResult{"order-1", "item-1", "SKU-1", 2, 7,
                                   true, "reserved", 12.5, ""};
  const example::model::types::serde::OrderItemResultSerde itemResultSerde;
  const auto itemResultEncoded = itemResultSerde.Serialize(itemResult);
  const auto itemResultDecoded = itemResultSerde.Deserialize(itemResultEncoded);
  Require(itemResultDecoded.order_id == itemResult.order_id,
          "OrderItemResult order_id");
  Require(itemResultDecoded.available_qty == itemResult.available_qty,
          "OrderItemResult available_qty");
  Require(itemResultDecoded.reserved == itemResult.reserved,
          "OrderItemResult reserved");
  Require(itemResultDecoded.error == itemResult.error,
          "OrderItemResult error");
  ValidateGeneratedTypeErasure(itemResult, "order_item_result");
  Emit("order_item_result", itemResultEncoded);

  const Order order{"order-1", "customer-1", {item}, 25.0,
                    "2026-08-10T00:00:00Z", "trace-1"};
  const example::order_service::types::serde::OrderSerde orderSerde;
  const auto orderEncoded = orderSerde.Serialize(order);
  const auto orderDecoded = orderSerde.Deserialize(orderEncoded);
  Require(orderDecoded.id == order.id, "Order id");
  Require(orderDecoded.customer_id == order.customer_id, "Order customer_id");
  Require(orderDecoded.items.size() == 1, "Order items size");
  Require(orderDecoded.items.front().sku == item.sku, "Order nested item");
  Require(orderDecoded.total_amount == order.total_amount, "Order total_amount");
  Require(orderDecoded.trace_id == order.trace_id, "Order trace_id");
  ValidateGeneratedTypeErasure(order, "order");
  Emit("order", orderEncoded);

  const OrderState state{"order-1", "confirmed", {itemResult}, 25.0,
                         "2026-08-10T00:00:01Z"};
  const example::order_service::types::serde::OrderStateSerde stateSerde;
  const auto stateEncoded = stateSerde.Serialize(state);
  const auto stateDecoded = stateSerde.Deserialize(stateEncoded);
  Require(stateDecoded.order_id == state.order_id, "OrderState order_id");
  Require(stateDecoded.status == state.status, "OrderState status");
  Require(stateDecoded.confirmed_items.size() == 1,
          "OrderState confirmed_items size");
  Require(stateDecoded.confirmed_items.front().item_id == itemResult.item_id,
          "OrderState nested result");
  Require(stateDecoded.total_amount == state.total_amount,
          "OrderState total_amount");
  ValidateGeneratedTypeErasure(state, "order_state");
  Emit("order_state", stateEncoded);
  std::cout << "generated_type_erasure_checks=4\n";
}
