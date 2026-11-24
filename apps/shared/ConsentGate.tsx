import { useState } from "react";
import { api, type ConsentRequirement } from "./api";
import { WithdrawalPanel } from "./WithdrawalPanel";
import "./ssoGate.css";
import "./consentGate.css";

const when = (epoch: number) => new Date(epoch * 1000).toLocaleString("ko-KR", { timeZone: "Asia/Seoul", dateStyle: "long", timeStyle: "short" });

/** What an app shows instead of itself while a member is held: a revised document that was announced as unfavourable is now in force and
 *  they have not agreed to it. They can agree (the same button as in the Discord DM), leave (withdrawal) or sign out. */
export function ConsentGate({ held, onLogout }: { held: ConsentRequirement[]; onLogout: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const agree = async () => {
    setBusy(true);
    setError("");
    try {
      for (const item of held) await api("/api/me/consent", { method: "POST", body: JSON.stringify({ campaignId: item.campaignId }) });
      window.location.reload();
    } catch (cause) {
      setError(cause instanceof Error && cause.message ? cause.message : "동의를 저장하지 못했습니다. 잠시 후 다시 시도해 주세요.");
      setBusy(false);
    }
  };

  return <main className="sso-gate consent-gate">
    <h1>개정된 약관에 동의해 주세요</h1>
    <p>이용자에게 불리하거나 중요한 변경이 있는 문서가 적용되었습니다. 동의하면 계속 이용할 수 있고, 동의하지 않으려면 탈퇴할 수 있습니다.</p>
    {held.map((item) => <section key={item.campaignId} className="consent-doc">
      <h2>{item.title}</h2>
      <p className="consent-when">{when(item.effectiveEpoch)}부터 적용</p>
      <pre className="consent-summary" tabIndex={0} aria-label={`${item.title} 변경 내용`}>{item.summary || "변경된 내용은 도움말의 약관 문서에서 확인할 수 있습니다."}</pre>
    </section>)}
    <p className="consent-note">변경된 부분만 보여 드립니다. 전체 문서는 Discord 봇의 도움말에서 열 수 있습니다.</p>
    {error && <p role="alert">{error}</p>}
    <button type="button" disabled={busy} onClick={() => void agree()}>{busy ? "저장 중…" : "동의하고 계속하기"}</button>
    <button type="button" className="sso-gate-quiet" onClick={onLogout}>로그아웃</button>
    <WithdrawalPanel />
  </main>;
}
