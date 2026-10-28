import { redirect } from "next/navigation";

// The MCC dashboard lived here until #628 retired it. The Aviary is the whole
// app now, so the root sends every visitor there: a server-side 307, which is
// what a stale home bookmark wants, and what a station's `/` will want once
// its porch retires (epic #627 Phase 5 -- until then Caddy owns `/` there and
// this never answers).
export default function Home() {
  redirect("/aviary");
}
