import { useCallback, useEffect, useRef, useState } from "react";
import { api, clearToken, onUnauthorized, signedOut, type ApiUser } from "../../shared/api";
import { logoutRedirect } from "../../shared/sso";
import { errorText } from "./common";
import type { Page } from "./pages";

const SESSION_TRIES = 5;

/** Login state: the session check (retried while the server is unreachable), the expired-session drop and logout. */
export function useSession({ say, reset, go }: { say: (text: string, error?: boolean) => void; reset: () => void; go: (page: Page) => void }) {
  const [user, setUser] = useState<ApiUser | null>(null);
  const [checked, setChecked] = useState(false);
  const [sessionError, setSessionError] = useState("");
  const tries = useRef(0);
  const timer = useRef<number | undefined>(undefined);

  const loadSession = useCallback(async () => {
    window.clearTimeout(timer.current);
    try {
      const result = await api<{ user: ApiUser }>("/api/auth/session");
      tries.current = 0;
      setSessionError("");
      setUser(result.user);
      setChecked(true);
    } catch (cause) {
      // Only a refused session signs out; a slow or unreachable server is retried a few times.
      if (!signedOut(cause)) {
        tries.current += 1;
        if (tries.current >= SESSION_TRIES) { setSessionError(errorText(cause, "서버에 연결하지 못했습니다.")); return; }
        timer.current = window.setTimeout(() => void loadSession(), 3000);
        return;
      }
      setUser(null);
      setChecked(true);
    }
  }, []);

  useEffect(() => {
    void loadSession();
    return () => window.clearTimeout(timer.current);
  }, [loadSession]);

  // The server refused our session (expired or signed out elsewhere): drop to the login screen.
  useEffect(() => onUnauthorized(() => {
    setUser(null);
    reset();
    say("로그인이 만료되었습니다. 다시 로그인해주세요.", true);
  }), [reset, say]);

  const retry = useCallback(() => { tries.current = 0; setSessionError(""); void loadSession(); }, [loadSession]);

  async function logout() {
    // Leaving a country or company account ($login) returns to the administrator, on this page.
    const delegated = Boolean(user?.delegatedBy);
    let result: { returnedTo?: ApiUser; logoutUrl?: string } = {};
    try {
      result = await api<{ returnedTo?: ApiUser; logoutUrl?: string }>("/api/auth/logout", { method: "POST" });
    } catch (cause) {
      // An already-refused session is as good as logged out; any other failure leaves everything as it was.
      if (!signedOut(cause)) { say(errorText(cause, delegated ? "관리자 계정으로 돌아가지 못했습니다. 잠시 후 다시 시도해주세요." : "로그아웃하지 못했습니다. 잠시 후 다시 시도해주세요."), true); return; }
    }
    clearToken();
    reset();
    if (result.returnedTo) { setUser(result.returnedTo); go("account"); say(`관리자 ${result.returnedTo.nickname} 계정으로 돌아왔습니다.`); return; }
    // Ending the single sign-on session is a page navigation. Clearing the user first would mount the sign-in gate, whose
    // automatic redirect to the login server would overwrite it and leave the login server's session alive.
    const singleSignOut = logoutRedirect(result);
    if (singleSignOut) { window.location.assign(singleSignOut); return; }
    setUser(null);
  }

  return { user, setUser, checked, sessionError, retry, logout };
}
