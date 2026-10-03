import type { NextConfig } from "next";

// One build worker and no worker threads: the machine is shared, and a build must not take it over.
const config: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  reactStrictMode: true,
  images: { unoptimized: true },
  // The only body this dashboard accepts is a short password form, and Next buffers a request body for
  // the proxy before the proxy looks at the request, so the default 10 MiB would let a few dozen
  // unauthenticated posts use up the container's memory.
  experimental: { cpus: 1, workerThreads: false, proxyClientMaxBodySize: "16kb" },
  // The proxy does not run on static files, so they get the headers that matter for a file here.
  async headers() {
    const headers = [
      { key: "X-Content-Type-Options", value: "nosniff" },
      { key: "Cross-Origin-Resource-Policy", value: "same-origin" },
    ];
    return ["/_next/static/:path*", "/brand/:path*", "/fonts/:path*"].map((source) => ({ source, headers }));
  },
};

export default config;
