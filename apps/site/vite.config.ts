import { defineConfig } from "vite";

// NACURI official site. Forms post to the API server, which stores them for the dashboard.
export default defineConfig({
  server: {
    host: "127.0.0.1",
    port: 4314,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:4312",
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 4314,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:4312",
    },
  },
});
