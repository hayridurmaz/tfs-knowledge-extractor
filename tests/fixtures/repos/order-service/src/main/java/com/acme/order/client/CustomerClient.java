package com.acme.order.client;

import org.springframework.cloud.openfeign.FeignClient;
import org.springframework.web.bind.annotation.*;

@FeignClient(name = "customer-service", path = "/customers")
public interface CustomerClient {
    @GetMapping("/{id}/exists")
    boolean exists(@PathVariable("id") Long id);
}
