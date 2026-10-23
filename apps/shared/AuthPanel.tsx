import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api, type ApiUser } from "./api";
import "./typography.css";
import "./auth.css";

type Mode = "login" | "register" | "find-id" | "reset-password";
type Country = { countryId: string; currencyName: string; currencyCode: string };

const titles: Record<Mode, string> = {
  login: "로그인",
  register: "계정 만들기",
  "find-id": "아이디 찾기",
  "reset-password": "비밀번호 재설정",
};

export function AuthPanel({ app, onAuthenticated }: { app: "dashboard" | "hts"; onAuthenticated: (user: ApiUser) => void }) {
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

  return <main className="auth-shell">
    <section className="auth-panel" aria-labelledby="auth-title">
      <a className="auth-wordmark" href="/" aria-label="INAZINA 홈">INAZINA <span>{app === "dashboard" ? "DASHBOARD" : "HTS"}</span></a>
      <div className="auth-heading">
        <p className="auth-eyebrow">INAZINA ACCOUNT</p>
        <h1 id="auth-title">{titles[mode]}</h1>
        <p className="auth-subtitle">{mode === "login"
          ? "아이디와 비밀번호로 접속하세요."
          : "Discord 본인확인으로 계정을 보호합니다."}</p>
      </div>

      {mode === "login" && <form className="auth-form" onSubmit={submit}>
        <label>아이디<input autoComplete="username" value={userId} onChange={(event) => setUserId(event.target.value)} placeholder="아이디" required /></label>
        <label>비밀번호<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
        <button className="auth-primary" disabled={busy}>{busy ? "확인 중…" : "로그인"}</button>
      </form>}

      {mode === "register" && <>
        {!verifiedId ? <div className="auth-step"><p>아이디의 소유자를 확인한 뒤 계정을 연결합니다.</p><button className="auth-primary" onClick={() => verifyWithDiscord("register")}>Discord로 본인확인</button></div> : <form className="auth-form" onSubmit={submit}>
          <label>아이디<input value={verifiedId} readOnly /></label>
          <label>국가 ID<select value={countryId} onChange={(event) => setCountryId(event.target.value)} required>{countries.map((country) => <option key={country.countryId} value={country.countryId}>{country.countryId} · {country.currencyName} ({country.currencyCode})</option>)}</select></label>
          <label>비밀번호<input type="password" autoComplete="new-password" minLength={12} maxLength={128} value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
          <label>비밀번호 확인<input type="password" autoComplete="new-password" minLength={12} maxLength={128} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} required /></label>
          <p className="auth-hint">영문과 숫자를 포함해 12자 이상 입력하세요.</p>
          <p className="auth-hint">새 사용자에게는 기존 봇 가입 기준의 1,000,000 기준 통화가 지급됩니다. 기존 계정 연결 시 잔액은 유지됩니다.</p>
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
      <nav className="auth-links" aria-label="계정 메뉴">
        {mode !== "login" && <button onClick={() => switchMode("login")}>로그인으로</button>}
        {mode !== "register" && <button onClick={() => switchMode("register")}>계정 만들기</button>}
        {mode !== "find-id" && <button onClick={() => switchMode("find-id")}>아이디 찾기</button>}
        {mode !== "reset-password" && <button onClick={() => switchMode("reset-password")}>비밀번호 찾기</button>}
      </nav>
      <p className="auth-footnote">계정은 Dashboard와 HTS에서 함께 사용됩니다.</p>
    </section>
  </main>;
}
