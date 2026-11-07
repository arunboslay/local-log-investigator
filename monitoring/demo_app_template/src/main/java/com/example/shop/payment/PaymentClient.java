package com.example.shop.payment;

import java.math.BigDecimal;
import java.net.http.HttpClient;
import java.time.Duration;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

/** Calls the outside payment service to authorize a card charge. */
@Component
public class PaymentClient {

    private static final Logger log = LoggerFactory.getLogger(PaymentClient.class);

    private final RestClient restClient;
    private final long timeoutMs;

    public PaymentClient(@Value("${payment.base-url}") String baseUrl, @Value("${payment.timeout-ms}") long timeoutMs) {
        HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofMillis(500)).build();
        JdkClientHttpRequestFactory factory = new JdkClientHttpRequestFactory(http);
        factory.setReadTimeout(Duration.ofMillis(timeoutMs));
        this.restClient = RestClient.builder().baseUrl(baseUrl).requestFactory(factory).build();
        this.timeoutMs = timeoutMs;
    }

    public String charge(Long customerId, String cardNumber, BigDecimal amount) {
        long start = System.nanoTime();
        try {
            Map<?, ?> result = restClient.post()
                    .uri("/charge")
                    .body(Map.of("customerId", customerId, "card", cardNumber, "amount", amount))
                    .retrieve()
                    .body(Map.class);
            return String.valueOf(result.get("authId"));
        } catch (RestClientException e) {
            long elapsedMs = (System.nanoTime() - start) / 1_000_000;
            log.warn("payment call failed customerId={} card={} elapsedMs={} timeoutMs={}",
                    customerId, cardNumber, elapsedMs, timeoutMs);
            throw new PaymentException("charge failed after " + elapsedMs + " ms: " + e.getMessage(), e);
        }
    }
}
