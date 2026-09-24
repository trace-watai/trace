import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
  // tsconfig keeps JSX for Next to compile, so tests that render a component
  // need the transform to produce React's automatic runtime calls instead.
  oxc: { jsx: { runtime: "automatic" } },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    environment: "node",
  },
});
