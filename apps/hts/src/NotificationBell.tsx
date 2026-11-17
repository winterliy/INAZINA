import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../shared/api";
import { navigate } from "./nav";
import { ago } from "./notificationFormat";
import "./notifications.css";

export type NotificationItem = {
  id: number;
  kind: string;
  title: string;
  body: string;
  action_path: string | null;
  payload: Record<string, unknown>;
  created_epoch: number;
  read: boolean;
};

const KIND_LABEL: Record<string, string> = {
  payout_choice: "지급 선택", payout_temp_account: "임시계좌", payout_expired: "기한 만료",
  credit_score: "신용점수", loan_maturity: "대출", loan_overdue: "대출", loan_payment: "대출",
  deposit_maturity: "예금·적금", deposit_payment: "예금·적금", account_notice: "계좌",
  order_fill: "체결", futures_margin: "선물", settlement: "정산", option_expiry: "옵션",
  bond_redemption: "국채", rehab: "회생", naturalization: "귀화", feedback: "문의·신고",
};

const POLL_MS = 30_000;
const READ_DELAY_MS = 900;

/** The bell beside the account button: unread badge, the inbox panel, delete one or all. */
export function NotificationBell() {
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [unread, setUnread] = useState(0);
  const [open, setOpen] = useState(false);
  const [failed, setFailed] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    try {
      const inbox = await api<{ notifications: NotificationItem[]; unread: number }>("/api/me/notifications");
      setItems(inbox.notifications);
      setUnread(inbox.unread);
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void load(); }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  // Opening the panel reads everything in it, after a moment so the unread dots can still be seen.
  useEffect(() => {
    if (!open || unread === 0) return;
    const timer = window.setTimeout(() => {
      api("/api/me/notifications/read-all", { method: "POST", body: "{}" })
        .then(() => { setUnread(0); setItems((current) => current.map((item) => ({ ...item, read: true }))); })
        .catch(() => undefined);
    }, READ_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [open, unread]);

  useEffect(() => {
    if (!open) return;
    const away = (event: MouseEvent) => { if (wrap.current && !wrap.current.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", escape); };
  }, [open]);

  const remove = (id: number) => {
    setItems((current) => current.filter((item) => item.id !== id));
    api("/api/me/notifications/delete", { method: "POST", body: JSON.stringify({ id }) }).catch(() => void load());
  };
  const removeAll = () => {
    setItems([]);
    setUnread(0);
    api("/api/me/notifications/delete-all", { method: "POST", body: "{}" }).catch(() => void load());
  };
  const go = (path: string) => { setOpen(false); navigate(path); };

  return <div className="bell-wrap" ref={wrap}>
    <button type="button" className="icon-button bell-button" aria-label={unread ? `알림 ${unread}개 안 읽음` : "알림"}
      aria-expanded={open} aria-controls="notification-panel" onClick={() => setOpen((current) => !current)}>
      <svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M6 9a6 6 0 1 1 12 0c0 6 2.5 7.5 2.5 7.5h-17S6 15 6 9Z" /><path d="M10 20a2 2 0 0 0 4 0" /></svg>
      {unread > 0 && <span className="bell-badge">{unread > 9 ? "9+" : unread}</span>}
    </button>
    {open && <section id="notification-panel" className="bell-panel" aria-label="알림">
      <div className="bell-head"><h2>알림</h2>{items.length > 0 && <button type="button" className="bell-clear" onClick={removeAll}>전체삭제</button>}</div>
      {failed && items.length === 0 ? <p className="bell-empty">알림을 불러오지 못했습니다.</p>
        : items.length === 0 ? <p className="bell-empty">새 알림이 없습니다.</p>
        : <ul className="bell-list">{items.map((item) => <li key={item.id} className={item.read ? "bell-item" : "bell-item unread"}>
          <span className="bell-dot" aria-hidden="true" />
          <div className="bell-body">
            <b><span className="bell-kind">{KIND_LABEL[item.kind] ?? "알림"}</span>{item.title}</b>
            {item.body && <p>{item.body}</p>}
            {item.action_path && <button type="button" className="bell-go" onClick={() => go(item.action_path!)}>{item.kind === "payout_choice" ? "선택하기" : "자세히 보기"}</button>}
            <time dateTime={new Date(item.created_epoch * 1000).toISOString()}>{ago(item.created_epoch)}</time>
          </div>
          <button type="button" className="bell-x" aria-label="알림 지우기" onClick={() => remove(item.id)}>×</button>
        </li>)}</ul>}
    </section>}
  </div>;
}
