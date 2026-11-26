import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../shared/api";
import { AnnouncementCard } from "./AnnouncementCard";
import { needsConsentNow, type Announcement } from "./announcements";
import "./announcements.css";

/** Shown on the first screen after signing in while there is a change to the terms or the privacy policy this member has not seen: each
 *  announcement with its before/after comparison, a way to agree right here (the Discord DM button does the same), and "확인했습니다". */
export function AnnouncementModal() {
  const [items, setItems] = useState<Announcement[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const confirm = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let live = true;
    api<{ announcements: Announcement[] }>("/api/me/announcements").then((result) => { if (live) setItems(result.announcements); }).catch(() => { /* nothing to show */ });
    return () => { live = false; };
  }, []);
  useEffect(() => { if (items.length) confirm.current?.focus(); }, [items.length]);

  const seen = useCallback(async () => {
    const ids = items.map((item) => item.id);
    setItems([]);
    await api("/api/me/announcements/seen", { method: "POST", body: JSON.stringify({ ids }) }).catch(() => { /* shown again next time */ });
  }, [items]);
  useEffect(() => {
    if (!items.length) return undefined;
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") void seen(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [items.length, seen]);

  const agree = async (item: Announcement) => {
    setBusy(true);
    setError("");
    try {
      await api("/api/me/consent", { method: "POST", body: JSON.stringify({ campaignId: item.campaignId }) });
      setItems((current) => current.map((entry) => entry.id === item.id ? { ...entry, consented: true } : entry));
    } catch (cause) {
      setError(cause instanceof Error && cause.message ? cause.message : "동의를 저장하지 못했습니다. 잠시 후 다시 시도해 주세요.");
    } finally { setBusy(false); }
  };

  if (!items.length) return null;
  return <div className="announce-backdrop" role="presentation">
    <section className="announce-modal" role="dialog" aria-modal="true" aria-labelledby="announce-title">
      <h2 id="announce-title">📢 약관·방침 개정 안내</h2>
      <p className="announce-lead">이용약관 또는 개인정보 처리방침이 바뀝니다. 변경된 부분을 확인해 주세요.</p>
      {items.map((item) => <AnnouncementCard key={item.id} item={item}>
        {needsConsentNow(item) && <button type="button" className="announce-agree" disabled={busy} onClick={() => void agree(item)}>지금 동의하기</button>}
        {item.consented === true && <p className="announce-done">✅ 동의하셨습니다.</p>}
      </AnnouncementCard>)}
      {error && <p className="announce-error" role="alert">{error}</p>}
      <footer>
        <a href="/announcements">개정 안내 전체 보기</a>
        <button ref={confirm} type="button" className="announce-ok" onClick={() => void seen()}>확인했습니다</button>
      </footer>
    </section>
  </div>;
}
