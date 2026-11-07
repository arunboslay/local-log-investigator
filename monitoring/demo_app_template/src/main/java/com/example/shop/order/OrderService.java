package com.example.shop.order;

import java.math.BigDecimal;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionTemplate;

import com.example.shop.customer.Customer;
import com.example.shop.customer.CustomerRepository;
import com.example.shop.order.OrderDtos.OrderDetail;
import com.example.shop.order.OrderDtos.OrderRequest;
import com.example.shop.order.OrderDtos.OrderResponse;
import com.example.shop.payment.PaymentClient;
import com.example.shop.product.Product;
import com.example.shop.product.ProductRepository;
import com.example.shop.web.NotFoundException;

@Service
public class OrderService {

    private static final Logger log = LoggerFactory.getLogger(OrderService.class);

    private final OrderRepository orderRepository;
    private final CustomerRepository customerRepository;
    private final ProductRepository productRepository;
    private final PaymentClient paymentClient;
    private final TransactionTemplate tx;

    public OrderService(OrderRepository orderRepository, CustomerRepository customerRepository,
                        ProductRepository productRepository, PaymentClient paymentClient, TransactionTemplate tx) {
        this.orderRepository = orderRepository;
        this.customerRepository = customerRepository;
        this.productRepository = productRepository;
        this.paymentClient = paymentClient;
        this.tx = tx;
    }

    public OrderResponse placeOrder(OrderRequest request) {
        Customer customer = customerRepository.findById(request.customerId())
                .orElseThrow(() -> new NotFoundException("customer", request.customerId()));
        Product product = productRepository.findById(request.productId())
                .orElseThrow(() -> new NotFoundException("product", request.productId()));
        log.info("order validated customerId={} customer={} productId={} quantity={}",
                customer.getId(), customer.getName(), product.getId(), request.quantity());

        if (product.getStock() < request.quantity()) {
            throw new IllegalStateException("not enough stock for product " + product.getId());
        }
        log.info("stock checked productId={} available={}", product.getId(), product.getStock());

        BigDecimal total = product.getPrice().multiply(BigDecimal.valueOf(request.quantity()));
        String authId = paymentClient.charge(customer.getId(), request.cardNumber(), total);
        log.info("payment authorized authId={} amount={}", authId, total);

        Order order = buildOrder(customer, product, request, total);
        Order saved = tx.execute(status -> {
            productRepository.decrementStock(product.getId(), request.quantity());
            return orderRepository.save(order);
        });
        log.info("order saved orderId={} total={}", saved.getId(), total);
        return new OrderResponse(saved.getId(), saved.getStatus(), total);
    }

    private Order buildOrder(Customer customer, Product product, OrderRequest request, BigDecimal total) {
        Order order = new Order(customer.getId(), total);
        order.addItem(product.getId(), request.quantity(), product.getPrice());
        order.setGiftMessage(request.giftMessage());
        return order;
    }

    @Transactional(readOnly = true)
    public OrderDetail getOrder(Long id) {
        Order order = orderRepository.findById(id).orElseThrow(() -> new NotFoundException("order", id));
        return OrderDetail.of(order);
    }
}
