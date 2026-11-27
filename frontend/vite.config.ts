import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The UI is served under /admin by FastAPI in production (same origin as the API).
// In development, Vite proxies /api to the selected local QA backend so cookies stay same-origin.
const runtimeEnvironment = (globalThis as typeof globalThis & {
  process?: { env?: Record<string, string | undefined> };
}).process?.env;
const apiProxyTarget = runtimeEnvironment?.INFRASPINE_DEV_API_PROXY_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  base: "/admin/",
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: { "/api": apiProxyTarget },
  },
  build: { outDir: "dist", sourcemap: false },
});
