import type { NextConfig } from "next";

// The MCC never talks to the daemon cross-origin: /daemon/* is proxied
// server-side to the Merle daemon (FastAPI, default 127.0.0.1:8000). That keeps
// the browser same-origin (no CORS work in the daemon) and means a phone on the
// LAN hitting this dev server still reaches the daemon through it.
//
// 127.0.0.1, not "localhost": uvicorn binds IPv4 only, but on Windows Node
// resolves "localhost" to IPv6 (::1) first -- so a "localhost" target makes a
// wasted ::1 attempt (and a doubled ECONNREFUSED when the daemon is down) before
// falling back to IPv4. Targeting IPv4 directly avoids the detour.
const DAEMON = process.env.MERLE_DAEMON_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  reactCompiler: true,
  // Let LAN devices (e.g. a phone) reach the dev server; Next blocks
  // cross-origin dev requests by default. Hostnames only, no protocol/port.
  // Whole subnet so it survives DHCP reshuffles of the dev machine's IP.
  allowedDevOrigins: ["192.168.1.*"],
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
