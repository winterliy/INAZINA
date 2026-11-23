import assert from "node:assert/strict";
import test from "node:test";
import { formatUptime, serviceLabel, serviceState } from "./opsFormat.ts";

test("uptime reads as the two biggest units in Korean", () => {
  assert.equal(formatUptime(5), "1분 미만");
  assert.equal(formatUptime(60), "1분");
  assert.equal(formatUptime(3 * 60 + 40), "3분");
  assert.equal(formatUptime(2 * 3600 + 14 * 60 + 9), "2시간 14분");
  assert.equal(formatUptime(3600), "1시간");
  assert.equal(formatUptime(86400 + 3600 * 5 + 120), "1일 5시간");
  assert.equal(formatUptime(-3), "1분 미만");
});

test("every service has a Korean name; numbered data workers keep their number", () => {
  assert.equal(serviceLabel("api"), "API 서버");
  assert.equal(serviceLabel("data"), "데이터 서비스");
  assert.equal(serviceLabel("data-2"), "데이터 서비스 2");
  assert.equal(serviceLabel("caddy"), "도메인 서버 (Caddy)");
  assert.equal(serviceLabel("hts"), "나루 화면");
  assert.equal(serviceLabel("dashboard"), "콘솔 화면");
  assert.equal(serviceLabel("something-new"), "something-new");
});

test("a service is on or off; the screens are fine without a dev server when Caddy serves the built files", () => {
  assert.deepEqual(serviceState({ name: "api", port: 4312, open: true }, {}), { text: "켜짐", ok: true });
  assert.deepEqual(serviceState({ name: "api", port: 4312, open: false }, {}), { text: "꺼짐", ok: false });
  assert.deepEqual(serviceState({ name: "hts", port: 4311, open: false }, { INAZINA_FRONTEND: "static" }), { text: "Caddy 가 직접 제공", ok: true });
  assert.deepEqual(serviceState({ name: "hts", port: 4311, open: false }, { INAZINA_FRONTEND: "dev" }), { text: "꺼짐", ok: false });
  assert.deepEqual(serviceState({ name: "api", port: 4312, open: false }, { INAZINA_FRONTEND: "static" }), { text: "꺼짐", ok: false }, "only the three screens");
  assert.deepEqual(serviceState({ name: "wl", port: 4315, open: false }, { INAZINA_FRONTEND: "static" }), { text: "꺼짐", ok: false }, "the wiki viewer always needs its server");
});
