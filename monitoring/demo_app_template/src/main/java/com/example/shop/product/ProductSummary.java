package com.example.shop.product;

import java.math.BigDecimal;

public record ProductSummary(Long id, String name, BigDecimal price, int stock, long reviewCount) {
}
