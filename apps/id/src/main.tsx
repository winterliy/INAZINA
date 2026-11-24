// Fonts are bundled (no request to Google: the privacy policy says no visitor data leaves).
import "@fontsource/archivo/500.css";
import "@fontsource/archivo/700.css";
import "@fontsource/archivo/800.css";
import "@fontsource/dm-sans/400.css";
import "@fontsource/dm-sans/500.css";
import "@fontsource/dm-sans/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "@fontsource/ibm-plex-sans-kr/400.css";
import "@fontsource/ibm-plex-sans-kr/500.css";
import "@fontsource/ibm-plex-sans-kr/600.css";
import "@fontsource/ibm-plex-sans-kr/700.css";
import React from "react";
import ReactDOM from "react-dom/client";
import { AuthPanel } from "../../shared/AuthPanel";
import "./id.css";

// The login server serves this page at /oidc/interaction/<uid>: the uid ties every call to the login request in progress.
const uid = /^\/oidc\/interaction\/([\w-]+)\/?$/.exec(window.location.pathname)?.[1];

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {uid
      ? <AuthPanel idp={{ uid }} />
      : <main className="id-lost"><h1>로그인 요청을 찾지 못했습니다</h1><p>사용하던 앱에서 로그인을 다시 시작해 주세요.</p><a href="/login">로그인 시작</a></main>}
  </React.StrictMode>,
);
