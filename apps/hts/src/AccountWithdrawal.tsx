import type { ApiUser } from "../../shared/api";
import { canRequestWithdrawal } from "./withdrawal";
import "./withdrawal.css";

/** The bottom of the account page: how to ask for withdrawal. There is no automatic withdrawal; the operators settle and answer. */
export function AccountWithdrawal({ user }: { user: ApiUser }) {
  if (!canRequestWithdrawal(user)) return null;
  return <details className="account-withdrawal">
    <summary>회원 탈퇴</summary>
    <div className="account-withdrawal-panel">
      <h3>탈퇴를 문의하시겠어요?</h3>
      <p>계정 탈퇴 전 보유 자산, 예금, 대출과 미처리 거래를 확인해야 합니다. 현재 자동 탈퇴 기능은 제공하지 않으므로, 고객센터에 탈퇴 요청을 보내면 운영진이 정산과 처리 방법을 안내합니다.</p>
      <a className="account-withdrawal-link" href="/info">고객센터에서 탈퇴 요청하기 →</a>
    </div>
  </details>;
}
