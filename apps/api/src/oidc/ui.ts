// The built login screen (apps/id/dist): its HTML shell for an interaction, and its files under /oidc/ui/.
import { existsSync, readFileSync, statSync } from "node:fs";
import { extname, join, resolve, sep } from "node:path";
import type { ServerResponse } from "node:http";

const types: Record<string, string> = {
  ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
  ".ico": "image/x-icon", ".json": "application/json; charset=utf-8", ".woff2": "font/woff2", ".woff": "font/woff", ".map": "application/json",
};
export const uiPrefix = "/oidc/ui/";

export type UiFiles = { shell: () => string | undefined; serve: (response: ServerResponse, pathname: string) => boolean };

export function createUiFiles(dir: string | undefined): UiFiles {
  const root = dir ? resolve(dir) : undefined;
  return {
    // The login screen's HTML, or nothing when it has not been built (the server-rendered login page is used then).
    shell: () => (root && existsSync(join(root, "index.html")) ? readFileSync(join(root, "index.html"), "utf8") : undefined),
    serve(response, pathname) {
      if (!root || !pathname.startsWith(uiPrefix)) return false;
      let relative: string;
      try { relative = decodeURIComponent(pathname.slice(uiPrefix.length)); } catch { return notFound(response); }
      if (!relative || relative === "index.html" || /[\\\u0000]/.test(relative) || relative.split("/").some((part) => part === ".." || part === "")) return notFound(response);
      const file = resolve(root, relative);
      if (!file.startsWith(root + sep) || !existsSync(file) || !statSync(file).isFile()) return notFound(response);
      const body = readFileSync(file);
      response.writeHead(200, {
        "content-type": types[extname(file).toLowerCase()] ?? "application/octet-stream", "content-length": body.length,
        "cache-control": relative.startsWith("assets/") ? "public, max-age=31536000, immutable" : "no-cache", "x-content-type-options": "nosniff",
      });
      response.end(body);
      return true;
    },
  };
}

function notFound(response: ServerResponse): true {
  response.writeHead(404, { "content-length": "0", "cache-control": "no-store" });
  response.end();
  return true;
}
