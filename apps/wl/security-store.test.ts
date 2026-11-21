// Run: node --import tsx --test apps/wl/security-store.test.ts
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { hashSecret, wlSecurity } from "./security-store.ts";

const fresh = () => { const dir = mkdtempSync(join(tmpdir(), "wl-sec-")); return { dir, store: wlSecurity(dir, join(dir, "wl-security.json")) }; };

test("a fresh store has no fixed defaults: random secrets are made once and kept hashed", () => {
  const { dir, store } = fresh();
  const logs: string[] = [];
  const original = console.warn;
  console.warn = (message: string) => { logs.push(String(message)); };
  try {
    assert.equal(store.verifyLogCode("0154"), false, "the old built-in code is gone");
    assert.equal(store.verifyEntry("definitely-wrong"), false);
    assert.equal(logs.length, 2);
    const code = /: (\d{6})$/.exec(logs.find((line) => line.includes("로그"))!)![1];
    assert.equal(store.verifyLogCode(code), true);
    assert.equal(store.verifyLogCode(`99${code}`), true, "the code ends the typed digits");
    const password = /: (\S+)$/.exec(logs.find((line) => line.includes("입장"))!)![1];
    assert.equal(store.verifyEntry(password), true);
    assert.equal(logs.length, 2, "announced once");
    assert.doesNotMatch(readFileSync(join(dir, "wl-security.json"), "utf8"), new RegExp(`${password}|${code}`));
    assert.deepEqual(store.state(), { entryCustom: false, codeCustom: false, codeLength: 6 });
  } finally { console.warn = original; }
});

test("a tampered iteration count in the store file is refused, not run", () => {
  const { dir } = fresh();
  const path = join(dir, "wl-security.json");
  const salt = "ab".repeat(16);
  writeFileSync(path, JSON.stringify({ entry: { hash: `pbkdf2$99999999999$${salt}$${"00".repeat(32)}` }, logCode: { hash: `pbkdf2$5$${salt}$00`, length: 4 } }));
  const store = wlSecurity(dir, path);
  const started = Date.now();
  assert.equal(store.verifyEntry("anything-at-all"), false);
  assert.equal(store.verifyLogCode("1234"), false);
  assert.ok(Date.now() - started < 1000);
});

test("a refused change writes nothing", () => {
  const { store } = fresh();
  assert.throws(() => store.change("not-the-current", "long-enough-pass"), /현재 입장 비밀번호/);
  assert.equal(store.verifyEntry("long-enough-pass"), false);
});

test("the entry password and the code change separately, need the current password and are stored salted", () => {
  const { dir } = fresh();
  const path = join(dir, "wl-security.json");
  writeFileSync(path, JSON.stringify({ entry: { hash: hashSecret("first-password") } }));
  const store = wlSecurity(dir, path);
  assert.equal(store.verifyEntry("first-password"), true);
  assert.throws(() => store.change("first-password", "short"), /8~64/);
  assert.throws(() => store.change("first-password", "first-password"), /다른/);
  assert.throws(() => store.change("first-password", undefined, "12"), /숫자/);
  assert.throws(() => store.change("first-password"), /바꿀 값/);
  store.change("first-password", "second-password", "98765");
  assert.equal(store.verifyEntry("second-password"), true);
  assert.equal(store.verifyEntry("first-password"), false);
  assert.equal(store.verifyLogCode("98765"), true);
  assert.equal(store.verifyLogCode("0154"), false, "the old code stops working");
  store.change("second-password", undefined, "2468");  // the code alone
  assert.equal(store.verifyEntry("second-password"), true);
  assert.equal(store.verifyLogCode("2468"), true);
  const text = readFileSync(path, "utf8");
  assert.match(text, /pbkdf2\$/);
  assert.doesNotMatch(text, /second-password|98765|2468/);
  assert.equal((statSync(path).mode & 0o777).toString(8), "600");
});
