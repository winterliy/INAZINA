import { formatMoney } from "../../shared/money";
import type { ReactNode } from "react";
import { tabButton, tabListKeys } from "./tabKeys";
import { confirmDialog } from "../../shared/dialogs";

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

// A question that ends in one of these verbs is destructive: red confirm button, focus on "cancel", and the verb as the button label.
const DESTRUCTIVE: [RegExp, string][] = [[/삭제(할|하시)?까요|지울까요/, "삭제"], [/초기화할까요/, "초기화"], [/해제할까요/, "해제"], [/폐지할까요/, "폐지"], [/종료할까요/, "종료"], [/뺄까요/, "빼기"], [/거둘까요/, "철회"], [/버리고/, "버리기"]];
/** window.confirm replacement: the dialog keeps the original question, destructive ones get the danger styling. */
export function confirmAction(message: string): Promise<boolean> {
  const verb = DESTRUCTIVE.find(([pattern]) => pattern.test(message))?.[1];
  return confirmDialog(verb ? { title: `${verb} 확인`, message, danger: true, confirmLabel: verb } : message);
}

/** Resolves true when the work (and its done callback) succeeded, false when it failed and a notice was shown. */
export type Run = <T>(work: () => Promise<T>, fallback: string, done?: (result: T) => void) => Promise<boolean>;

/** What every page needs from the shell: request runner, notices and busy state. */
export type Ctx = {
  busy: boolean;
  run: Run;
  say: (text: string, error?: boolean) => void;
  query: string;
  openCommand: (name: string, args: string, national?: boolean) => void;
};

/** `panel`: role/id/aria attributes for a tabpanel, put on the card itself so it stays a direct child of its container (the layout CSS depends on that). */
export function Card({ title, caption, actions, children, className = "", panel }: { title: string; caption?: ReactNode; actions?: ReactNode; children?: ReactNode; className?: string; panel?: Record<string, unknown> }) {
  return <section className={`glass card mb rise ${className}`} {...panel}>
    <div className="card-head"><div><h2>{title}</h2>{caption && <p className="cap">{caption}</p>}</div>{actions && <div className="actions">{actions}</div>}</div>
    <div className="card-body">{children}</div>
  </section>;
}

export function Tabs<T extends string>({ value, options, onChange, label, group }: { value: T; options: [T, string, number?][]; onChange: (value: T) => void; label: string; group: string }) {
  return <div className="segment-tabs mb" role="tablist" aria-label={label} onKeyDown={tabListKeys}>
    {options.map(([id, text, count]) => <button key={id} type="button" {...tabButton(group, id, value === id)} className={value === id ? "active" : ""} onClick={() => onChange(id)}>{text}{count !== undefined && <span>{count}</span>}</button>)}
  </div>;
}

export function Field({ label, hint, wide, error, children }: { label: string; hint?: string; wide?: boolean; error?: string; children: ReactNode }) {
  const className = [wide ? "span-wide" : "", error ? "invalid" : ""].filter(Boolean).join(" ") || undefined;
  return <label className={className}>{label}{hint && <small>{hint}</small>}{children}{error && <small className="field-error" role="alert">{error}</small>}</label>;
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
