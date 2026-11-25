import { defineConfig, type Plugin } from "vite";

// INAZINA official site. On the internal network, the Contact/Report forms post to the API server, which stores them for the dashboard.
// The public build (GitHub Pages) is display only: VITE_DISPLAY_ONLY=1 removes everything between <!--display:hide--> markers
// (the forms and every mention of them) and keeps what is between <!--display:show--> markers; VITE_BASE is the sub-path
// (e.g. /INAZINA/) and VITE_SITE_URL the public address used for the link-preview image.
const displayOnly = process.env.VITE_DISPLAY_ONLY === "1";

function displayVariant(): Plugin {
  return {
    name: "display-variant",
    transformIndexHtml: {
      order: "pre",
      handler(html) {
        const hide = /<!--display:hide-->[\s\S]*?<!--\/display:hide-->/g;
        const show = /<!--display:show-->[\s\S]*?<!--\/display:show-->/g;
        let out = displayOnly ? html.replace(hide, "").replace(/<!--\/?display:show-->/g, "") : html.replace(show, "").replace(/<!--\/?display:hide-->/g, "");
        if (process.env.VITE_SITE_URL) out = out.replaceAll("https://www.inazina.localhost/", process.env.VITE_SITE_URL);
        return out;
      },
    },
  };
}

export default defineConfig({
  base: process.env.VITE_BASE || "/",
  plugins: [displayVariant()],
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
