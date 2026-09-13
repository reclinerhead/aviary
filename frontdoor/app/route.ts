import { landingPage } from "@/lib/pages";

export const dynamic = "force-static";

export async function GET() {
  return new Response(landingPage(), {
    status: 200,
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}
