package com.acme.order.api;

import com.acme.order.service.OrderService;
import com.acme.order.domain.Order;
import org.springframework.web.bind.annotation.*;
import java.util.List;

/**
 * REST API for orders.
 */
@RestController
@RequestMapping("/api/orders")
public class OrderController {
    private final OrderService orderService;

    public OrderController(OrderService orderService) {
        this.orderService = orderService;
    }

    @GetMapping("/{id}")
    public Order get(@PathVariable Long id) {
        return orderService.find(id);
    }

    @PostMapping
    public Order create(@RequestBody Order order) {
        Order saved = orderService.place(order);
        return saved;
    }

    @RequestMapping(value = {"/search", "/find"}, method = RequestMethod.GET)
    public List<Order> search(@RequestParam String q) {
        return orderService.search(q);
    }
}
