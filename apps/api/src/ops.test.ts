import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { LOG_SERVICES, portOpen, processRunning, readLog, safeSettings, serviceStatus } from "./ops.js";

const withDir = <T>(run: (dir: string) => T): T => {
  const dir = mkdtempSync(join(tmpdir(), "ops-"));
  try { return run(dir); } finally { rmSync(dir, { recursive: true, force: true }); }
};

test("readLog returns the last lines of a known service's log, in order", () => withDir((dir) => {
  writeFileSync(join(dir, "api.log"), ["one", "two", "three", "four", "five"].join("\n") + "\n");
  assert.deepEqual(readLog(dir, "api", 3), { service: "api", missing: false, lines: ["three", "four", "five"] });
  assert.deepEqual(readLog(dir, "api", 99)!.lines, ["one", "two", "three", "four", "five"]);
}));

test("readLog clamps the line count and refuses names that are not a known service", () => withDir((dir) => {
  writeFileSync(join(dir, "api.log"), "x\n");
  assert.equal(readLog(dir, "../../etc/passwd", 10), undefined);
  assert.equal(readLog(dir, "api.log", 10), undefined);
  assert.equal(readLog(dir, "", 10), undefined);
  assert.equal(readLog(dir, "api", 0)!.lines.length, 1, "at least one line is asked for");
  assert.equal(readLog(dir, "api", 100000)!.lines.length, 1, "and at most 500");
  for (const name of LOG_SERVICES) assert.notEqual(readLog(dir, name, 1), undefined, name);
}));

test("a missing log is reported as missing, not as an error", () => withDir((dir) => {
  assert.deepEqual(readLog(dir, "bot", 10), { service: "bot", missing: true, lines: [] });
}));

test("only the end of a big log is read, and a line cut in half by that is dropped", () => withDir((dir) => {
  const lines = Array.from({ length: 20000 }, (_, index) => `line ${index} ${"x".repeat(40)}`);
  writeFileSync(join(dir, "api.log"), lines.join("\n") + "\n");
  const tail = readLog(dir, "api", 3, 4096)!.lines;
  assert.deepEqual(tail, lines.slice(-3));
  const wide = readLog(dir, "api", 500, 4096)!.lines;
  assert.ok(wide.length < 500 && wide.length > 10, "limited by the bytes read");
  assert.ok(wide.every((line) => /^line \d+ x{40}$/.test(line)), "no half line at the cut");
}));

test("terminal colour codes are removed from log lines", () => withDir((dir) => {
  writeFileSync(join(dir, "hts.log"), "\u001b[32m➜\u001b[0m  Local: http://127.0.0.1:4311/\n\u001b[2K\r\u001b[1mready\u001b[22m\n");
  assert.deepEqual(readLog(dir, "hts", 5)!.lines, ["➜  Local: http://127.0.0.1:4311/", "ready"]);
}));

test("portOpen tells a listening port from a closed one", async () => {
  const server = createServer();
  await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
  const port = (server.address() as { port: number }).port;
  assert.equal(await portOpen(port), true);
  await new Promise<void>((done) => server.close(() => done()));
  assert.equal(await portOpen(port), false);
});

test("serviceStatus lists every service with its port and whether it answers", async () => {
  const rows = await serviceStatus({ api: 4312, data: 4313, hts: 4311 }, async (port) => port !== 4313);
  assert.deepEqual(rows, [
    { name: "api", port: 4312, open: true }, { name: "data", port: 4313, open: false }, { name: "hts", port: 4311, open: true },
  ]);
});

test("safeSettings shows chosen settings and never a secret", () => {
  const shown = safeSettings({ INAZINA_PYTHON_WORKERS: "2", INAZINA_MAX_HTTP_THREADS: "16", INAZINA_FRONTEND: "static", INAZINA_DOMAIN: "inazina.localhost",
    INAZINA_AUTH_SECRET: "s3cret", DISCORD_CLIENT_SECRET: "d1sc0rd", INAZINA_TOKEN: "bot-token", DATABASE_URL: "postgres://u:p@h/db", PATH: "/usr/bin" });
  assert.deepEqual(shown, { INAZINA_PYTHON_WORKERS: "2", INAZINA_MAX_HTTP_THREADS: "16", INAZINA_FRONTEND: "static", INAZINA_DOMAIN: "inazina.localhost" });
  assert.ok(!JSON.stringify(shown).includes("s3cret") && !JSON.stringify(shown).includes("bot-token"));
  assert.equal(safeSettings({}).INAZINA_PYTHON_WORKERS, "1", "the default is shown when nothing is set");
});

test("processRunning finds a running process by its command line, and only while it runs", async () => {
  const marker = `ops-test-marker-${process.pid}-${Date.now()}`;
  const child = spawn(process.execPath, ["-e", "setTimeout(() => {}, 30000)", marker], { stdio: "ignore" });
  try {
    await new Promise((done) => setTimeout(done, 300));
    assert.equal(await processRunning(marker), true);
    assert.equal(await processRunning("/no/such/program-" + Date.now()), false);
    assert.equal(await processRunning(marker + ")(["), false, "regular expression characters in the pattern are taken literally");
  } finally { child.kill(); }
  await new Promise((done) => child.once("exit", done));
  assert.equal(await processRunning(marker), false, "gone once it has exited");
});
