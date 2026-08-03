import { defineConfig } from "vitest/config"

export default defineConfig({
  // `tsconfig.json` sets `jsx: "preserve"` because Next.js does its own transform.
  // Vitest has no Next.js pipeline, so it needs the automatic runtime stated here or
  // every component test fails with "React is not defined".
  esbuild: { jsx: "automatic" },
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.ts", "tests/**/*.test.tsx"],
  },
  resolve: {
    alias: { "@": new URL("./src", import.meta.url).pathname },
  },
})
