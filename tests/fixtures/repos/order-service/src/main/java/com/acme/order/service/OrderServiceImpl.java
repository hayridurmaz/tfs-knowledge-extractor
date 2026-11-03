package com.acme.order.service;

import com.acme.order.domain.*;
import com.acme.order.client.CustomerClient;
import com.acme.common.Money;
import lombok.RequiredArgsConstructor;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Service;
import java.util.List;
import static com.acme.common.Money.zero;

@Service
@RequiredArgsConstructor
public class OrderServiceImpl implements OrderService {
    private static final String TOPIC = "order-created";
    private final OrderRepository orderRepository;
    private final CustomerClient customerClient;
    private final KafkaTemplate<String, Order> kafkaTemplate;

    @Value("${app.payment.timeout-ms}")
    private int paymentTimeout;

    @Override
    public Order find(Long id) {
        return orderRepository.findById(id).orElseThrow();
    }

    @Override
    public Order place(Order order) {
        validate(order);
        if (!customerClient.exists(order.getCustomerId())) {
            throw new IllegalArgumentException("Customer not found");
        }
        Money total = zero();
        Order saved = orderRepository.save(order);
        kafkaTemplate.send(TOPIC, saved);
        return saved;
    }

    @Override
    public List<Order> search(String q) {
        return orderRepository.findByNameContaining(q);
    }

    private void validate(Order order) {
        if (order == null) throw new IllegalArgumentException("order");
    }

    @KafkaListener(topics = "payment-completed")
    public void onPayment(String msg) {
        this.validate(null);
    }
}
