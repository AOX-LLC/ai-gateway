import type { NextConfig } from "next";

// One build worker and no worker threads: the machine is shared, and a build must not take it over.
const config: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  reactStrictMode: true,
  images: { unoptimized: true },
  experimental: { cpus: 1, workerThreads: false },
};

export default config;
