import { execFile } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";

// Wide-Learning viewer. It only READS docs/wide-learning (and runs the read-only
// checker); it never writes, so the two threads stay the single source of truth.
const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const docs = join(root, "docs/wide-learning");
const FILES = [
  "README.md", "RULES.md", "WLMD.md", "SOURCES.md", "REVIEW.md", "topics.md", "wide-learning.md", "apply-queue.md",
  "threads/cthread-claude.md", "threads/gthread-codex.md",
  "registry/user.md", "registry/cthread-claude.md", "registry/gthread-codex.md",
];
const python = existsSync(join(root, ".venv/bin/python")) ? join(root, ".venv/bin/python") : "python3";

function run(args: string[]): Promise<string> {
  return new Promise((done) => {
    execFile(python, ["-I", join(root, "tools/wlmd_check.py"), ...args], { cwd: root, timeout: 15_000 },
      (_error, stdout, stderr) => done(`${stdout}${stderr}`.trim()));
  });
}

function wideLearningData(): Plugin {
  return {
    name: "wide-learning-data",
    configureServer(server) {
      // Push a change notice the moment any watched file is saved (the page re-reads at once).
      server.watcher.add([docs, join(root, "tools/wlmd_check.py")]);
      const notify = (path: string) => {
        if (path.startsWith(docs) || path.endsWith("wlmd_check.py")) server.ws.send({ type: "custom", event: "wl:changed", data: { path } });
      };
      server.watcher.on("change", notify);
      server.watcher.on("add", notify);
      server.watcher.on("unlink", notify);
      server.middlewares.use("/wl-api/files", (_request, response) => {
        const files = Object.fromEntries(FILES.map((name) => {
          const path = join(docs, name);
          return [name, existsSync(path) ? readFileSync(path, "utf8") : ""];
        }));
        response.setHeader("content-type", "application/json; charset=utf-8");
        response.setHeader("cache-control", "no-store");
        response.end(JSON.stringify({ files, readAt: Date.now() }));
      });
      server.middlewares.use("/wl-api/check", (_request, response) => {
        void Promise.all([run(["--todo", "CLAUDE"]), run(["--todo", "CODEX"])]).then(([claude, codex]) => {
          response.setHeader("content-type", "application/json; charset=utf-8");
          response.setHeader("cache-control", "no-store");
          response.end(JSON.stringify({ claude, codex }));
        });
      });
    },
  };
}

export default defineConfig({
  plugins: [wideLearningData()],
  server: { host: "127.0.0.1", port: 4315, strictPort: true },
  // The entry screen imports Three.js lazily. Bundle it when the server starts, so the
  // first visit does not race a dependency re-optimisation (a failed dynamic import).
  optimizeDeps: {
    include: ["three", "three/addons/effects/AsciiEffect.js", "three/addons/controls/OrbitControls.js",
      "three/addons/loaders/GLTFLoader.js", "three/addons/environments/RoomEnvironment.js"],
  },
});
