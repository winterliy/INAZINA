import { useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { ActionFields, DataView, READ_ONLY, initialValues, number, type Order, type Result, type useCommands } from "../commandKit";

export type Kit = ReturnType<typeof useCommands>;
export type Account = { number: string; product_name: string; balance: number; currency_code: string; decimal_places: number; resolution_status: string };
export type ServiceProps = { kit: Kit; cash: number; homeCurrency: string; homeDigits: number; creditScore: number; accounts: Account[]; orders: Order[] };

export const SERVICES = [
  { path: "/transfer", label: "송금", description: "계좌이체 · 내 계좌 간 이체 · 외화 환전 이체" },
  { path: "/bank", label: "은행", description: "내 통장 · 예금·적금 가입 · 대출" },
  { path: "/invest", label: "투자상품", description: "옵션 · 선물 · 금리스왑 · 메자닌 채권 · 랩어카운트" },
  { path: "/casino", label: "복권 · 게임", description: "로또 6/45 · 럭키 베팅" },
  { path: "/market", label: "시장정보", description: "실시간차트 · 자산 순위 · 거래소 지수 · 금리 · 전체 상품 목록 (/검색)" },
  { path: "/history", label: "거래내역", description: "입출금 · 송금 · 기본소득 · 환전 · 주문 내역" },
  { path: "/financials", label: "기업 재무", description: "회사·우선주의 재무상태표 · 손익 · 현금흐름 · 주요 비율" },
  { path: "/info", label: "정보 · 고객센터", description: "법정 최고이자 · 문의 · 신고 · 파산 신청 · 주주총회" },
] as const;

export const won = (value: number, digits = 0) => number(value, digits);

/** A result banner: the server's message, or its data as a table. */
export function Outcome({ result }: { result: Result | null }) {
  if (!result) return null;
  return <div className={result.success ? "svc-outcome ok" : "svc-outcome failed"} role={result.success ? "status" : "alert"}>
    {result.data !== null && result.data !== undefined ? <DataView data={result.data} /> : <p>{result.message}</p>}
  </div>;
}

/** One action as a self-contained form card, with its result shown under it. */
export function ActionCard({ kit, name, title, description, submit, only, labels, preset, children, autoRun }: {
  kit: Kit; name: string; title: string; description?: ReactNode; submit?: string; only?: string[]; labels?: Record<string, string>;
  preset?: Record<string, string>; children?: ReactNode; autoRun?: boolean;
}) {
  const command = kit.commands.find((item) => item.name === name);
  const [values, setValues] = useState<Record<string, string>>({});
  const [result, setResult] = useState<Result | null>(null);
  useEffect(() => { setValues({ ...initialValues(command), ...preset }); }, [command, JSON.stringify(preset)]);
  // Read-only views with nothing to fill in load on their own.
  // Keyed on the name: the catalog reloads after every action, which must not re-run these views.
  const loaded = Boolean(command);
  useEffect(() => { if (autoRun && command) void kit.execute(command, { ...initialValues(command), ...preset }, false).then(setResult); }, [autoRun, name, loaded]);
  if (!command) return null;
  const readOnly = READ_ONLY.has(name);
  function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void kit.execute(command!, values).then((next) => { if (next) setResult(next); });
  }
  return <form className="svc-card" onSubmit={send}>
    <header><h3>{title}</h3>{description && <p>{description}</p>}</header>
    {children}
    {command.fields.some((field) => !only || only.includes(field.key)) && <ActionFields command={command} values={values} onChange={setValues} sources={kit.sources} only={only} labels={labels} />}
    <button className="svc-submit" disabled={kit.busy}>{kit.busy ? "처리 중…" : submit ?? (readOnly ? "조회" : title)}</button>
    <Outcome result={result} />
  </form>;
}

/** Sub-navigation shared by the service pages. */
export function ServiceNav({ here }: { here: string }) {
  return <nav className="svc-nav" aria-label="서비스">
    {SERVICES.map((item) => <a key={item.path} href={item.path} aria-current={here === item.path ? "page" : undefined}>{item.label}</a>)}
  </nav>;
}
