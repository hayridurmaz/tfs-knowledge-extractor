import React from "react";

type Props = { order: any };

const OrderCard = ({ order }: Props) => <div className="card">{order?.name}</div>;

export default OrderCard;
