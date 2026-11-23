import { useCallback, useEffect, useRef, useState } from "react";

export type Notice = { id: number; text: string; error: boolean };
const SUCCESS_NOTICE_MS = 4000;

/** The one notice bar: successes fade on their own, errors stay until dismissed, and a page change drops older notices. */
export function useNotice() {
  const [notice, setNotice] = useState<Notice | null>(null);
  const seq = useRef(0);
  const pageChangeSeq = useRef(0);

  const say = useCallback((text: string, error = false) => { seq.current += 1; setNotice({ id: seq.current, text, error }); }, []);
  const clear = useCallback(() => setNotice(null), []);
  // Every page change calls this first, so a notice raised in the same handler (after the change) survives it.
  const markPageChange = useCallback(() => { pageChangeSeq.current = seq.current; }, []);
  const dropStale = useCallback(() => setNotice((current) => current && current.id <= pageChangeSeq.current ? null : current), []);

  useEffect(() => {
    if (!notice || notice.error) return;
    const timer = window.setTimeout(() => setNotice((current) => current?.id === notice.id ? null : current), SUCCESS_NOTICE_MS);
    return () => window.clearTimeout(timer);
  }, [notice]);

  return { notice, say, clear, markPageChange, dropStale };
}

export function NoticeBar({ notice, onClose }: { notice: Notice | null; onClose: () => void }) {
  if (!notice) return null;
  return <div className={notice.error ? "notice error" : "notice ok"} role={notice.error ? "alert" : "status"}><span>{notice.text}</span><button type="button" className="ghost" aria-label="알림 닫기" onClick={onClose}>×</button></div>;
}
