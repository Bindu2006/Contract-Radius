export async function loadProducts() {
  const res = await fetch("/products");
  const body = await res.json();
  // `items[].price` was removed, but this page only reads `name` -> not reported.
  return body.items.map((p: { name: string }) => p.name);
}
