import { useEffect, useState } from "react";
import { api, type ApiUser } from "../../shared/api";
import { Backdrop } from "./Backdrop";
import { BoardPanel } from "./BoardPanel";

/**
 * /board — the board as its own page. Anyone can read; a signed-in session
 * (if any) enables writing. "?asset=CODE" opens that instrument's discussion.
 */
export function BoardPage() {
  // undefined while the session check runs, null for a visitor.
  const [user, setUser] = useState<ApiUser | null | undefined>(undefined);
  useEffect(() => {
    api<{ user: ApiUser }>("/api/auth/session").then((result) => setUser(result.user)).catch(() => setUser(null));
  }, []);
  const asset = new URLSearchParams(location.search).get("asset") ?? "";
  const here = location.pathname + location.search;
  return <div className="stage board-stage"><Backdrop />
    {user !== undefined && <BoardPanel asPage user={user} initialAsset={asset}
      onClose={() => location.assign("/")}
      onLogin={() => location.assign(`/?return=${encodeURIComponent(here)}`)} />}
  </div>;
}
