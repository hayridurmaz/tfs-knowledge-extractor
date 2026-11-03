import { useEffect, useState } from "react";
import { getOrder } from "../api";

export function useOrder(id: number) {
  const [order, setOrder] = useState(null);
  useEffect(() => { getOrder(id).then(r => setOrder(r.data)); }, [id]);
  return order;
}
