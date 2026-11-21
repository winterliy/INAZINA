import { useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../../shared/api";
import { Field } from "./common";

type Source = "product" | "stock" | "loan" | "els" | "dividend" | "user" | "country" | "exchange" | "currency";
type Spec = { key: string; label: string; type?: "text" | "number" | "select"; options?: [string, string][]; source?: Source; optional?: boolean; hint?: string; placeholder?: string };
type Product = { code: string; name: string; kind: string; status: string };
type UserHit = { userId: string; nickname: string };
type Lookup = { products: Product[]; countries: string[]; exchanges: string[]; currencies: string[] };

const OP: [string, string][] = [["add", "더하기 (add)"], ["del", "빼기 (del)"], ["modify", "지정값으로 (modify)"]];
const MOOD: [string, string][] = [["bull", "강세 (bull)"], ["flat", "보합 (flat)"], ["bear", "약세 (bear)"]];

/** Every $ administrator command as named fields, in its argument order. */
export const COMMAND_FORMS: Record<string, { group: string; fields: Spec[]; note?: string }> = {
  상품가격수정: { group: "상품", fields: [{ key: "code", label: "종목", source: "product" }, { key: "op", label: "작업", type: "select", options: OP }, { key: "amount", label: "금액", type: "number" }] },
  상품삭제: { group: "상품", fields: [{ key: "code", label: "상품", source: "product" }], note: "거래를 중지합니다. 기록과 보유분은 남습니다." },
  상품분위기: { group: "상품", fields: [{ key: "code", label: "종목", source: "product" }, { key: "mood", label: "분위기", type: "select", options: MOOD }] },
  상품이율수정: { group: "상품", fields: [{ key: "code", label: "대출 상품", source: "loan" }, { key: "base", label: "원이율 %", type: "number" }, { key: "ext", label: "연장이율 %", type: "number", hint: "0이면 원이율 적용" }] },
  소수점거래관리: { group: "상품", fields: [{ key: "code", label: "종목", source: "product" }, { key: "mode", label: "소수점 거래", type: "select", options: [["0", "켜기"], ["1", "끄기"]] }] },
  ELS취소: { group: "상품", fields: [{ key: "code", label: "ELS", source: "els" }], note: "모집 중인 ELS의 청약금과 쿠폰 예치금을 환불합니다." },
  자본변경: { group: "기업 · 배당", fields: [{ key: "dir", label: "방향", type: "select", options: [["증자", "증자"], ["감자", "감자"]] }, { key: "kind", label: "방식", type: "select", options: [["유상", "유상"], ["무상", "무상"]] }, { key: "code", label: "회사", source: "stock" }, { key: "qty", label: "수량", type: "number" }, { key: "price", label: "유상 지급액/발행가", type: "number", optional: true }] },
  증자: { group: "기업 · 배당", fields: [{ key: "kind", label: "방식", type: "select", options: [["유상", "유상"], ["무상", "무상"]] }, { key: "code", label: "회사", source: "stock" }, { key: "qty", label: "수량", type: "number" }, { key: "price", label: "유상 가격", type: "number", optional: true }] },
  액면분할병합: { group: "기업 · 배당", fields: [{ key: "dir", label: "방향", type: "select", options: [["분할", "분할"], ["병합", "병합"]] }, { key: "code", label: "회사", source: "stock" }, { key: "ratio", label: "비율", type: "number" }] },
  정기배당수정: { group: "기업 · 배당", fields: [{ key: "code", label: "종목", source: "dividend" }, { key: "cycle", label: "주기", placeholder: "86400, 1일, 1주, 1달" }, { key: "amount", label: "주당 금액 또는 비율", placeholder: "5 또는 5%" }] },
  정기배당활성: { group: "기업 · 배당", fields: [{ key: "code", label: "종목", source: "dividend" }, { key: "state", label: "정기배당", type: "select", options: [["on", "켜기"], ["off", "끄기"]] }] },
  특별배당실시: { group: "기업 · 배당", fields: [{ key: "code", label: "종목", source: "dividend" }, { key: "amount", label: "주당 배당금", type: "number" }] },
  합병: { group: "기업 · 배당", fields: [{ key: "a", label: "존속 회사 A", source: "stock" }, { key: "b", label: "통합 회사 B", source: "stock" }, { key: "ratio", label: "A:B 교환비율", type: "number", optional: true }] },
  분할: { group: "기업 · 배당", fields: [{ key: "kind", label: "방식", type: "select", options: [["인적", "인적분할 (주주에게 신설회사 주식 배정)"], ["물적", "물적분할 (분할회사가 100% 보유)"]] }, { key: "code", label: "분할 회사", source: "stock" }, { key: "newCode", label: "신설회사 id", placeholder: "예: SELO2" }, { key: "name", label: "신설회사 이름" }, { key: "ratio", label: "분할비율 %", type: "number", hint: "회사 현금·자본금·가치 중 신설회사로 옮길 몫" }, { key: "shares", label: "신설 발행주식수", type: "number", optional: true, hint: "비우면 분할회사와 같은 주식수" }], note: "미체결 주문·옵션·선물·회사채·DR이 연결된 회사는 먼저 정리해야 합니다. 인적분할은 분할회사 주가가 분할비율만큼 내려갑니다." },
  귀화심사: { group: "사용자 · 국가", fields: [{ key: "action", label: "작업", type: "select", options: [["목록", "심사 대기 목록"], ["승인", "승인 (자산 정리 · 되돌릴 수 없음)"], ["거절", "거절"]] }, { key: "id", label: "신청번호", type: "number", optional: true }, { key: "note", label: "사유", optional: true }] },
  KYC심사: { group: "사용자 · 국가", fields: [{ key: "action", label: "작업", type: "select", options: [["목록", "프로필 목록"], ["설정", "상태·위험등급 설정"], ["제한", "제한 (송금·예금 개설·대출 신청 차단)"], ["해제", "제한 해제 (확인 완료·일반)"], ["만료처리", "만료일 지난 프로필 만료 처리"]] }, { key: "user", label: "사용자", source: "user", optional: true }, { key: "status", label: "상태 (설정만)", type: "select", options: [["대기", "검토 대기"], ["확인", "확인 완료"], ["거절", "확인 거절"], ["만료", "만료"]], optional: true }, { key: "tier", label: "위험등급 (설정만)", type: "select", options: [["일반", "일반"], ["강화", "강화"], ["제한", "제한"]], optional: true }, { key: "reason", label: "사유 (설정만)", type: "select", options: [["검토시작", "검토 시작"], ["요건충족", "요건 충족"], ["요건미충족", "요건 미충족"], ["정기검토", "정기 검토"], ["위험상향", "위험 상향"], ["사용자요청", "사용자 요청"]], optional: true }], note: "KYC 프로필이 없는 사용자는 제한 없이 거래합니다. 제한은 송금·예금 개설·대출 신청만 막고 수취는 허용합니다." },
  AML경보: { group: "사용자 · 국가", fields: [{ key: "action", label: "작업", type: "select", options: [["목록", "미종결 경보 목록"], ["검토", "검토 중으로 변경"], ["종결", "종결"], ["규칙", "국적별 감시 규칙 보기·변경"]] }, { key: "target", label: "경보 번호 · 국적", optional: true, hint: "검토·종결은 번호, 규칙은 국적" }, { key: "extra", label: "종결 결과 · 규칙 동작", optional: true, placeholder: "오탐 / 추가조치없음 / 끄기 / 설정", hint: "종결: 추가조치없음·오탐·추가검토·상급검토·제한검토 · 규칙: 끄기 또는 설정" }, { key: "large", label: "단건 기준 (규칙 설정만)", type: "number", optional: true }, { key: "rolling", label: "24시간 누적액 (규칙 설정만)", type: "number", optional: true }, { key: "count", label: "24시간 횟수 (규칙 설정만)", type: "number", optional: true }], note: "경보는 거래를 막지 않고 기록만 남깁니다. 규칙은 기본 비활성입니다." },
  법인잔고수정: { group: "기업 · 배당", fields: [{ key: "code", label: "보통주 법인", source: "stock" }, { key: "op", label: "작업", type: "select", options: OP }, { key: "amount", label: "금액", type: "number" }] },
  법인청산: { group: "기업 · 배당", fields: [{ key: "code", label: "보통주 법인", source: "stock" }], note: "회사채 → 미지급 배당 → 우선주 → 보통주 순으로 배분합니다." },
  잔고수정: { group: "시민 · 잔고", fields: [{ key: "target", label: "사용자 ID 또는 계좌번호", source: "user" }, { key: "op", label: "작업", type: "select", options: OP }, { key: "amount", label: "금액", type: "number", hint: "국적 통화 기준" }] },
  기본소득: { group: "시민 · 잔고", fields: [{ key: "country", label: "국가", source: "country" }, { key: "amount", label: "1인당 금액", type: "number", hint: "그 국가의 화폐 기준" }], note: "해당 국가 국고에서 그 국가 시민에게만 지급합니다." },
  등록: { group: "시민 · 잔고", fields: [{ key: "user", label: "Discord 사용자 ID" }] },
  서버설치: { group: "시민 · 잔고", fields: [{ key: "guild", label: "Discord 서버 ID" }, { key: "country", label: "국적", source: "country" }, { key: "currency", label: "통화", source: "currency" }] },
  국가생성: { group: "국가 · 금리", fields: [{ key: "id", label: "국가 ID (숫자)", type: "number", hint: "국고 계정 ID이자 나루 로그인 ID" }, { key: "name", label: "국가명" }, { key: "currency", label: "통화", source: "currency" }, { key: "base", label: "기준금리 %", type: "number", optional: true }, { key: "max", label: "법정최고이자 %", type: "number", optional: true }, { key: "tax", label: "세율 %", type: "number", optional: true }] },
  국가삭제: { group: "국가 · 금리", fields: [{ key: "country", label: "국가", source: "country" }] },
  금리수정: { group: "국가 · 금리", fields: [{ key: "country", label: "국적", source: "country" }, { key: "op", label: "작업", type: "select", options: OP }, { key: "value", label: "기준금리 %", type: "number" }] },
  법정최고이자수정: { group: "국가 · 금리", fields: [{ key: "country", label: "국적", source: "country" }, { key: "op", label: "작업", type: "select", options: OP }, { key: "value", label: "법정최고이자 %", type: "number" }] },
  시장금리수정: { group: "국가 · 금리", fields: [{ key: "country", label: "국적", source: "country" }, { key: "curve", label: "지표", type: "select", options: [["funding", "조달금리 (funding)"], ["bank_bond", "은행채 (bank_bond)"], ["treasury", "국채 (treasury)"]] }, { key: "days", label: "만기일수", type: "number" }, { key: "rate", label: "금리 %", type: "number" }] },
  세율: { group: "국가 · 금리", fields: [{ key: "country", label: "국적", source: "country", optional: true, hint: "비우면 전체 기본 세율" }, { key: "rate", label: "세율 %", type: "number" }] },
  소득세율: { group: "국가 · 금리", fields: [{ key: "country", label: "국적", source: "country" }, { key: "source", label: "소득원", type: "select", options: [["bond_sale_gain", "채권 매도차익"], ["corporate_bond_interest", "회사채 이자"], ["els_profit", "ELS 수익"], ["foreign_security_sale_gain", "해외증권 매도차익"], ["fractional_sale_gain", "소수점 매도차익"], ["futures_realized_gain", "선물 실현손익"], ["gambling_winnings", "도박 당첨금"], ["liquidation", "법인청산 배분"], ["lottery_winnings", "복권 당첨금"], ["regular_dividend", "정기배당"], ["saving_interest", "예금 이자"], ["special_dividend", "특별배당"], ["stock_sale_gain", "주식 매도차익"], ["treasury_bond_interest", "국채 이자"]] }, { key: "rate", label: "세율 % 또는 기본", placeholder: "15 또는 기본" }], note: "수치를 입력하면 이 소득원 세율을 지정하고, ‘기본’은 예외를 지워 국가 기본 세율로 되돌립니다." },
  환율수정: { group: "시장 · 환율", fields: [{ key: "a", label: "통화 A", source: "currency" }, { key: "b", label: "통화 B", source: "currency" }, { key: "ratio", label: "1 A당 B", type: "number" }] },
  시장분위기: { group: "시장 · 환율", fields: [{ key: "mood", label: "시장 분위기", type: "select", options: MOOD }] },
  휴장: { group: "거래소", fields: [{ key: "exchange", label: "거래소", source: "exchange", optional: true, hint: "비우면 전체" }] },
  개장: { group: "거래소", fields: [{ key: "exchange", label: "거래소", source: "exchange", optional: true, hint: "비우면 전체" }] },
  장일시정지: { group: "거래소", fields: [{ key: "exchange", label: "거래소", source: "exchange", optional: true, hint: "비우면 전체" }] },
  장재개: { group: "거래소", fields: [{ key: "exchange", label: "거래소", source: "exchange", optional: true, hint: "비우면 전체" }] },
  서킷브레이커: { group: "거래소", fields: [{ key: "exchange", label: "거래소", source: "exchange", optional: true, hint: "비우면 전체" }, { key: "stage", label: "단계", type: "select", options: [["1", "1단계 (20분)"], ["2", "2단계 (20분)"], ["3", "3단계 (다음 날까지)"]] }] },
  사이드카: { group: "거래소", fields: [{ key: "exchange", label: "거래소", source: "exchange", optional: true, hint: "비우면 전체" }], note: "5분 동안 신규 거래와 가격 변동을 멈춥니다." },
  도박확률: { group: "도박 · 복권", fields: [{ key: "value", label: "당첨 확률 %", type: "number", hint: "0~100" }] },
  도박리턴값: { group: "도박 · 복권", fields: [{ key: "value", label: "당첨 시 리턴 %", type: "number" }] },
  복권가격설정: { group: "도박 · 복권", fields: [{ key: "price", label: "복권 가격", type: "number" }], note: "다음 회차부터 적용됩니다." },
  복권당첨번호보기: { group: "도박 · 복권", fields: [] },
  복권당첨번호변경: { group: "도박 · 복권", fields: [...[1, 2, 3, 4, 5, 6].map((index): Spec => ({ key: `n${index}`, label: `번호 ${index}`, type: "number", hint: "1~45" })), { key: "bonus", label: "보너스 번호", type: "number", optional: true, hint: "1~45" }], note: "현재 미추첨 회차에 적용됩니다." },
  상품생성: { group: "상품", fields: [], note: "상품 종류별 입력 칸은 종목·상품 / 금융 상품 탭에서 제공합니다." },
  상품수정: { group: "상품", fields: [], note: "상품 종류별 입력 칸은 종목·상품 / 금융 상품 탭에서 제공합니다." },
};

const quote = (value: string) => /^[^\s"'\\]+$/.test(value) ? value : `"${value.replace(/(["\\])/g, "\\$1")}"`;

/** Same order and quoting as typing `$명령어 인자` in Discord; empty optional fields are left out. */
export function buildArguments(fields: Spec[], values: Record<string, string>) {
  return fields.map((field) => (values[field.key] ?? "").trim()).filter((value, index) => value || !fields[index].optional).map(quote).join(" ");
}

const withObject = (label: string) => {
  // The particle follows the word before a trailing "(...)", as in "위험등급 (설정만)".
  const last = label.replace(/\s*\([^)]*\)\s*$/, "").trim().slice(-1).charCodeAt(0);
  if (last >= 0xac00 && last <= 0xd7a3) return `${label}${(last - 0xac00) % 28 ? "을" : "를"}`;
  return `${label}을(를)`;
};

export type SlotGap = { field: Spec; later: Spec; message: string };

/**
 * Arguments are positional. If an optional field is empty while a LATER optional field has a value, the later value
 * would slide into the empty slot and the bot would read it as the wrong argument, so that case is reported here.
 * (A leading optional field followed only by required fields, like 세율's country, is meant to be dropped.)
 */
export function slotGaps(fields: Spec[], values: Record<string, string>): SlotGap[] {
  const filled = (field: Spec) => (values[field.key] ?? "").trim() !== "";
  const gaps: SlotGap[] = [];
  fields.forEach((field, index) => {
    if (!field.optional || filled(field)) return;
    const later = fields.slice(index + 1).find((item) => item.optional && filled(item));
    if (later) gaps.push({ field, later, message: `${withObject(later.label)} 쓰려면 ${field.label}도 입력해야 합니다.` });
  });
  return gaps;
}

// Products, countries and exchanges are the same for every command form: fetch once and share.
const LOOKUP_TTL = 5 * 60_000;
let lookupCache: { at: number; value: Lookup } | null = null;
let lookupRequest: Promise<Lookup> | null = null;
function loadLookup(): Promise<Lookup> {
  if (lookupCache && Date.now() - lookupCache.at < LOOKUP_TTL) return Promise.resolve(lookupCache.value);
  lookupRequest ??= Promise.all([
    api<{ products: Product[] }>("/api/admin/products?group=market"),
    api<{ products: Product[] }>("/api/admin/products?group=finance"),
    api<{ countries: { nationality: string }[] }>("/api/admin/countries"),
    api<{ exchanges: { exchange_id: string }[] }>("/api/admin/exchanges"),
  ]).then(([market, finance, countries, exchanges]) => {
    const value: Lookup = {
      products: [...market.products, ...finance.products],
      countries: countries.countries.map((item) => item.nationality), exchanges: exchanges.exchanges.map((item) => item.exchange_id),
      currencies: finance.products.filter((item) => item.kind === "currency").map((item) => item.code),
    };
    lookupCache = { at: Date.now(), value };
    return value;
  }).finally(() => { lookupRequest = null; });
  return lookupRequest;
}
const EMPTY_LOOKUP: Lookup = { products: [], countries: [], exchanges: [], currencies: [] };

function initialValues(spec: (typeof COMMAND_FORMS)[string] | undefined, prefill: string) {
  const defaults: Record<string, string> = Object.fromEntries((spec?.fields ?? []).filter((field) => field.type === "select" && !field.optional).map((field) => [field.key, field.options![0][0]]));
  // Links from other pages pass the target code; it fills the first free-text field.
  const target = (spec?.fields ?? []).find((field) => field.type !== "select");
  if (prefill.trim() && target) defaults[target.key] = prefill.trim();
  return defaults;
}

export function CommandForm({ command, usage, prefill, busy, onRun, openProducts }: { command: string; usage: string; prefill: string; busy: boolean; onRun: (args: string) => void; openProducts: (group: "market" | "finance") => void }) {
  const spec = COMMAND_FORMS[command];
  const [values, setValues] = useState<Record<string, string>>(() => initialValues(spec, prefill));
  const [raw, setRaw] = useState(!spec);
  const [rawArgs, setRawArgs] = useState(() => spec ? "" : prefill.trim());
  const [lookup, setLookup] = useState<Lookup>(lookupCache?.value ?? EMPTY_LOOKUP);
  const [users, setUsers] = useState<UserHit[]>([]);

  useEffect(() => {
    let cancelled = false;
    loadLookup().then((value) => { if (!cancelled) setLookup(value); }).catch(() => undefined);
    return () => { cancelled = true; };
  }, []);

  // The command changed or another page passed a new target: start the form over.
  useEffect(() => {
    setValues(initialValues(spec, prefill));
    setRawArgs(spec ? "" : prefill.trim());
    setRaw(!spec);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [command, prefill]);

  // Users are searched on the server as the field is typed; the whole user list is never loaded.
  const userField = spec?.fields.find((field) => field.source === "user");
  const userTerm = userField ? (values[userField.key] ?? "").trim() : "";
  useEffect(() => {
    if (!userTerm) { setUsers([]); return; }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      api<{ users: UserHit[] }>(`/api/admin/users?search=${encodeURIComponent(userTerm)}`, { signal: controller.signal })
        .then((result) => { if (!controller.signal.aborted) setUsers(result.users.slice(0, 30)); })
        .catch(() => undefined);
    }, 250);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [userTerm]);

  const options = useMemo(() => ({
    product: lookup.products.filter((item) => !["currency"].includes(item.kind)),
    stock: lookup.products.filter((item) => item.kind === "stock"),
    loan: lookup.products.filter((item) => item.kind === "loan"),
    els: lookup.products.filter((item) => item.kind === "els"),
    dividend: lookup.products.filter((item) => ["stock", "preferred", "asset", "etf", "fund", "indexfund", "commodity"].includes(item.kind)),
  }), [lookup]);

  const fields = spec?.fields ?? [];
  const gaps = useMemo(() => raw ? [] : slotGaps(fields, values), [raw, fields, values]);
  const gapOf = (field: Spec) => gaps.find((gap) => gap.field.key === field.key);
  const args = raw ? rawArgs.trim() : buildArguments(fields, values);
  const invalid = gaps.length > 0;
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (invalid) return;
    onRun(args);
  }

  function input(field: Spec) {
    const value = values[field.key] ?? "";
    const set = (next: string) => setValues({ ...values, [field.key]: next });
    const bad = gapOf(field) ? true : undefined;
    if (field.type === "select") {
      return <select value={value} aria-invalid={bad} onChange={(event) => set(event.target.value)}>{field.optional && <option value="">선택 안 함</option>}{field.options!.map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select>;
    }
    if (field.source === "country" || field.source === "exchange" || field.source === "currency") {
      const list = field.source === "country" ? lookup.countries : field.source === "exchange" ? lookup.exchanges : lookup.currencies;
      return <select value={value} aria-invalid={bad} required={!field.optional} onChange={(event) => set(event.target.value)}><option value="">{field.optional ? (field.source === "exchange" ? "전체 거래소" : "선택 안 함") : "선택하세요"}</option>{list.map((item) => <option key={item} value={item}>{item}</option>)}</select>;
    }
    const listId = field.source ? `command-${field.source}` : undefined;
    return <input className={field.type === "number" || field.source ? "mono" : undefined} list={listId} inputMode={field.type === "number" ? "decimal" : undefined} value={value} aria-invalid={bad} required={!field.optional} placeholder={field.placeholder} onChange={(event) => set(event.target.value)} />;
  }

  return <form onSubmit={submit}>
    {spec?.note && <p className="hint form-note">{spec.note}</p>}
    {(command === "상품생성" || command === "상품수정") && !raw && <div className="actions form-note"><button type="button" className="btn primary small" onClick={() => openProducts("market")}>종목·상품 탭 열기</button><button type="button" className="soft" onClick={() => openProducts("finance")}>금융 상품 탭 열기</button></div>}
    {!raw && spec && fields.length > 0 && <div className="form-grid">{fields.map((field) => <Field key={field.key} label={field.label} error={gapOf(field)?.message} hint={[field.optional ? "선택" : "", field.hint ?? ""].filter(Boolean).join(" · ") || undefined}>{input(field)}</Field>)}</div>}
    {raw && <div className="form-grid"><Field label="인자 직접 입력" wide hint={usage || "인자 없음"}><input className="mono" value={rawArgs} onChange={(event) => setRawArgs(event.target.value)} placeholder={usage || "인자 없음"} /></Field></div>}
    {!raw && fields.length > 1 && <ol className="slot-chips" aria-label="인자 칸 순서">{fields.map((field, index) => {
      const value = (values[field.key] ?? "").trim();
      const gap = gapOf(field);
      return <li key={field.key} className={gap ? "gap" : undefined}><span className="n">{index + 1}</span><span>{field.label}</span>{value ? <span className="v">{value}</span> : <span className="empty-slot">{gap ? "비어 있음 · 채워야 함" : "비어 있음"}</span>}</li>;
    })}</ol>}
    <div className="command-bar">
      <code className="mono preview">${command}{args ? ` ${args}` : ""}</code>
      <label className="check"><input type="checkbox" checked={raw} onChange={(event) => setRaw(event.target.checked)} />직접 입력</label>
      <button className="btn primary" disabled={busy || invalid || ((command === "상품생성" || command === "상품수정") && !raw)} title={invalid ? gaps[0].message : undefined}>{busy ? "실행 중…" : "실행"}</button>
    </div>
    <datalist id="command-product">{options.product.map((item) => <option key={item.code} value={item.code}>{item.name} · {item.kind}{item.status !== "active" ? ` · ${item.status}` : ""}</option>)}</datalist>
    <datalist id="command-stock">{options.stock.map((item) => <option key={item.code} value={item.code}>{item.name}</option>)}</datalist>
    <datalist id="command-loan">{options.loan.map((item) => <option key={item.code} value={item.code}>{item.name}</option>)}</datalist>
    <datalist id="command-els">{options.els.map((item) => <option key={item.code} value={item.code}>{item.name}</option>)}</datalist>
    <datalist id="command-dividend">{options.dividend.map((item) => <option key={item.code} value={item.code}>{item.name}</option>)}</datalist>
    <datalist id="command-user">{users.map((item) => <option key={item.userId} value={item.userId}>{item.nickname}</option>)}</datalist>
  </form>;
}
