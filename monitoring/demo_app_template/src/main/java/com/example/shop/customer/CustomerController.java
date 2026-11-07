package com.example.shop.customer;

import jakarta.validation.Valid;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.example.shop.customer.CustomerDtos.AddressRequest;
import com.example.shop.customer.CustomerDtos.CustomerResponse;
import com.example.shop.customer.CustomerDtos.SignupRequest;
import com.example.shop.customer.CustomerDtos.SignupResponse;

@RestController
@RequestMapping("/api/customers")
public class CustomerController {

    private final CustomerService customerService;

    public CustomerController(CustomerService customerService) {
        this.customerService = customerService;
    }

    @PostMapping
    public ResponseEntity<SignupResponse> signup(@Valid @RequestBody SignupRequest request) {
        return ResponseEntity.status(HttpStatus.CREATED).body(customerService.signup(request));
    }

    @PutMapping("/{id}/address")
    public CustomerResponse updateAddress(@PathVariable Long id, @Valid @RequestBody AddressRequest request) {
        return customerService.updateAddress(id, request);
    }

    @GetMapping("/{id}")
    public CustomerResponse getCustomer(@PathVariable Long id) {
        return customerService.getCustomer(id);
    }
}
