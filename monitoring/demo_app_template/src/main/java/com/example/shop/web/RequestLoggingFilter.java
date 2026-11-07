package com.example.shop.web;

import java.io.IOException;
import java.util.UUID;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * Gives every request an id and logs one "request completed" line with status, latency and
 * the number of SQL statements. The id and request details go into the MDC, so every log line
 * written while handling the request carries them.
 */
@Component
@Order(Ordered.HIGHEST_PRECEDENCE)
public class RequestLoggingFilter extends OncePerRequestFilter {

    private static final Logger log = LoggerFactory.getLogger(RequestLoggingFilter.class);

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        String requestId = UUID.randomUUID().toString().substring(0, 12);
        MDC.put("requestId", requestId);
        MDC.put("httpMethod", request.getMethod());
        MDC.put("path", request.getRequestURI());
        response.setHeader("X-Request-Id", requestId);
        QueryCounter.reset();
        long start = System.nanoTime();
        try {
            chain.doFilter(request, response);
        } finally {
            if (!request.getRequestURI().startsWith("/actuator")) {
                int status = response.getStatus();
                MDC.put("status", String.valueOf(status));
                MDC.put("latencyMs", String.valueOf((System.nanoTime() - start) / 1_000_000));
                MDC.put("dbQueries", String.valueOf(QueryCounter.get()));
                if (status >= 500) {
                    log.error("request completed");
                } else if (status >= 400) {
                    log.warn("request completed");
                } else {
                    log.info("request completed");
                }
            }
            MDC.clear();
        }
    }
}
