import axios from "axios";

const ORDERS = "/api/orders";

export const api = axios.create({ baseURL: "/" });

export function getOrder(id: number) {
  return api.get(`${ORDERS}/${id}`);
}

export async function createOrder(order: unknown) {
  const res = await fetch("/api/orders", { method: "POST", body: JSON.stringify(order) });
  return res.json();
}

export const searchOrders = (q: string) => axios.get("/api/orders/search?q=" + q);
