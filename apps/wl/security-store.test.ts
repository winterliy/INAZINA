// Run: node --import tsx --test apps/wl/security-store.test.ts
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { hashSecret, wlSecurity } from "./security-store.ts";

const fresh = () => { const dir = mkdtempSync(join(tmpdir(), "wl-sec-")); return { dir, store: wlSecurity(dir, join(dir, "wl-security.json")) }; };

test("before any change the built-in log code works and a wrong password does not", () => {
  const { store } = fresh();
  assert.equal(store.verifyLogCode("0154"), true);
  assert.equal(store.verifyLogCode("12340154"), true, "the code ends the typed digits");
  assert.equal(store.verifyLogCode("0155"), false);
  assert.equal(store.verifyEntry("definitely-wrong"), false);
  assert.deepEqual(store.state(), { entryCustom: false, codeCustom: false, codeLength: 4 });
});

test("a refused change writes nothing", () => {
  const { dir, store } = fresh();
  assert.throws(() => store.change("not-the-current", "long-enough-pass"), /현재 입장 비밀번호/);
  assert.throws(() => statSync(join(dir, "wl-security.json")));
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
