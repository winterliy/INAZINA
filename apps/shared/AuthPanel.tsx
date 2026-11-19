import { Logo } from "./Logo";
import { useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api, type ApiUser } from "./api";
import "./typography.css";
import "./auth.css";
import sceneSvg from "./scene.svg?raw";

type Mode = "login" | "register" | "find-id" | "reset-password";
type Country = { countryId: string; currencyName: string; currencyCode: string };

const titles: Record<Mode, string> = {
  login: "로그인",
  register: "계정 만들기",
  "find-id": "아이디 찾기",
  "reset-password": "비밀번호 재설정",
};

export function AuthPanel({ app, onAuthenticated, extra }: { app: "dashboard" | "hts"; onAuthenticated: (user: ApiUser) => void; extra?: ReactNode }) {
  const [mode, setMode] = useState<Mode>("login");
  const [userId, setUserId] = useState("");
  const [verifiedId, setVerifiedId] = useState("");
  const [countryId, setCountryId] = useState("");
  const [countries, setCountries] = useState<Country[]>([]);
  const [discordConfigured, setDiscordConfigured] = useState(false);
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [show, setShow] = useState(false);

  useEffect(() => {
    void api<{ discordOAuthConfigured: boolean }>("/api/auth/config")
      .then((result) => setDiscordConfigured(result.discordOAuthConfigured));
    void api<{ countries: Country[] }>("/api/auth/countries")
      .then((result) => {
        setCountries(result.countries);
        setCountryId((current) => current || result.countries[0]?.countryId || "");
      })
      .catch((cause) => setError(cause instanceof Error ? cause.message : "국가 정보를 불러오지 못했습니다."));

    const query = new URLSearchParams(window.location.search);
    const authError = query.get("authError");
    if (authError) {
      const messages: Record<string, string> = {
        oauth_state_invalid: "인증 요청이 만료됐습니다. 다시 시도해주세요.",
        discord_authorization_failed: "Discord 인증을 완료하지 못했습니다.",
        discord_verification_failed: "Discord 계정을 확인하지 못했습니다.",
      };
      setError(messages[authError] ?? "Discord 인증에 실패했습니다.");
      window.history.replaceState({}, "", window.location.pathname);
    }
    if (query.get("discordAuth") === "verified") {
      void api<{ userId: string; nickname: string; purpose: Mode }>("/api/auth/discord/verification")
        .then((result) => {
          setVerifiedId(result.userId);
          setUserId(result.userId);
          setMode(result.purpose);
          setMessage(result.purpose === "find-id"
            ? `확인된 아이디: ${result.userId}`
            : "Discord 본인확인이 완료됐습니다.");
          window.history.replaceState({}, "", window.location.pathname);
        })
        .catch((cause) => setError(cause instanceof Error ? cause.message : "인증 상태를 확인하지 못했습니다."));
    }
  }, []);

  function switchMode(next: Mode) {
    setMode(next);
    setVerifiedId("");
    setError("");
    setMessage("");
    setPassword("");
    setConfirmation("");
    if (next !== "register" && next !== "reset-password" && next !== "find-id") setVerifiedId("");
  }

  function verifyWithDiscord(purpose: "register" | "find-id" | "reset-password") {
    if (!discordConfigured) {
      setError("Discord OAuth 설정이 필요합니다. 프로젝트 .env의 Discord client ID와 secret을 설정해주세요.");
      return;
    }
    const query = new URLSearchParams({ purpose, returnTo: app });
    window.location.assign(`/api/auth/discord/start?${query.toString()}`);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setMessage("");
    if ((mode === "register" || mode === "reset-password") && password !== confirmation) {
      setError("비밀번호 확인이 일치하지 않습니다.");
      return;
    }
    setBusy(true);
    try {
      if (mode === "login") {
        const result = await api<{ user: ApiUser }>("/api/auth/login", {
          method: "POST", body: JSON.stringify({ loginId: userId.trim(), password }),
        });
        onAuthenticated(result.user);
      } else if (mode === "register") {
        const result = await api<{ user: ApiUser }>("/api/auth/register", {
          method: "POST", body: JSON.stringify({ countryId, password }),
        });
        onAuthenticated(result.user);
      } else if (mode === "reset-password") {
        const result = await api<{ user: ApiUser }>("/api/auth/password/reset", {
          method: "POST", body: JSON.stringify({ password }),
        });
        onAuthenticated(result.user);
      }
    } catch (cause) {
      const reason = cause instanceof Error ? cause.message : "요청을 처리하지 못했습니다.";
      if (mode === "login" && reason === "account_not_found") {
        setError("입력한 아이디로 계정을 찾지 못했습니다. 아이디를 확인해주세요.");
      } else if (mode === "login" && reason === "invalid_credentials") {
        setError("아이디 또는 비밀번호가 일치하지 않습니다. 입력한 정보를 확인해주세요.");
      } else if (reason === "account_exists") {
        setMode("login");
        setError("이미 연결된 계정입니다. 로그인하거나 비밀번호를 재설정해주세요.");
      } else {
        setError(reason);
      }
    } finally {
      setBusy(false);
    }
  }

  const logoNote = app === "dashboard" ? "Dashboard" : "나루";
  return <main className="auth-shell">
    <aside className="auth-visual" aria-hidden="true"><AuthScene /></aside>
    <section className="auth-panel" aria-labelledby="auth-title">
      <a className="auth-wordmark" href="/" aria-label="INAZINA 홈"><Logo className="auth-logo" /><small>{logoNote}</small></a>
      <div className="auth-body">
        <h1 id="auth-title">{mode === "login" ? "다시 만나서 반갑습니다" : titles[mode]}</h1>
        {mode !== "login" && <p className="auth-subtitle">Discord 본인확인으로 계정을 보호합니다.</p>}

        {mode === "login" && <form className="auth-form" onSubmit={submit}>
          <label><span>아이디</span><input autoComplete="username" value={userId} onChange={(event) => setUserId(event.target.value)} placeholder="아이디" required /></label>
          <label><span>비밀번호</span>
            <span className="auth-secret"><input type={show ? "text" : "password"} autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="비밀번호" required />
              <button type="button" className="auth-eye" aria-label={show ? "비밀번호 숨기기" : "비밀번호 보기"} aria-pressed={show} onClick={() => setShow((value) => !value)}>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{show
                  ? <><path d="M3 3l18 18" /><path d="M10.6 10.6a2 2 0 002.8 2.8" /><path d="M9.9 5.1A9.8 9.8 0 0112 5c5 0 8.5 4.2 9.5 7a11 11 0 01-3.2 4.4M6.1 6.1A11.3 11.3 0 002.5 12c1 2.8 4.5 7 9.5 7a9.6 9.6 0 004.1-.9" /></>
                  : <><path d="M2.5 12S6 5 12 5s9.5 7 9.5 7-3.5 7-9.5 7S2.5 12 2.5 12z" /><circle cx="12" cy="12" r="3" /></>}</svg>
              </button></span></label>
          <div className="auth-row"><span /><button type="button" className="auth-link" onClick={() => switchMode("reset-password")}>비밀번호를 잊으셨나요?</button></div>
          <button className="auth-primary" disabled={busy}>{busy ? "확인 중…" : "로그인"}</button>
        </form>}

      {mode === "register" && <>
        {!verifiedId ? <div className="auth-step"><p>아이디의 소유자를 확인한 뒤 계정을 연결합니다.</p><button className="auth-primary" onClick={() => verifyWithDiscord("register")}>Discord로 본인확인</button></div> : <form className="auth-form" onSubmit={submit}>
          <label>아이디<input value={verifiedId} readOnly /></label>
          <label>국가 ID<select value={countryId} onChange={(event) => setCountryId(event.target.value)} required>{countries.map((country) => <option key={country.countryId} value={country.countryId}>{country.countryId} · {country.currencyName} ({country.currencyCode})</option>)}</select></label>
          <label>비밀번호<input type="password" autoComplete="new-password" minLength={12} maxLength={128} value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
          <label>비밀번호 확인<input type="password" autoComplete="new-password" minLength={12} maxLength={128} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} required /></label>
          <p className="auth-hint">영문과 숫자를 포함해 12자 이상 입력하세요.</p>
          <p className="auth-hint">새 사용자에게는 가입 지원금이 지급됩니다. 기존 계정 연결 시 잔액은 유지됩니다.</p>
          <button className="auth-primary" disabled={busy || !countries.length}>{busy ? "계정 생성 중…" : "계정 만들기"}</button>
        </form>}
      </>}

      {mode === "find-id" && <div className="auth-step">
        {verifiedId ? <><p>Discord 계정으로 확인된 아이디</p><div className="verified-id">{verifiedId}</div></> : <><p>Discord 인증을 완료하면 연결된 아이디를 확인할 수 있습니다.</p><button className="auth-primary" onClick={() => verifyWithDiscord("find-id")}>Discord로 아이디 확인</button></>}
      </div>}

      {mode === "reset-password" && <>
        {!verifiedId ? <div className="auth-step"><p>비밀번호를 바꾸려면 계정 소유자의 Discord 인증이 필요합니다.</p><button className="auth-primary" onClick={() => verifyWithDiscord("reset-password")}>Discord로 본인확인</button></div> : <form className="auth-form" onSubmit={submit}>
          <label>확인된 아이디<input value={verifiedId} readOnly /></label>
          <label>새 비밀번호<input type="password" autoComplete="new-password" minLength={12} maxLength={128} value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
          <label>새 비밀번호 확인<input type="password" autoComplete="new-password" minLength={12} maxLength={128} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} required /></label>
          <p className="auth-hint">영문과 숫자를 포함해 12자 이상 입력하세요.</p>
          <button className="auth-primary" disabled={busy}>{busy ? "변경 중…" : "비밀번호 변경"}</button>
        </form>}
      </>}

      {message && <p className="auth-message" role="status">{message}</p>}
      {error && <p className="auth-error" role="alert">{error}</p>}
      <div className="auth-divider" />
      <nav className="auth-links" aria-label="계정 메뉴">
        {mode !== "login" ? <button type="button" onClick={() => switchMode("login")}>로그인으로 돌아가기</button> : <>
          <span>계정이 없나요? <button type="button" onClick={() => switchMode("register")}>계정 만들기</button></span>
          <button type="button" onClick={() => switchMode("find-id")}>아이디 찾기</button></>}
      </nav>
      </div>
      {extra && <div className="auth-extra">{extra}</div>}
      <footer className="auth-foot"><span>계정은 Dashboard와 나루에서 함께 사용됩니다.</span><span>© INAZINA</span></footer>
    </section>
  </main>;
}

/** The sign-in picture: the inazina. wordmark rising letter by letter, the blue dots dropping in, the period sending out ripples.
 *  One SVG file (scene.svg) shared with the plain gate pages on 4312/4313. */
function AuthScene() {
  return <div className="auth-scene" dangerouslySetInnerHTML={{ __html: sceneSvg }} />;
}
