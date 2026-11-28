import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import test from "node:test";
import { DOC_SLUGS, footerBar, footerColumns } from "./footerLinks.ts";

const links = new URL("../../site/public/links/", import.meta.url);
const all = () => [...footerColumns("http://naru/", "http://naru/board").flatMap((column) => column.links), ...footerBar()];

test("every document the footer links to exists as a public document", () => {
  for (const slug of DOC_SLUGS) assert.ok(existsSync(new URL(`${slug}.txt`, links)), `${slug}.txt is missing`);
});

test("document links point at the official site's page for that document", () => {
  const docs = all().filter((link) => DOC_SLUGS.some((slug) => link.href.endsWith(`/${slug}/`)));
  assert.ok(docs.length >= DOC_SLUGS.length, "each document is linked at least once");
  assert.ok(docs.every((link) => link.href.startsWith("https://")));
});

test("the support links open a mail with a subject so the operators can sort them", () => {
  const mails = all().filter((link) => link.href.startsWith("mailto:"));
  assert.ok(mails.length >= 2);
  assert.ok(mails.every((link) => link.href.includes("subject=")));
});

test("the policy row names the privacy policy and the terms", () => {
  const labels = footerBar().map((link) => link.label);
  for (const wanted of ["Contact", "개인정보 처리방침", "이용약관", "운영규칙"]) assert.ok(labels.includes(wanted), wanted);
});

test("the app links use the addresses passed in, so they follow the console's host", () => {
  const columns = footerColumns("http://example:4311/", "http://example:4311/board");
  const hrefs = columns.flatMap((column) => column.links.map((link) => link.href));
  assert.ok(hrefs.includes("http://example:4311/"));
  assert.ok(hrefs.includes("http://example:4311/board"));
});
