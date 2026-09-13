import type { NextConfig } from "next";

// The station front door (issue #545): route handlers only, no pages, no
// React tree. Deployed as its own Vercel project with Root Directory set to
// this folder -- the repo's one Vercel surface (frontdoor/README.md).
const nextConfig: NextConfig = {};

export default nextConfig;
