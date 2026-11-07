package com.example.shop.customer;

import jakarta.validation.constraints.Email;
import jakarta.validation.constraints.NotBlank;

public final class CustomerDtos {

    private CustomerDtos() {
    }

    public record SignupRequest(@NotBlank String name, @NotBlank @Email String email, String phone) {
    }

    public record SignupResponse(String status, String message, Long customerId) {
    }

    public record AddressRequest(@NotBlank String street, @NotBlank String city) {
    }

    public record CustomerResponse(Long id, String name, String email, String phone, String street, String city) {

        static CustomerResponse of(Customer c) {
            return new CustomerResponse(c.getId(), c.getName(), c.getEmail(), c.getPhone(), c.getStreet(), c.getCity());
        }
    }
}
