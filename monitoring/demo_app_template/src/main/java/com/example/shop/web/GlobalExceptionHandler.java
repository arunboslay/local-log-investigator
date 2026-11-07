package com.example.shop.web;

import java.time.Instant;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.servlet.resource.NoResourceFoundException;

import com.example.shop.payment.PaymentException;

@RestControllerAdvice
public class GlobalExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);

    @ExceptionHandler(NotFoundException.class)
    public ResponseEntity<Map<String, Object>> notFound(NotFoundException e) {
        log.warn("not found: {}", e.getMessage());
        return body(HttpStatus.NOT_FOUND, e.getMessage());
    }

    @ExceptionHandler(NoResourceFoundException.class)
    public ResponseEntity<Map<String, Object>> noRoute(NoResourceFoundException e) {
        log.warn("no route: {}", e.getMessage());
        return body(HttpStatus.NOT_FOUND, "not found");
    }

    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<Map<String, Object>> invalid(MethodArgumentNotValidException e) {
        log.warn("validation failed: {}", e.getBindingResult().getFieldErrors());
        return body(HttpStatus.BAD_REQUEST, "invalid request");
    }

    @ExceptionHandler(PaymentException.class)
    public ResponseEntity<Map<String, Object>> payment(PaymentException e) {
        log.error("payment failed: {}", e.getMessage(), e);
        return body(HttpStatus.BAD_GATEWAY, "payment service unavailable");
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<Map<String, Object>> unexpected(Exception e) {
        log.error("request failed: {}", e.toString(), e);
        return body(HttpStatus.INTERNAL_SERVER_ERROR, "internal server error");
    }

    private ResponseEntity<Map<String, Object>> body(HttpStatus status, String message) {
        return ResponseEntity.status(status).body(Map.of(
                "status", status.value(),
                "error", message,
                "requestId", String.valueOf(MDC.get("requestId")),
                "timestamp", Instant.now().toString()));
    }
}
