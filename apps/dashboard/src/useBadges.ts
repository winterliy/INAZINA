import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ApiUser } from "../../shared/api";

/** Unread Contact/Report and pending naturalization counts for the nav badges, refreshed every minute while the tab is visible. */
export function useBadges(user: ApiUser | null, page: string, say: (text: string) => void) {
  const [feedback, setFeedback] = useState(0);
  const [naturalization, setNaturalization] = useState(0);
  const seen = useRef(0);

  const reload = useCallback(async () => {
    try {
      const result = await api<{ unread: number }>("/api/admin/feedback?status=new");
      if (result.unread > seen.current) say(`공식 사이트에서 새 문의·신고가 ${result.unread - seen.current}건 왔습니다.`);
      seen.current = result.unread;
      setFeedback(result.unread);
      setNaturalization((await api<{ pending: number }>("/api/admin/naturalization")).pending);
    } catch { /* the badge is best effort */ }
  }, [say]);
  const reset = useCallback(() => { setFeedback(0); setNaturalization(0); seen.current = 0; }, []);

  useEffect(() => {
    if (!user || user.role !== "admin" || page === "ownership") return;
    void reload();
    // Skip polling while the tab is hidden and catch up as soon as it is shown again.
    const timer = window.setInterval(() => { if (!document.hidden) void reload(); }, 60_000);
    const onVisible = () => { if (!document.hidden) void reload(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", onVisible); };
  }, [user, page, reload]);

  return { badges: { feedback, naturalization }, reload, reset };
}
