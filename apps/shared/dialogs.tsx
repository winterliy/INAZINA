import { useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import "./dialogs.css";

/**
 * One in-page replacement for window.confirm / prompt / alert, used by the dashboard and the HTS.
 * Mount <DialogHost /> once near the root, then `await confirmDialog("...")` where window.confirm was.
 * Without a mounted host the functions fall back to the browser's own dialogs, so nothing breaks.
 */
export type DialogOptions = {
  title?: string;
  message?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** A destructive action: the red confirm button, and focus starts on the cancel button. */
  danger?: boolean;
};
export type PromptOptions = DialogOptions & { label?: string; initial?: string; maxLength?: number; multiline?: boolean };

type Settle = (value: boolean | string | null) => void;
type Request = { id: number; kind: "confirm" | "alert" | "prompt"; options: PromptOptions; settle: Settle };

let push: ((request: Request) => void) | null = null;
let nextId = 0;
const options = (input: string | PromptOptions): PromptOptions => typeof input === "string" ? { message: input } : input;
const plain = (input: string | PromptOptions) => typeof input === "string" ? input : typeof input.message === "string" ? input.message : input.title ?? "";

export function confirmDialog(input: string | DialogOptions): Promise<boolean> {
  if (!push) return Promise.resolve(window.confirm(plain(input)));
  return new Promise((resolve) => push!({ id: ++nextId, kind: "confirm", options: options(input), settle: (value) => resolve(value === true) }));
}

export function alertDialog(input: string | DialogOptions): Promise<void> {
  if (!push) { window.alert(plain(input)); return Promise.resolve(); }
  return new Promise((resolve) => push!({ id: ++nextId, kind: "alert", options: options(input), settle: () => resolve() }));
}

/** Resolves to the entered text, or null when cancelled. */
export function promptDialog(input: string | PromptOptions): Promise<string | null> {
  if (!push) return Promise.resolve(window.prompt(plain(input), typeof input === "string" ? "" : input.initial ?? ""));
  return new Promise((resolve) => push!({ id: ++nextId, kind: "prompt", options: options(input), settle: (value) => resolve(typeof value === "string" ? value : null) }));
}

const FOCUSABLE = 'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])';

export function DialogHost() {
  const [queue, setQueue] = useState<Request[]>([]);
  useEffect(() => {
    push = (request) => setQueue((current) => [...current, request]);
    return () => { push = null; };
  }, []);
  const current = queue[0];
  if (!current) return null;
  const settle: Settle = (value) => { current.settle(value); setQueue((all) => all.slice(1)); };
  return <Dialog key={current.id} request={current} settle={settle} />;
}

function Dialog({ request, settle }: { request: Request; settle: Settle }) {
  const { kind, options: o } = request;
  const root = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const fieldRef = useRef<HTMLInputElement & HTMLTextAreaElement>(null);
  const [text, setText] = useState(o.initial ?? "");
  const titleId = useId();
  const bodyId = useId();
  const pressedOnBackdrop = useRef(false);
  const cancel = () => settle(kind === "prompt" ? null : kind === "alert" ? true : false);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    // A destructive question starts on "cancel", a text question on its field, anything else on the confirm button.
    (kind === "prompt" ? fieldRef.current : o.danger ? cancelRef.current : confirmRef.current)?.focus();
    return () => { document.body.style.overflow = overflow; previous?.focus?.(); };
  }, []);

  function onKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") { event.stopPropagation(); cancel(); return; }
    if (event.key !== "Tab") return;
    const items = [...(root.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])];
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }

  const submit = () => settle(kind === "prompt" ? text : true);
  return <div className="dialog-backdrop"
    onPointerDown={(event) => { pressedOnBackdrop.current = event.target === event.currentTarget; }}
    onClick={(event) => { if (pressedOnBackdrop.current && event.target === event.currentTarget) cancel(); }}>
    <div ref={root} className={o.danger ? "dialog danger" : "dialog"} role={kind === "prompt" ? "dialog" : "alertdialog"} aria-modal="true"
      aria-labelledby={o.title ? titleId : undefined} aria-describedby={o.message ? bodyId : undefined} aria-label={o.title ? undefined : o.label ?? "확인"} onKeyDown={onKeyDown}>
      {o.title && <h2 id={titleId}>{o.title}</h2>}
      {o.message && <div id={bodyId} className="dialog-message">{o.message}</div>}
      {kind === "prompt" && <label className="dialog-field">{o.label && <span>{o.label}</span>}
        {o.multiline
          ? <textarea ref={fieldRef} rows={4} value={text} maxLength={o.maxLength} onChange={(event) => setText(event.target.value)} />
          : <input ref={fieldRef} value={text} maxLength={o.maxLength} onChange={(event) => setText(event.target.value)}
              onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); submit(); } }} />}
        {o.maxLength ? <small>{text.length}/{o.maxLength}</small> : null}
      </label>}
      <div className="dialog-actions">
        {kind !== "alert" && <button ref={cancelRef} type="button" className="dialog-button" onClick={cancel}>{o.cancelLabel ?? "취소"}</button>}
        <button ref={confirmRef} type="button" className={o.danger ? "dialog-button danger" : "dialog-button primary"} onClick={submit}>{o.confirmLabel ?? "확인"}</button>
      </div>
    </div>
  </div>;
}
