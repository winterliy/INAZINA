import { PUBLIC_SITE } from "../../shared/links";

export type FooterLink = { label: string; href: string };
export type FooterColumn = { title: string; links: FooterLink[] };

/** The documents on the official site (apps/site/public/links/<slug>.txt). */
export const DOC_SLUGS = ["terms", "privacy", "operations", "internal", "help"] as const;

const doc = (slug: (typeof DOC_SLUGS)[number]) => `${PUBLIC_SITE}${slug}/`;
const EMAIL = "w2nterist@gmail.com";
const mail = (subject: string) => `mailto:${EMAIL}?subject=${encodeURIComponent(`[INAZINA ${subject}]`)}`;

/** The four link columns: where the app and the board are depends on the host the console is opened on. */
export const footerColumns = (naru: string, board: string): FooterColumn[] => [
  { title: "서비스", links: [{ label: "나루", href: naru }, { label: "광장", href: board }, { label: "공식 사이트", href: PUBLIC_SITE }, { label: "명령어 도움말", href: doc("help") }] },
  { title: "지원", links: [{ label: "Contact (문의)", href: mail("문의") }, { label: "Report (신고)", href: mail("신고") }, { label: "공지", href: `${PUBLIC_SITE}notices/` }, { label: "공지 구독 (Atom)", href: `${PUBLIC_SITE}feed.xml` }] },
  { title: "약관과 방침", links: [{ label: "이용약관", href: doc("terms") }, { label: "개인정보 처리방침", href: doc("privacy") }] },
  { title: "운영정책", links: [{ label: "서비스 운영규칙", href: doc("operations") }, { label: "서비스 내부규칙", href: doc("internal") }] },
];

/** The row along the bottom edge. */
export const footerBar = (): FooterLink[] => [
  { label: "Contact", href: mail("문의") },
  { label: "개인정보 처리방침", href: doc("privacy") },
  { label: "이용약관", href: doc("terms") },
  { label: "운영규칙", href: doc("operations") },
];
