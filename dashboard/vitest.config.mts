import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

const path = (relative: string) => fileURLToPath(new URL(relative, import.meta.url));

export default defineConfig({
  esbuild: { jsx: "automatic" },
  resolve: { alias: { "@": path("./src"), "server-only": path("./tests/stubs/server-only.ts") } },
  test: { environment: "node", include: ["tests/**/*.test.{ts,tsx}"] },
});
