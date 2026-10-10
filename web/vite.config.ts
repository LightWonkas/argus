/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Argus dashboard frontend — dev server on port 3000.
// /api requests are proxied to the backend (default 127.0.0.1:8787) → the browser sees a
// single origin, so CORS/cookies just work. Override the backend with VITE_API_TARGET.
const apiTarget = process.env.VITE_API_TARGET ?? "http://127.0.0.1:8787";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          const normalized = id.replaceAll("\\", "/");
          if (id.includes("lightweight-charts")) return "charts-vendor";
          if (id.includes("react-grid-layout") || id.includes("react-draggable")) {
            return "layout-vendor";
          }
          if (/\/node_modules\/(react|react-dom|scheduler)\//.test(normalized)) {
            return "react-vendor";
          }
          return undefined;
        },
      },
    },
  },
  server: {
    port: 3000,
    strictPort: true,
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
  // Vitest — network-free smoke tests (pure helpers + component render). jsdom DOM environment.
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
