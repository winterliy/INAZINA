import { useEffect, useState } from "react";
import { api, clearToken, signedOut, type ApiUser } from "../../shared/api";
import { Backdrop } from "./Backdrop";
import { BoardPanel } from "./BoardPanel";

/**
 * /board — the board as its own page. Anyone can read; a signed-in session
 * (if any) enables writing. "?asset=CODE" opens that instrument's discussion.
 */
export function BoardPage() {
  // undefined while the session check runs, null for a visitor.
  // The HTS caches the signed-in user per tab; a slow server must not look like a sign-out.
  const [user, setUser] = useState<ApiUser | null | undefined>(() => { try { return JSON.parse(sessionStorage.getItem("inazina.hts.user") ?? "null") ?? undefined; } catch { return undefined; } });
  useEffect(() => {
    api<{ user: ApiUser }>("/api/auth/session").then((result) => setUser(result.user))
      .catch((cause) => { if (signedOut(cause) || user === undefined) setUser(null); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const asset = new URLSearchParams(location.search).get("asset") ?? "";
  const here = location.pathname + location.search;
  const logout = () => {
    clearToken();
    try { sessionStorage.removeItem("inazina.hts.user"); } catch { /* nothing cached */ }
    void api("/api/auth/logout", { method: "POST" });
    setUser(null);
  };
  return <div className="stage board-stage"><Backdrop />
    {user !== undefined && <BoardPanel user={user} initialAsset={asset} onLogout={logout}
      onLogin={() => location.assign(`/?return=${encodeURIComponent(here)}`)} />}
  </div>;
}
