// Run: node --import tsx --test apps/wl/src/settings.test.ts
import assert from "node:assert/strict";
import { test } from "node:test";
import { DEFAULTS, THEMES, normalize } from "./settings.ts";

test("every theme has a full 16-colour ANSI palette", () => {
  for (const theme of THEMES) {
    assert.equal(theme.ansi.length, 16, theme.id);
    for (const color of [theme.bg, theme.fg, theme.cursor, theme.selection, ...theme.ansi]) assert.match(color, /^#[0-9a-f]{6}$/, theme.id);
  }
});

test("normalize keeps known values and drops broken ones", () => {
  assert.deepEqual(normalize(null), DEFAULTS);
  assert.deepEqual(normalize("x"), DEFAULTS);
  const kept = normalize({ theme: "nord", font: "mono", zoom: 110, chat: "log", autoRefresh: false, home: "chat",
    author: "CODEX", status: "open", order: "desc", collapse: true, notify: false }, ["overview", "chat"]);
  assert.deepEqual(kept, { theme: "nord", font: "mono", zoom: 110, chat: "log", autoRefresh: false, home: "chat",
    author: "CODEX", status: "open", order: "desc", collapse: true, notify: false });
  const bad = normalize({ theme: "nope", font: "serif", zoom: 300, chat: 1, autoRefresh: null, home: "gone",
    author: "ME", status: "x", order: 1, collapse: "y", notify: 0 }, ["overview"]);
  assert.deepEqual(bad, DEFAULTS);
});

test("Laplace Blue is the default theme and matches the entry screen", () => {
  assert.equal(DEFAULTS.theme, "laplace-blue");
  const blue = THEMES.find((theme) => theme.id === "laplace-blue")!;
  assert.deepEqual([blue.name, blue.bg, blue.fg], ["Laplace Blue", "#007be5", "#ffffff"]);
  assert.equal(normalize({ theme: "system" }).theme, "system");  // the system theme stays available
});
