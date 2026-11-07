package com.example.shop.order;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;

import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;

public final class OrderDtos {

    private OrderDtos() {
    }

    public record OrderRequest(@NotNull Long customerId, @NotNull Long productId, @Min(1) int quantity,
                               @NotBlank String cardNumber, String giftMessage) {
    }

    public record OrderResponse(Long orderId, String status, BigDecimal total) {
    }

    public record OrderDetail(Long id, Long customerId, Instant createdAt, BigDecimal total, String status,
                              List<Item> items) {

        public record Item(Long productId, int quantity, BigDecimal price) {
        }

        static OrderDetail of(Order o) {
            return new OrderDetail(o.getId(), o.getCustomerId(), o.getCreatedAt(), o.getTotal(), o.getStatus(),
                    o.getItems().stream().map(i -> new Item(i.getProductId(), i.getQuantity(), i.getPrice())).toList());
        }
    }
}
