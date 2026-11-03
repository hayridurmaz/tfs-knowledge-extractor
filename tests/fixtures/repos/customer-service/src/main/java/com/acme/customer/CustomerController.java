package com.acme.customer;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping(path = "/customers")
public class CustomerController {
    private final CustomerRepository repo;

    CustomerController(CustomerRepository repo) { this.repo = repo; }

    @GetMapping("/{customerId}/exists")
    public boolean exists(@PathVariable Long customerId) {
        return repo.existsById(customerId);
    }

    @GetMapping(value = "/{customerId:\d+}")
    public Customer get(@PathVariable Long customerId) {
        return repo.findById(customerId).orElse(null);
    }
}
