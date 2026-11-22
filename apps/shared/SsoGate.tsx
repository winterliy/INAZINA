import { useEffect, useState } from "react";
import { currentReturnTo, ssoAction, ssoLoginUrl } from "./sso";
import "./ssoGate.css";

// What an app (naru, console) shows when nobody is signed in. The apps have no login of their own: this sends the visitor to
// the login server (id.<domain>), or says why the last attempt failed and offers to try again. `autoRedirect`: go at once (the
// console); the HTS only shows it when asked (register, reset, a sign-in error), because its pages are open to visitors.
// `onBrowse`: where the page lets visitors, a button on the error screen, carry on without signing in (the HTS).
export function SsoGate({ autoRedirect, onBrowse }: { autoRedirect: boolean; onBrowse?: () => void }) {
  // Decided once, on arrival: the address is cleaned up below, and a later render must not change the answer.
  const [action] = useState(() => ssoAction(true, { pathname: window.location.pathname, search: window.location.search }, { autoRedirect }));
  const login = () => window.location.assign(ssoLoginUrl(currentReturnTo(window.location)));
  useEffect(() => {
    if (action.kind === "redirect") window.location.replace(action.url);
    else if (action.kind === "error") window.history.replaceState(null, "", currentReturnTo(window.location));
  }, [action]);
  if (action.kind === "redirect") return <main className="sso-gate"><p role="status">로그인 화면으로 이동하는 중…</p></main>;
  if (action.kind === "error") return <main className="sso-gate"><h1>로그인하지 못했습니다</h1><p role="alert">{action.message}</p><button type="button" onClick={login}>다시 로그인</button>{onBrowse && <button type="button" className="sso-gate-quiet" onClick={onBrowse}>로그인 없이 둘러보기</button>}</main>;
  return <main className="sso-gate"><h1>로그인이 필요합니다</h1><button type="button" onClick={login}>로그인</button></main>;
}
