// Run: node --import tsx --test apps/wl/src/wlmd.test.ts
import assert from "node:assert/strict";
import { test } from "node:test";
import { filterThread, finalStatus, inline, markdown, parse, threadEntries } from "./wlmd.ts";

const claude = `# CThread
### [C-003] CLAUDE · 2026-10-18 · T-04
- type: claim
- claim: 국내 개인신용평가에서 상환이력 정보의 반영 비중이 가장 크다.
- detail: 신용평가사가 공개한 평가요소별 비중을 확인했다.
  부채수준이 뒤를 잇는다.
- sources: S-101; S-102
- tier: S2
- confidence: medium
- status: proposed
- laplace: 4

\`\`\`markdown
### [C-999] CLAUDE · 2026-10-18 · T-04
- type: claim
\`\`\`

### [C-009] CLAUDE · 2026-10-19 · T-04
- type: correction
- re: C-003
- claim: 국내 개인신용평가에서 상환이력 정보의 반영 비중이 가장 크다.
- sources: S-101; S-005
- tier: S1
- confidence: high
- status: verified
- answers: G-004
`;
const codex = `### [G-004] CODEX · 2026-10-18 · T-04
- type: review
- re: C-003
- verdict: agree
- checked: S-101; S-005
`;

test("parses entries, continuation lines, and skips fenced examples", () => {
  const entries = parse(claude, "threads/cthread-claude.md");
  assert.deepEqual(entries.map((entry) => entry.id), ["C-003", "C-009"]);
  assert.equal(entries[0].author, "CLAUDE");
  assert.equal(entries[0].topic, "T-04");
  assert.equal(entries[0].fields.detail, "신용평가사가 공개한 평가요소별 비중을 확인했다. 부채수준이 뒤를 잇는다.");
});

test("a claim's status follows its author's latest correction", () => {
  const all = threadEntries({ "threads/cthread-claude.md": claude, "threads/gthread-codex.md": codex });
  assert.equal(all.length, 3);
  const root = all.find((entry) => entry.id === "C-003")!;
  assert.equal(finalStatus(root, all), "verified");
});

test("web text is escaped and ids are linked without nesting", () => {
  const html = inline('<script>alert(1)</script> see C-003 and S-101, [doc](RULES.md), [x](javascript:alert(1))');
  assert.ok(!html.includes("<script>"));
  assert.ok(html.includes('href="#/chat?focus=C-003"'));
  assert.ok(html.includes('href="#/sources?focus=S-101"'));
  assert.ok(html.includes('href="#/rules"'));
  assert.ok(!/href="javascript:/i.test(html), "non-http links are not clickable");
  assert.equal((html.match(/<a /g) ?? []).length, 3);
});

test("markdown renders tables and code blocks safely", () => {
  const html = markdown("| a | b |\n|---|---|\n| `x` | <b>y</b> |\n\n```\n<tag>\n```");
  assert.ok(html.includes("<table>"));
  assert.ok(html.includes("<code>x</code>"));
  assert.ok(html.includes("&#60;b&#62;y"));
  assert.ok(html.includes("<pre><code>&#60;tag&#62;</code></pre>"));
});

test("chat filters keep a claim's reply chain and respect the author", () => {
  const all = threadEntries({ "threads/cthread-claude.md": claude, "threads/gthread-codex.md": codex });
  const idsOf = (entries: { id: string }[]) => entries.map((entry) => entry.id).sort();
  assert.deepEqual(idsOf(filterThread(all, all, "all", "verified")), ["C-003", "C-009", "G-004"]);
  assert.deepEqual(idsOf(filterThread(all, all, "all", "open")), []);
  assert.deepEqual(idsOf(filterThread(all, all, "CODEX", "all")), ["G-004"]);
  assert.deepEqual(idsOf(filterThread(all, all, "CLAUDE", "verified")), ["C-003", "C-009"]);
});
