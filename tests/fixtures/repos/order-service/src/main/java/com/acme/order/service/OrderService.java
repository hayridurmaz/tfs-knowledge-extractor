package com.acme.order.service;

import com.acme.order.domain.Order;
import java.util.List;

public interface OrderService {
    Order find(Long id);
    Order place(Order order);
    List<Order> search(String q);
}
