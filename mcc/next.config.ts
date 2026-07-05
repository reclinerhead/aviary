import type { NextConfig } from "next";

// The MCC never talks to the daemon cross-origin: /daemon/* is proxied
// server-side to the Merle daemon (FastAPI, default localhost:8000). That keeps
// the browser same-origin (no CORS work in the daemon) and means a phone on the
// LAN hitting this dev server still reaches the daemon through it.
const DAEMON = process.env.MERLE_DAEMON_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  reactCompiler: true,
  async rewrites() {
    return [
      {
        source: "/daemon/:path*",
        destination: `${DAEMON}/:path*`,
      },
    ];
  },
};

export default nextConfig;
