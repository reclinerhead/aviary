import { redirect } from "next/navigation";

// The MCC dashboard lived here until #628 retired it. The Aviary is the whole
// app now, so the root sends every visitor there: a server-side 307, which is
// what a stale home bookmark wants. On a station Caddy owns `/` (the porch,
// #696: the launcher for the Aviary, the Quarry and the Conservatory) and
// this never answers there; orchid has no porch and still needs it.
export default function Home() {
  redirect("/aviary");
}
