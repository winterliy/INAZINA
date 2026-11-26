import assert from "node:assert/strict";
import test from "node:test";
import { renderNoticeBar, renderNotices, renderFeed, type Notice } from "./noticesHtml.ts";

const base: Notice = {
  id: 12, document: "terms", title: "서비스 이용약관", status: "open", announcedEpoch: 1795000000, effectiveEpoch: 1797600000, unfavourable: true,
  changes: [{ section: "제10조 (탈퇴)", before: ["옛 문장 <b>"], after: ["새 문장", "추가 문장"] }],
};

test("a notice shows its state, dates, the before/after comparison and links to the full document; everything is escaped", () => {
  const html = renderNotices([base]);
  assert.match(html, /시행 예정/);
  assert.match(html, /서비스 이용약관 개정 안내/);
  assert.match(html, /제10조 \(탈퇴\)/);
  assert.match(html, /변경 전/);
  assert.match(html, /변경 후/);
  assert.match(html, /새 문장/);
  assert.match(html, /href="\.\.\/terms\/"/, "the notice page sits one level down, like the documents");
  assert.doesNotMatch(html, /<b>/, "document text is never markup");
  assert.match(html, /옛 문장 &lt;b&gt;/);
  assert.match(html, /id="notice-12"/);
});

test("only an unfavourable change asks for agreement; a plain one says continued use is agreement; a cancelled one asks for nothing", () => {
  assert.match(renderNotices([base]), /동의합니다/);
  assert.match(renderNotices([{ ...base, unfavourable: false }]), /계속 이용하면 동의한 것으로 봅니다/);
  const cancelled = renderNotices([{ ...base, status: "cancelled" }]);
  assert.match(cancelled, /취소됨/);
  assert.doesNotMatch(cancelled, /동의합니다/);
  assert.match(renderNotices([{ ...base, status: "applied" }]), /시행 중/);
});

test("an empty list says so", () => {
  assert.match(renderNotices([]), /아직 공지가 없습니다/);
});

test("the home page bar names the newest open announcement and links to the list; nothing open means no bar", () => {
  const bar = renderNoticeBar([{ ...base, id: 3, status: "applied" }, base], "notices/");
  assert.match(bar, /서비스 이용약관/);
  assert.match(bar, /href="notices\/"/);
  assert.equal(renderNoticeBar([{ ...base, status: "cancelled" }], "notices/"), "");
  assert.equal(renderNoticeBar([], "notices/"), "");
});

test("the feed has one entry per notice with a link to its place on the notice page", () => {
  const xml = renderFeed([base], "https://example.github.io/INAZINA/", 1795000000);
  assert.match(xml, /<feed xmlns="http:\/\/www\.w3\.org\/2005\/Atom">/);
  assert.match(xml, /https:\/\/example\.github\.io\/INAZINA\/notices\/#notice-12/);
  assert.match(xml, /서비스 이용약관/);
  assert.equal((xml.match(/<entry>/g) ?? []).length, 1);
});
