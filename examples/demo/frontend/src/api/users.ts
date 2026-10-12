import axios from "axios";

export async function getUser(id: number) {
  const { data } = await axios.get(`/users/${id}`);
  // `name` is unchanged; `email` was removed from the API -> real break.
  // `age` and `address.zip` were also removed, but nothing here reads them.
  return { name: data.name, email: data.email };
}
