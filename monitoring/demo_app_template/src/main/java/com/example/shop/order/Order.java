package com.example.shop.order;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;

import jakarta.persistence.CascadeType;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.OneToMany;
import jakarta.persistence.Table;

@Entity
@Table(name = "orders")
public class Order {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;
    private Long customerId;
    private Instant createdAt;
    private BigDecimal total;
    private String status;
    private String giftMessage;

    @OneToMany(mappedBy = "order", cascade = CascadeType.ALL)
    private List<OrderItem> items = new ArrayList<>();

    protected Order() {
    }

    public Order(Long customerId, BigDecimal total) {
        this.customerId = customerId;
        this.total = total;
        this.createdAt = Instant.now();
        this.status = "PLACED";
    }

    public void addItem(Long productId, int quantity, BigDecimal price) {
        items.add(new OrderItem(this, productId, quantity, price));
    }

    public Long getId() { return id; }
    public Long getCustomerId() { return customerId; }
    public Instant getCreatedAt() { return createdAt; }
    public BigDecimal getTotal() { return total; }
    public String getStatus() { return status; }
    public String getGiftMessage() { return giftMessage; }
    public List<OrderItem> getItems() { return items; }

    public void setGiftMessage(String giftMessage) {
        this.giftMessage = giftMessage;
    }
}
