import { date, type DrVotingDelegation } from "./appShared";
import type { Instrument } from "./types";

type Props = {
  selected: Instrument;
  busy: boolean;
  drQuantity: string;
  onDrQuantity: (value: string) => void;
  drVoteQuantity: string;
  onDrVoteQuantity: (value: string) => void;
  drProxyUserId: string;
  onDrProxyUserId: (value: string) => void;
  delegations: DrVotingDelegation[];
  onConvert: (action: "issue" | "redeem") => void;
  onDelegate: () => void;
  onRevoke: (delegation: DrVotingDelegation) => void;
};

/** DR issue/redeem and voting delegation for the selected depositary receipt. */
export function DrPanel({ selected, busy, drQuantity, onDrQuantity, drVoteQuantity, onDrVoteQuantity, drProxyUserId, onDrProxyUserId, delegations, onConvert, onDelegate, onRevoke }: Props) {
  return <section className="data-panel order-entry dr-conversion"><div className="panel-title"><h2>DR 발행 · 상환</h2><span>{selected.drProgram?.programStatus === "active" && selected.status === "active" ? "기초주식 예탁 방식" : "프로그램 중지"}</span></div>
      <p className="orders-hint">{selected.drProgram?.programStatus === "active" && selected.status === "active" ? "DR 수량을 입력하세요. 발행은 보유 원주를 잠그고 DR을 지급하며, 상환은 보유 DR을 소각하고 보관 원주를 지급합니다." : "중지되거나 종료된 DR 프로그램은 새 전환을 할 수 없습니다."}</p>
      <label>전환할 DR 수량<input inputMode="numeric" value={drQuantity} onChange={(event) => onDrQuantity(event.target.value.replace(/\D/g, ""))} /></label>
      <div className="actions"><button className="submit buy-submit" disabled={busy || !Number(drQuantity) || selected.drProgram?.programStatus !== "active" || selected.status !== "active"} onClick={() => void onConvert("issue")}>원주 예탁 · DR 발행</button><button className="submit sell-submit" disabled={busy || !Number(drQuantity) || selected.drProgram?.programStatus !== "active" || selected.status !== "active"} onClick={() => void onConvert("redeem")}>DR 반납 · 원주 상환</button></div>
      <hr />
      <h3>DR 의결권 위임</h3>
      <p className="orders-hint">시뮬레이터 내부 위임 기록입니다. 위임된 수량은 매도·상환할 수 없으며, 기준일 의결권은 주주총회 투표에서 대리인에게 합산됩니다. 실제 수탁기관 지시는 포함하지 않습니다.</p>
      <label>대리 사용자 ID<input value={drProxyUserId} onChange={(event) => onDrProxyUserId(event.target.value)} placeholder="예: user-123" /></label>
      <label>위임 수량 (DR)<input inputMode="numeric" value={drVoteQuantity} onChange={(event) => onDrVoteQuantity(event.target.value.replace(/\D/g, ""))} /></label>
      <button className="submit buy-submit" disabled={busy || !drProxyUserId.trim() || !Number(drVoteQuantity)} onClick={() => void onDelegate()}>의결권 위임</button>
      {delegations.filter((item) => item.dr_code === selected.code).map((item) => <div className="holding" key={item.delegation_id}>
        <span><b>{item.proxy_user_id} · {item.dr_quantity} DR · {item.status === "active" ? "위임 중" : "철회됨"}</b><small>위임 {date(item.created_epoch)}{item.revoked_epoch ? ` · 철회 ${date(item.revoked_epoch)}` : ""}</small></span>
        {item.status === "active" && <button className="cancel-button" disabled={busy} onClick={() => void onRevoke(item)}>철회</button>}
      </div>)}
    </section>;
}
