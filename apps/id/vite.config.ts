import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The login server (apps/api, the identity provider) serves this build: the page at /oidc/interaction/<uid> and its files under /oidc/ui/.
export default defineConfig({
  base: "/oidc/ui/",
  plugins: [react()],
  publicDir: "../dashboard/public", // the same icons as the dashboard
});
