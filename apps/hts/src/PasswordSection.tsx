import { useState } from "react";
import type { FormEvent } from "react";
import { api } from "../../shared/api";

const ERRORS: Record<string, string> = {
  invalid_credentials: "현재 비밀번호가 일치하지 않습니다.",
  password_policy: "새 비밀번호는 영문과 숫자를 포함해 12~128자여야 합니다.",
  password_unchanged: "현재 비밀번호와 다른 비밀번호를 입력해 주세요.",
  delegated_session: "관리자 대리 로그인 중에는 비밀번호를 바꿀 수 없습니다.",
};

/** 계정 페이지: change the password; the server reissues this browser's session. */
export function PasswordSection() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ text: string; ok: boolean } | null>(null);
  const policyOk = next.length >= 12 && /[A-Za-z]/.test(next) && /\d/.test(next);
  const mismatch = confirm.length > 0 && confirm !== next;

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (mismatch) return;
    setBusy(true);
    setMessage(null);
    try {
      await api("/api/hts/password", { method: "POST", body: JSON.stringify({ password: current, newPassword: next }) });
      setCurrent(""); setNext(""); setConfirm("");
      setMessage({ text: "비밀번호를 변경했습니다. 다른 기기의 로그인은 해제됩니다.", ok: true });
    } catch (cause) {
      const reason = cause instanceof Error ? cause.message : "";
      setMessage({ text: ERRORS[reason] ?? (reason || "비밀번호를 변경하지 못했습니다."), ok: false });
    } finally {
      setBusy(false);
    }
  }

  return <section className="account-section">
    <header><h2>비밀번호 변경</h2><p>영문과 숫자를 포함해 12자 이상. 변경하면 다른 기기의 로그인은 모두 해제됩니다.</p></header>
    <form className="account-form" onSubmit={save}>
      <label>현재 비밀번호<input type="password" autoComplete="current-password" value={current} onChange={(event) => setCurrent(event.target.value)} required /></label>
      <label>새 비밀번호<input type="password" autoComplete="new-password" minLength={12} maxLength={128} value={next} onChange={(event) => setNext(event.target.value)} required aria-invalid={next.length > 0 && !policyOk} /></label>
      {next.length > 0 && !policyOk && <p className="account-error">영문과 숫자를 포함해 12자 이상이어야 합니다.</p>}
      <label>새 비밀번호 확인<input type="password" autoComplete="new-password" value={confirm} onChange={(event) => setConfirm(event.target.value)} required aria-invalid={mismatch} /></label>
      {mismatch && <p className="account-error">새 비밀번호가 서로 다릅니다.</p>}
      {message && <p className={message.ok ? "account-success" : "account-error"} role={message.ok ? "status" : "alert"}>{message.text}</p>}
      <button className="account-save" disabled={busy || !current || !policyOk || confirm !== next}>{busy ? "변경 중…" : "비밀번호 변경"}</button>
    </form>
  </section>;
}
