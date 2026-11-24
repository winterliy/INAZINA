import { useState } from "react";
import { api, clearToken } from "./api";
import { confirmDialog } from "./dialogs";
import { logoutRedirect } from "./sso";
import "./withdrawal.css";

/** The bottom of an account page: leave the service. Everything the member owns passes to their nationality's treasury, so it is asked
 *  once in a dialog and cannot be undone; afterwards the session and the single sign-on end. Shown to ordinary members only. */
export function WithdrawalPanel() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const withdraw = async () => {
    const sure = await confirmDialog({
      title: "정말 탈퇴하시겠어요?",
      message: "현금, 예금, 보유 주식, 대출 등 모든 재산과 계약이 국적 국가의 국고로 귀속되며 돌려받을 수 없습니다. 이 작업은 되돌릴 수 없습니다.",
      confirmLabel: "탈퇴하기", cancelLabel: "취소", danger: true,
    });
    if (!sure) return;
    setBusy(true);
    setError("");
    try {
      const result = await api<{ logoutUrl?: string }>("/api/me/withdraw", { method: "POST", body: JSON.stringify({ confirm: true }) });
      clearToken();
      window.location.assign(logoutRedirect(result) ?? "/");
    } catch (cause) {
      setError(cause instanceof Error && cause.message ? cause.message : "탈퇴하지 못했습니다. 잠시 후 다시 시도해 주세요.");
      setBusy(false);
    }
  };

  return <details className="account-withdrawal">
    <summary>회원 탈퇴</summary>
    <div className="account-withdrawal-panel">
      <h3>탈퇴하시겠어요?</h3>
      <ul>
        <li>보유한 현금, 예금, 주식, 대출 등 모든 재산과 계약이 <b>국적 국가의 국고로 귀속</b>됩니다.</li>
        <li>체결되지 않은 주문은 취소되고, 게시글과 댓글, 알림, 로그인 정보는 삭제됩니다.</li>
        <li>탈퇴하면 되돌릴 수 없습니다. 같은 Discord 계정으로 다시 가입하면 새 계정으로 시작합니다.</li>
        <li>선물, 옵션, ELS, 법인 지분처럼 정리가 필요한 계약이 있으면 탈퇴가 막히며, 운영진에게 정리를 요청해야 합니다.</li>
      </ul>
      {error && <p className="account-withdrawal-error" role="alert">{error}</p>}
      <button type="button" className="account-withdrawal-button" disabled={busy} onClick={() => void withdraw()}>{busy ? "처리 중…" : "탈퇴하기"}</button>
    </div>
  </details>;
}
