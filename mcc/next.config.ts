import type { NextConfig } from "next";

// Daemon traffic (/daemon/*) is proxied by app/daemon/[...path]/route.ts, not
// a rewrite: the route owns the daemon-down failure path (quiet 503s,
// transition-only logging -- issue #35), which Next's built-in rewrite proxy
// can't do (it logs every failed upstream attempt, and with the MCC on pearl
// running 24/7 the daemon being down is the normal state, not an error).
const nextConfig: NextConfig = {
  reactCompiler: true,
  // Let LAN devices (e.g. a phone) reach the dev server; Next blocks
  // cross-origin dev requests by default. Hostnames only, no protocol/port.
  // Whole subnet so it survives DHCP reshuffles of the dev machine's IP.
  allowedDevOrigins: ["192.168.1.*"],
};

export default nextConfig;
