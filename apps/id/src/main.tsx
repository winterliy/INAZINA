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
