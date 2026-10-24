import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api, type ApiUser } from "../../shared/api";

export type BoardDefaults = { anonymous: boolean; secret: boolean; commentAnonymous: boolean; commentSecret: boolean };
const DEFAULTS_KEY = "inazina.board.defaults";
const NONE: BoardDefaults = { anonymous: false, secret: false, commentAnonymous: false, commentSecret: false };

/** Writing defaults are a per-viewer convenience kept in this browser. */
export function loadBoardDefaults(): BoardDefaults {
  try { return { ...NONE, ...JSON.parse(localStorage.getItem(DEFAULTS_KEY) ?? "{}") }; } catch { return NONE; }
}

const ROLE: Record<ApiUser["role"], string> = { user: "개인 계정", admin: "관리자", nation: "국가 계정", company: "법인 계정" };

/** 게시판 계정설정: who you post as, your login ID, and how new posts and comments start. */
export function BoardAccount({ user, onClose, onChange }: { user: ApiUser; onClose: () => void; onChange: (defaults: BoardDefaults) => void }) {
  const [loginId, setLoginId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [defaults, setDefaults] = useState(loadBoardDefaults);
  useEffect(() => { api<{ htsId: string | null }>("/api/hts/account").then((result) => { setLoginId(result.htsId); setInput(result.htsId ?? ""); }).catch(() => undefined); }, []);

  function toggle(key: keyof BoardDefaults) {
    const next = { ...defaults, [key]: !defaults[key] };
    setDefaults(next);
    onChange(next);
    try { localStorage.setItem(DEFAULTS_KEY, JSON.stringify(next)); } catch { /* not remembered */ }
  }
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true); setMessage(null);
    try {
      const result = await api<{ htsId: string }>("/api/hts/account", { method: "POST", body: JSON.stringify({ htsId: input, password }) });
      setLoginId(result.htsId); setPassword(""); setMessage({ text: "로그인 아이디를 저장했습니다.", error: false });
    } catch (cause) {
      const reason = cause instanceof Error ? cause.message : "";
      setMessage({ text: reason === "invalid_credentials" ? "현재 비밀번호가 일치하지 않습니다." : reason || "저장하지 못했습니다.", error: true });
    } finally { setBusy(false); }
  }
  const options: [keyof BoardDefaults, string, string][] = [
    ["anonymous", "글을 익명으로 쓰기", "새 글의 '익명으로 표시'를 켠 채로 시작합니다."],
    ["secret", "글을 비밀글로 쓰기", "본문은 나와 관리자만 봅니다."],
    ["commentAnonymous", "댓글을 익명으로 쓰기", "댓글·답글 입력칸의 '익명'을 켠 채로 시작합니다."],
    ["commentSecret", "댓글을 비밀댓글로 쓰기", "나, 글쓴이, 관리자만 봅니다."],
  ];

  return <section className="board-account" aria-labelledby="board-account-title">
    <header><h3 id="board-account-title">계정설정</h3><button type="button" className="board-post-close" onClick={onClose}>닫기 ✕</button></header>
    <dl className="board-account-profile">
      <div><dt>닉네임</dt><dd>{user.nickname}</dd></div>
      <div><dt>계정</dt><dd>{ROLE[user.role] ?? user.role} · {user.nationality}</dd></div>
      <div><dt>로그인 아이디</dt><dd>{loginId ?? "미설정"}</dd></div>
    </dl>
    <h4>글쓰기 기본값</h4>
    <ul className="board-account-options">{options.map(([key, label, hint]) => <li key={key}><label className="board-check"><input type="checkbox" checked={defaults[key]} onChange={() => toggle(key)} />{label} <small>{hint}</small></label></li>)}</ul>
    <h4>로그인 아이디</h4>
    <form className="board-account-form" onSubmit={(event) => void save(event)}>
      <label>아이디<input autoComplete="username" minLength={3} maxLength={32} pattern="[A-Za-z0-9_.\-]{3,32}" required value={input} onChange={(event) => setInput(event.target.value)} /></label>
      <label>현재 비밀번호<input type="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} /></label>
      <button type="submit" className="board-primary" disabled={busy || !input || input === loginId}>{busy ? "저장 중…" : loginId ? "아이디 변경" : "아이디 설정"}</button>
      {message && <p className={message.error ? "board-error" : "board-notice"} role={message.error ? "alert" : "status"}>{message.text}</p>}
    </form>
    <p className="board-account-more">닉네임·지갑·본인확인 등 전체 계정 정보는 <a href="/account">HTS 계정 페이지</a>에서 봅니다.</p>
  </section>;
}
