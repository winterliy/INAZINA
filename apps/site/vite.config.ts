import { defineConfig } from "vite";

// NACURI official site. Forms post to the API server, which stores them for the dashboard.
export default defineConfig({
  server: {
    // Behind Caddy the Host header is the public name (naru.inazina.kr ...): Vite refuses names it was not told about.
    allowedHosts: [`.${process.env.INAZINA_DOMAIN || "inazina.localhost"}`],
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
