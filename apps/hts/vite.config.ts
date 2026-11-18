import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: Number(process.env.INAZINA_HTS_PORT ?? 4311),
    strictPort: true,
    proxy: {
      // xfwd passes the browser's address on (X-Forwarded-For) for the board's IP records.
      "/api": { target: `http://127.0.0.1:${process.env.PORT ?? 4312}`, xfwd: true },
    },
  },
});
