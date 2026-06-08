/// <reference types="node" />
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

// The FastAPI backend (gencall-server) listens on :8080 by default.
// In dev, proxy the API + websocket through Vite so the SPA can talk to it
// without CORS. In production, FastAPI serves the built assets from /static.
const BACKEND = process.env.GENCALL_BACKEND ?? "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react()],
  base: "/console/",
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: BACKEND, changeOrigin: true },
      "/ws": { target: BACKEND, ws: true, changeOrigin: true },
    },
  },
  build: {
    // Emit into the Python package so FastAPI can serve it as static files.
    outDir: fileURLToPath(new URL("../gencall/web/console", import.meta.url)),
    emptyOutDir: true,
    sourcemap: true,
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
