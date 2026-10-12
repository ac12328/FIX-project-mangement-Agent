import type { NextConfig } from "next";

const apiProxyTarget = (process.env.API_PROXY_TARGET ?? "http://127.0.0.1:8317").replace(/\/+$/, "");
const allowedDevOrigins = (process.env.DASHBOARD_ALLOWED_DEV_ORIGINS ?? "127.0.0.1")
  .split(",")
  .map((origin) => origin.trim())
  .filter(Boolean);

const nextConfig: NextConfig = {
  distDir: process.env.NEXT_DIST_DIR ?? ".next",
  allowedDevOrigins,
  async redirects() {
    return [
      { source: "/queries", destination: "/ask", permanent: false },
      { source: "/queries/:path*", destination: "/ask", permanent: false },
      { source: "/portfolio", destination: "/operations?tab=health", permanent: false },
      { source: "/timeline", destination: "/operations?tab=timeline", permanent: false },
      { source: "/inbox", destination: "/mail?tab=inbox", permanent: false },
      { source: "/drafts", destination: "/mail?tab=drafts", permanent: false },
      { source: "/compose", destination: "/mail?tab=compose", permanent: false },
      { source: "/clients", destination: "/counterparties?tab=clients", permanent: false },
      { source: "/vendors", destination: "/counterparties?tab=vendors", permanent: false },
    ];
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${apiProxyTarget}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
