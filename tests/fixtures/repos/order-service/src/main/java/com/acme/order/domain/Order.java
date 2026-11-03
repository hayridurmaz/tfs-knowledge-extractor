package com.acme.order.domain;

import jakarta.persistence.*;

@Entity
@Table(name = "orders")
public class Order {
    @Id
    private Long id;
    private Long customerId;
    private String name;

    public Long getCustomerId() { return customerId; }
}
