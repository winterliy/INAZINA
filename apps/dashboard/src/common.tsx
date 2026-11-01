import { formatMoney } from "../../shared/money";
import type { ReactNode } from "react";

export const number = formatMoney;
export const date = (value: number) => new Intl.DateTimeFormat("ko-KR", { dateStyle: "short", timeStyle: "medium" }).format(value * 1000);
export const errorText = (cause: unknown, fallback: string) => cause instanceof Error ? cause.message : fallback;
export const show = (value: unknown) => value === null || value === undefined || value === "" ? "—" : String(value);

// Discord markup in reused bot messages (**bold**, `code`, <t:epoch:F>) shown as plain text.
export function plain(text: string) {
  return text
    .replace(/<t:(\d+)(?::[a-zA-Z])?>/g, (_match, epoch: string) => date(Number(epoch)))
    .replace(/\*\*/g, "")
    .replace(/`{3}\w*\n?/g, "")
    .replace(/`/g, "");
}

export type Run = <T>(work: () => Promise<T>, fallback: string, done?: (result: T) => void) => Promise<void>;

/** What every page needs from the shell: request runner, notices and busy state. */
export type Ctx = {
  busy: boolean;
  run: Run;
  say: (text: string, error?: boolean) => void;
  query: string;
  openCommand: (name: string, args: string, national?: boolean) => void;
};

export function Card({ title, caption, actions, children, className = "" }: { title: string; caption?: ReactNode; actions?: ReactNode; children?: ReactNode; className?: string }) {
  return <section className={`glass card mb rise ${className}`}>
    <div className="card-head"><div><h2>{title}</h2>{caption && <p className="cap">{caption}</p>}</div>{actions && <div className="actions">{actions}</div>}</div>
    {children}
  </section>;
}

export function Tabs<T extends string>({ value, options, onChange, label }: { value: T; options: [T, string, number?][]; onChange: (value: T) => void; label: string }) {
  return <div className="segment-tabs mb" role="tablist" aria-label={label}>
    {options.map(([id, text, count]) => <button key={id} role="tab" aria-selected={value === id} className={value === id ? "active" : ""} onClick={() => onChange(id)}>{text}{count !== undefined && <span>{count}</span>}</button>)}
  </div>;
}

export function Field({ label, hint, wide, children }: { label: string; hint?: string; wide?: boolean; children: ReactNode }) {
  return <label className={wide ? "span-wide" : undefined}>{label}{hint && <small>{hint}</small>}{children}</label>;
}

/** Only fields whose text differs from the loaded value are sent. */
export function changed(current: Record<string, string>, original: Record<string, unknown>) {
  return Object.fromEntries(Object.entries(current).filter(([key, value]) => value !== (original[key] === null || original[key] === undefined ? "" : String(original[key]))));
}

export const text = (value: unknown) => value === null || value === undefined ? "" : String(value);

/** A tile value; long exact amounts shrink and wrap only after a comma, never inside a digit group. */
export function BigValue({ value }: { value: string }) {
  const long = value.length > 14;
  return <p className={long ? "tile-value long" : "tile-value"} title={value}>{long ? value.split(",").flatMap((part, index) => index ? [",", <wbr key={index} />, part] : [part]) : value}</p>;
}
