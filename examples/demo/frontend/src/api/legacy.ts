export function removeLegacy(id: number) {
  return fetch(`/legacy/${id}`, { method: "DELETE" });
}
