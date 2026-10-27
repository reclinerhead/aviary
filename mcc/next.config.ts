import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactCompiler: true,
  // Let LAN devices (e.g. a phone) reach the dev server; Next blocks
  // cross-origin dev requests by default. Hostnames only, no protocol/port.
  // Whole subnet so it survives DHCP reshuffles of the dev machine's IP.
  allowedDevOrigins: ["192.168.1.*"],
};

export default nextConfig;
