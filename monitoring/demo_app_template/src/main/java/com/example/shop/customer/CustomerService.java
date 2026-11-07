package com.example.shop.customer;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataAccessException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import com.example.shop.customer.CustomerDtos.AddressRequest;
import com.example.shop.customer.CustomerDtos.CustomerResponse;
import com.example.shop.customer.CustomerDtos.SignupRequest;
import com.example.shop.customer.CustomerDtos.SignupResponse;
import com.example.shop.web.NotFoundException;

@Service
public class CustomerService {

    private static final Logger log = LoggerFactory.getLogger(CustomerService.class);

    private final CustomerRepository customerRepository;

    public CustomerService(CustomerRepository customerRepository) {
        this.customerRepository = customerRepository;
    }

    public SignupResponse signup(SignupRequest request) {
        log.info("signup validated name={} email={} phone={}", request.name(), request.email(), request.phone());
        Customer customer = new Customer(request.name(), request.email(), normalizePhone(request.phone()));
        try {
            customerRepository.saveAndFlush(customer);
            log.info("customer saved customerId={}", customer.getId());
        } catch (DataAccessException e) {
            // Best effort: a storage hiccup should not block the signup page.
            log.warn("could not save customer email={}: {}", request.email(), e.getMostSpecificCause().getMessage());
        }
        log.info("signup completed email={}", request.email());
        return new SignupResponse("SUCCESS", "Signup complete", customer.getId());
    }

    private String normalizePhone(String phone) {
        return phone == null ? null : phone.trim();
    }

    @Transactional
    public CustomerResponse updateAddress(Long id, AddressRequest request) {
        Customer customer = customerRepository.findById(id).orElseThrow(() -> new NotFoundException("customer", id));
        customer.setAddress(request.street(), request.city());
        log.info("address updated customerId={} street={} city={}", id, request.street(), request.city());
        return CustomerResponse.of(customer);
    }

    @Transactional(readOnly = true)
    public CustomerResponse getCustomer(Long id) {
        Customer customer = customerRepository.findById(id).orElseThrow(() -> new NotFoundException("customer", id));
        return CustomerResponse.of(customer);
    }
}
