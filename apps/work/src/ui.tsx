import { useCallback, useEffect, useState, type ReactNode } from "react";
import { errorText } from "./api.ts";

export function useLoad<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState("");
  const reload = useCallback(() => { loader().then((d) => { setData(d); setError(""); }).catch((e) => setError(errorText(e))); }, deps); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(reload, [reload]);
  return { data, error, reload };
}

export function Attempt({ action, children, className }: { action: () => Promise<unknown>; children: ReactNode; className?: string }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return <>
    <button className={className} disabled={busy} onClick={async () => { setBusy(true); setError(""); try { await action(); } catch (e) { setError(errorText(e)); } finally { setBusy(false); } }}>{children}</button>
    {error && <span className="err"> {error}</span>}
  </>;
}
export const Err = ({ text }: { text: string }) => (text ? <p className="err">{text}</p> : null);
export const Empty = ({ text = "비어 있습니다." }: { text?: string }) => <p className="muted">{text}</p>;
export const when = (value?: string | null) => (value ? value.slice(0, 16).replace("T", " ") : "");
export function Field({ label, children }: { label: string; children: ReactNode }) { return <label className="field"><span>{label}</span>{children}</label>; }
