import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // Behind Caddy the Host header is the public name (naru.inazina.kr ...): Vite refuses names it was not told about.
    allowedHosts: [`.${process.env.INAZINA_DOMAIN || "inazina.localhost"}`],
    host: "127.0.0.1",
    port: Number(process.env.INAZINA_DASHBOARD_PORT ?? 4310),
    strictPort: true,
    proxy: {
      "/api": `http://127.0.0.1:${process.env.PORT ?? 4312}`,
    },
  },
});
