import React from "react";
import { useParams } from "react-router-dom";
import OrderCard from "../components/OrderCard";
import { useOrder } from "@/hooks/useOrder";
import { createOrder } from "@/api";

export default function OrderPage() {
  const { id } = useParams();
  const order = useOrder(Number(id));
  const onSave = () => createOrder(order);
  return (
    <div>
      <OrderCard order={order} />
      <button onClick={onSave}>Save</button>
    </div>
  );
}
