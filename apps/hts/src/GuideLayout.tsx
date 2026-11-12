import { useEffect, useRef, useState } from "react";
import type { MouseEvent, ReactNode } from "react";

export const pad = (value: number) => String(value).padStart(2, "0");

/** Sticky numbered sidebar + content column (reference: Straightforward Brand Guidelines).
 *  Hash links in the sidebar scroll smoothly and the link for the section in view is marked aria-current. */
export function GuideLayout({ nav, footer, children }: { nav: ReactNode; footer?: ReactNode; children: ReactNode }) {
  const navRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const links = [...(navRef.current?.querySelectorAll<HTMLAnchorElement>('a[href^="#"]') ?? [])];
    const targets = links.map((link) => document.getElementById(link.hash.slice(1))).filter((node): node is HTMLElement => Boolean(node));
    if (!targets.length) return;
    const visible = new Set<string>();
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => entry.isIntersecting ? visible.add(entry.target.id) : visible.delete(entry.target.id));
      const current = targets.find((node) => visible.has(node.id))?.id;
      links.forEach((link) => link.toggleAttribute("aria-current", link.hash === `#${current}`));
    }, { rootMargin: "-30% 0px -60% 0px" });
    targets.forEach((node) => observer.observe(node));
    return () => observer.disconnect();
  }, [nav]);
  const scrollTo = (event: MouseEvent) => {
    const link = (event.target as Element).closest<HTMLAnchorElement>('a[href^="#"]');
    const target = link && document.getElementById(link.hash.slice(1));
    if (!target) return;
    event.preventDefault();
    target.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
    history.replaceState(null, "", link.hash);
  };
  return <div className="bg-page">
    <aside className="bg-side"><nav ref={navRef} onClickCapture={scrollTo}>{nav}</nav>{footer && <div className="bg-side-foot">{footer}</div>}</aside>
    <div className="bg-main">{children}</div>
  </div>;
}

/** Hero band: a data background with the headline figure laid over it. */
export function Hero({ background, dark = false, children }: { background: ReactNode; dark?: boolean; children: ReactNode }) {
  return <div className={dark ? "bg-hero on-dark" : "bg-hero"}>
    {background}
    <div className="bg-hero-content">{children}</div>
  </div>;
}

export function Section({ id, index, title, aside, children }: { id?: string; index: number; title: string; aside?: ReactNode; children: ReactNode }) {
  return <section className="bg-section" id={id}>
    <header className="bg-section-head"><h2><span>{pad(index)}</span>{title}</h2>{aside && <div className="bg-aside">{aside}</div>}</header>
    {children}
  </section>;
}

export type Stat = { label: string; value: string; unit?: string; tone?: "up" | "down" };

/** Hero figure that cycles through several stats (label above, unit below). Pauses on hover/focus. */
export function RotatingStat({ stats, interval = 4000 }: { stats: Stat[]; interval?: number }) {
  const [index, setIndex] = useState(0);
  const [paused, setPaused] = useState(false);
  const count = stats.length;
  useEffect(() => {
    if (paused || count < 2) return;
    const timer = window.setInterval(() => setIndex((value) => (value + 1) % count), interval);
    return () => window.clearInterval(timer);
  }, [paused, count, interval]);
  const stat = stats[index % Math.max(1, count)];
  if (!stat) return null;
  return <div className="bg-stat" onPointerEnter={() => setPaused(true)} onPointerLeave={() => setPaused(false)} onFocus={() => setPaused(true)} onBlur={() => setPaused(false)}>
    <div className="bg-stat-body" key={index % count} aria-live="polite">
      <small>{stat.label}</small>
      <strong className={stat.tone ?? ""}>{stat.value}</strong>
      {stat.unit && <span>{stat.unit}</span>}
    </div>
    {count > 1 && <div className="bg-stat-dots" role="tablist" aria-label="표시할 정보">{stats.map((item, position) => <button key={item.label} type="button" role="tab" aria-selected={position === index % count} aria-label={item.label} onClick={() => setIndex(position)} />)}</div>}
  </div>;
}

/** True when a #rrggbb colour is dark enough that white text reads better (WCAG relative luminance). */
export function isDark(hex: string) {
  const [r, g, b] = [1, 3, 5].map((start) => { const c = parseInt(hex.slice(start, start + 2), 16) / 255; return c <= .03928 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4; });
  return .2126 * r + .7152 * g + .0722 * b < .3;
}
