import { useEffect, useRef, useState } from "react";
import type { MutableRefObject } from "react";
import { ApiError, api, type ApiUser } from "../../shared/api";
import { confirmDialog } from "../../shared/dialogs";
import { definitiveRefusal, errorText, money, type DrCustodyInstruction, type DrMovement, type DrVotingAgenda, type DrVotingDelegation, type ReceivedDrVotingDelegation } from "./appShared";
import type { Instrument } from "./types";

type Options = {
  user: ApiUser | null;
  selected: Instrument | null;
  submitLock: MutableRefObject<boolean>;
  setBusy: (busy: boolean) => void;
  setNotice: (text: string) => void;
  notify: (text: string) => void;
  refreshAccount: () => Promise<void>;
  refreshAccountQuiet: () => Promise<void>;
  reloadMarket: () => Promise<void>;
};

/** Depositary-receipt flows: issue/redeem (with a retry-safe reference key), voting delegation and votes. */
export function useDrActions({ user, selected, submitLock, setBusy, setNotice, notify, refreshAccount, refreshAccountQuiet, reloadMarket }: Options) {
  const [drMovements, setDrMovements] = useState<DrMovement[]>([]);
  const [drDelegations, setDrDelegations] = useState<DrVotingDelegation[]>([]);
  const [receivedDrDelegations, setReceivedDrDelegations] = useState<ReceivedDrVotingDelegation[]>([]);
  const [drVotingAgenda, setDrVotingAgenda] = useState<DrVotingAgenda[]>([]);
  const [drQuantity, setDrQuantity] = useState("1");
  const [drVoteQuantity, setDrVoteQuantity] = useState("1");
  const [drProxyUserId, setDrProxyUserId] = useState("");
  const drRequestKeys = useRef(new Map<string, string>());

  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    // DR extras load once; a failure here only leaves those panels empty.
    void api<{ movements: DrMovement[] }>("/api/me/dr/movements")
      .then(({ movements }) => { if (!cancelled) setDrMovements(movements); }).catch(() => undefined);
    void api<{ delegations: DrVotingDelegation[] }>("/api/me/dr/voting-delegations")
      .then(({ delegations }) => { if (!cancelled) setDrDelegations(delegations); }).catch(() => undefined);
    void api<{ delegations: ReceivedDrVotingDelegation[] }>("/api/me/dr/proxy-voting-delegations")
      .then(({ delegations }) => { if (!cancelled) setReceivedDrDelegations(delegations); }).catch(() => undefined);
    void api<{ agenda: DrVotingAgenda[] }>("/api/me/dr/voting-agenda")
      .then(({ agenda }) => { if (!cancelled) setDrVotingAgenda(agenda); }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [user]);

  const reloadDelegations = async () => setDrDelegations((await api<{ delegations: DrVotingDelegation[] }>("/api/me/dr/voting-delegations")).delegations);

  async function convertDr(action: "issue" | "redeem") {
    if (!selected || selected.type !== "depositary_receipt") return;
    const qty = Number(drQuantity);
    if (!Number.isSafeInteger(qty) || qty < 1) { setNotice("DR 수량은 1 이상의 정수여야 합니다."); return; }
    const verb = action === "issue" ? "원주를 예탁해 DR을 발행" : "보유 DR을 원주로 상환";
    if (!await confirmDialog({ title: action === "issue" ? "DR 발행" : "DR 상환", message: `${selected.code} ${qty} DR을 ${verb}할까요?`, danger: action === "redeem" })) return;
    if (submitLock.current) return;
    const requestKey = `${user?.userId ?? "session"}:${selected.code}:${action}:${qty}`;
    const storageKey = `inazina.dr.request.${requestKey}`;
    const forget = () => {
      drRequestKeys.current.delete(requestKey);
      try { window.sessionStorage.removeItem(storageKey); } catch { /* ignore unavailable session storage */ }
    };
    let referenceKey = drRequestKeys.current.get(requestKey);
    if (!referenceKey) {
      try { referenceKey = window.sessionStorage.getItem(storageKey) ?? undefined; } catch { /* session storage may be unavailable */ }
    }
    if (!referenceKey) {
      referenceKey = typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      drRequestKeys.current.set(requestKey, referenceKey);
      try { window.sessionStorage.setItem(storageKey, referenceKey); } catch { /* in-memory retry still works */ }
    }
    submitLock.current = true;
    setBusy(true);
    try {
      const result = await api<{ success: boolean; result: { status: string; message: string } }>("/api/dr/convert", { method: "POST", body: JSON.stringify({
        drCode: selected.code, action, quantity: qty,
        referenceKey,
      }) });
      notify(result.result.message);
      forget();
      try {
        const [, , history] = await Promise.all([refreshAccount(), reloadMarket(), api<{ movements: DrMovement[] }>("/api/me/dr/movements")]);
        setDrMovements(history.movements);
      } catch {
        setNotice(`${result.result.message} 화면 새로고침에 실패했습니다. 다시 불러와 잔액과 이력을 확인해주세요.`);
      }
    } catch (cause) {
      if (definitiveRefusal(cause)) {
        // The server refused this request outright, so the next attempt must be a new one.
        forget();
        if ((cause as ApiError).status !== 401) setNotice(errorText(cause, "DR 전환 요청이 거절되었습니다."));
      } else setNotice(`${errorText(cause, "DR 전환 요청 결과를 확인하지 못했습니다.")} 같은 수량과 방향으로 재시도하면 중복 전환되지 않습니다.`);
    } finally { submitLock.current = false; setBusy(false); }
  }

  async function cancelDrConversion(item: DrCustodyInstruction) {
    if (!await confirmDialog({ title: "DR 전환 취소", message: `${item.reference_key} DR 전환 요청을 취소할까요? 예약된 입력 수량이 해제됩니다.`, danger: true })) return;
    setBusy(true);
    try {
      const result = await api<{ success: boolean; result: { status: string } }>(
        "/api/dr/convert/cancel", { method: "POST", body: JSON.stringify({ referenceKey: item.reference_key }) },
      );
      notify(result.result.status === "cancelled" ? "DR 전환 요청을 취소하고 예약 수량을 해제했습니다." : "취소 처리했습니다.");
      await refreshAccountQuiet();
    } catch (cause) {
      setNotice(errorText(cause, "DR 전환 요청을 취소하지 못했습니다."));
    } finally { setBusy(false); }
  }

  async function delegateDrVotes() {
    if (!selected || selected.type !== "depositary_receipt") return;
    const quantityValue = Number(drVoteQuantity);
    const proxyUserId = drProxyUserId.trim();
    if (!Number.isSafeInteger(quantityValue) || quantityValue < 1 || !proxyUserId) {
      setNotice("대리 사용자 ID와 1 이상의 정수 DR 수량을 입력해주세요.");
      return;
    }
    setBusy(true);
    try {
      await api("/api/dr/voting-delegations", { method: "POST", body: JSON.stringify({
        drCode: selected.code, proxyUserId, quantity: quantityValue,
      }) });
      await reloadDelegations();
      notify(`${selected.code} 의결권 ${quantityValue} DR을 ${proxyUserId}에게 위임했습니다.`);
    } catch (cause) {
      setNotice(errorText(cause, "의결권 위임을 처리하지 못했습니다."));
    } finally { setBusy(false); }
  }

  async function revokeDrVotes(delegation: DrVotingDelegation) {
    if (!await confirmDialog({ title: "의결권 위임 철회", message: `${delegation.dr_code} ${delegation.dr_quantity} DR 의결권 위임을 철회할까요?`, danger: true })) return;
    setBusy(true);
    try {
      await api(`/api/dr/voting-delegations/${delegation.delegation_id}/revoke`, {
        method: "POST", body: JSON.stringify({ delegationId: delegation.delegation_id }),
      });
      await reloadDelegations();
      notify("의결권 위임을 철회했습니다.");
    } catch (cause) {
      setNotice(errorText(cause, "의결권 위임을 철회하지 못했습니다."));
    } finally { setBusy(false); }
  }

  async function castDrVote(item: DrVotingAgenda, choice: string) {
    setBusy(true);
    try {
      await api("/api/dr/votes", { method: "POST", body: JSON.stringify({
        meetingId: item.meetingId, agendaItemId: item.agendaItemId, choice,
      }) });
      const { agenda } = await api<{ agenda: DrVotingAgenda[] }>("/api/me/dr/voting-agenda");
      setDrVotingAgenda(agenda);
      notify(`${item.agendaTitle}: ${choice}로 투표했습니다. 기준일 의결권 ${money(item.votingPower)}표가 반영됩니다.`);
    } catch (cause) {
      setNotice(errorText(cause, "의결권 투표를 처리하지 못했습니다."));
    } finally { setBusy(false); }
  }

  return {
    drMovements, drDelegations, receivedDrDelegations, drVotingAgenda,
    drQuantity, setDrQuantity, drVoteQuantity, setDrVoteQuantity, drProxyUserId, setDrProxyUserId,
    convertDr, cancelDrConversion, delegateDrVotes, revokeDrVotes, castDrVote,
  };
}
