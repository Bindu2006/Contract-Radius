import { apiClient } from "./client";

export async function listTotals() {
  const res = await apiClient.get("/orders");
  // `total` changed from number to string -> real break.
  return res.data.map((order) => order.total);
}

export async function createOrder(productId: number) {
  // The API now requires `currency` and this call doesn't send it -> real break.
  return apiClient.post("/orders", { product_id: productId, quantity: 1 });
}
