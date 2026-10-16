import { useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../../shared/api";
import { Field } from "./common";

type Source = "product" | "stock" | "loan" | "els" | "dividend" | "user" | "country" | "exchange" | "currency";
type Spec = { key: string; label: string; type?: "text" | "number" | "select"; options?: [string, string][]; source?: Source; optional?: boolean; hint?: string; placeholder?: string };
type Product = { code: string; name: string; kind: string; status: string };
type Lookup = { products: Product[]; users: { userId: string; nickname: string }[]; countries: string[]; exchanges: string[]; currencies: string[] };

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
  자본변경: { group: "기업 · 배당", fields: [{ key: "dir", label: "방향", type: "select", options: [["증자", "증자"], ["감자", "감자"]] }, { key: "kind", label: "방식", type: "select", options: [["유상", "유상"], ["무상", "무상"]] }, { key: "code", label: "회사", source: "stock" }, { key: "qty", label: "수량", type: "number" }, { key: "price", label: "유상 가격", type: "number", optional: true }] },
  증자: { group: "기업 · 배당", fields: [{ key: "kind", label: "방식", type: "select", options: [["유상", "유상"], ["무상", "무상"]] }, { key: "code", label: "회사", source: "stock" }, { key: "qty", label: "수량", type: "number" }, { key: "price", label: "유상 가격", type: "number", optional: true }] },
  액면분할병합: { group: "기업 · 배당", fields: [{ key: "dir", label: "방향", type: "select", options: [["분할", "분할"], ["병합", "병합"]] }, { key: "code", label: "회사", source: "stock" }, { key: "ratio", label: "비율", type: "number" }] },
  정기배당수정: { group: "기업 · 배당", fields: [{ key: "code", label: "종목", source: "dividend" }, { key: "cycle", label: "주기", placeholder: "86400, 1일, 1주, 1달" }, { key: "amount", label: "주당 금액 또는 비율", placeholder: "5 또는 5%" }] },
  정기배당활성: { group: "기업 · 배당", fields: [{ key: "code", label: "종목", source: "dividend" }, { key: "state", label: "정기배당", type: "select", options: [["on", "켜기"], ["off", "끄기"]] }] },
  특별배당실시: { group: "기업 · 배당", fields: [{ key: "code", label: "종목", source: "dividend" }, { key: "amount", label: "주당 배당금", type: "number" }] },
  합병: { group: "기업 · 배당", fields: [{ key: "a", label: "존속 회사 A", source: "stock" }, { key: "b", label: "통합 회사 B", source: "stock" }, { key: "ratio", label: "A:B 교환비율", type: "number", optional: true }] },
  법인잔고수정: { group: "기업 · 배당", fields: [{ key: "code", label: "보통주 법인", source: "stock" }, { key: "op", label: "작업", type: "select", options: OP }, { key: "amount", label: "금액", type: "number" }] },
  법인청산: { group: "기업 · 배당", fields: [{ key: "code", label: "보통주 법인", source: "stock" }], note: "회사채 → 미지급 배당 → 우선주 → 보통주 순으로 배분합니다." },
  잔고수정: { group: "시민 · 잔고", fields: [{ key: "target", label: "사용자 ID 또는 계좌번호", source: "user" }, { key: "op", label: "작업", type: "select", options: OP }, { key: "amount", label: "금액", type: "number", hint: "국적 통화 기준" }] },
  기본소득: { group: "시민 · 잔고", fields: [{ key: "country", label: "국가", source: "country" }, { key: "amount", label: "1인당 금액", type: "number", hint: "그 국가의 화폐 기준" }], note: "해당 국가 국고에서 그 국가 시민에게만 지급합니다." },
  등록: { group: "시민 · 잔고", fields: [{ key: "user", label: "Discord 사용자 ID" }] },
  서버설치: { group: "시민 · 잔고", fields: [{ key: "guild", label: "Discord 서버 ID" }, { key: "country", label: "국적", source: "country" }, { key: "currency", label: "통화", source: "currency" }] },
  국가생성: { group: "국가 · 금리", fields: [{ key: "name", label: "국가명" }, { key: "currency", label: "통화", source: "currency" }, { key: "base", label: "기준금리 %", type: "number", optional: true }, { key: "max", label: "법정최고이자 %", type: "number", optional: true }, { key: "tax", label: "세율 %", type: "number", optional: true }] },
  국가삭제: { group: "국가 · 금리", fields: [{ key: "country", label: "국가", source: "country" }] },
  금리수정: { group: "국가 · 금리", fields: [{ key: "country", label: "국적", source: "country" }, { key: "op", label: "작업", type: "select", options: OP }, { key: "value", label: "기준금리 %", type: "number" }] },
  법정최고이자수정: { group: "국가 · 금리", fields: [{ key: "country", label: "국적", source: "country" }, { key: "op", label: "작업", type: "select", options: OP }, { key: "value", label: "법정최고이자 %", type: "number" }] },
  시장금리수정: { group: "국가 · 금리", fields: [{ key: "country", label: "국적", source: "country" }, { key: "curve", label: "지표", type: "select", options: [["funding", "조달금리 (funding)"], ["bank_bond", "은행채 (bank_bond)"], ["treasury", "국채 (treasury)"]] }, { key: "days", label: "만기일수", type: "number" }, { key: "rate", label: "금리 %", type: "number" }] },
  세율: { group: "국가 · 금리", fields: [{ key: "country", label: "국적", source: "country", optional: true, hint: "비우면 전체 기본 세율" }, { key: "rate", label: "세율 %", type: "number" }] },
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

export function CommandForm({ command, usage, prefill, busy, onRun, openProducts }: { command: string; usage: string; prefill: string; busy: boolean; onRun: (args: string) => void; openProducts: (group: "market" | "finance") => void }) {
  const spec = COMMAND_FORMS[command];
  const [values, setValues] = useState<Record<string, string>>({});
  const [raw, setRaw] = useState(false);
  const [rawArgs, setRawArgs] = useState("");
  const [lookup, setLookup] = useState<Lookup>({ products: [], users: [], countries: [], exchanges: [], currencies: [] });

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      api<{ products: Product[] }>("/api/admin/products?group=market"),
      api<{ products: Product[] }>("/api/admin/products?group=finance"),
      api<{ users: { userId: string; nickname: string }[] }>("/api/admin/users?search="),
      api<{ countries: { nationality: string }[] }>("/api/admin/countries"),
      api<{ exchanges: { exchange_id: string }[] }>("/api/admin/exchanges"),
    ]).then(([market, finance, users, countries, exchanges]) => {
      if (cancelled) return;
      setLookup({
        products: [...market.products, ...finance.products], users: users.users,
        countries: countries.countries.map((item) => item.nationality), exchanges: exchanges.exchanges.map((item) => item.exchange_id),
        currencies: finance.products.filter((item) => item.kind === "currency").map((item) => item.code),
      });
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    const defaults: Record<string, string> = Object.fromEntries((spec?.fields ?? []).filter((field) => field.type === "select" && !field.optional).map((field) => [field.key, field.options![0][0]]));
    // Links from other pages pass the target code; it fills the first free-text field.
    const target = (spec?.fields ?? []).find((field) => field.type !== "select");
    if (prefill.trim() && target) defaults[target.key] = prefill.trim();
    setValues(defaults);
    setRawArgs(spec ? "" : prefill.trim());
    setRaw(!spec);
  }, [command, prefill]);

  const options = useMemo(() => ({
    product: lookup.products.filter((item) => !["currency"].includes(item.kind)),
    stock: lookup.products.filter((item) => item.kind === "stock"),
    loan: lookup.products.filter((item) => item.kind === "loan"),
    els: lookup.products.filter((item) => item.kind === "els"),
    dividend: lookup.products.filter((item) => ["stock", "preferred", "asset", "etf", "fund", "indexfund", "commodity"].includes(item.kind)),
  }), [lookup]);

  const args = raw ? rawArgs.trim() : buildArguments(spec?.fields ?? [], values);
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onRun(args);
  }

  function input(field: Spec) {
    const value = values[field.key] ?? "";
    const set = (next: string) => setValues({ ...values, [field.key]: next });
    if (field.type === "select") {
      return <select value={value} onChange={(event) => set(event.target.value)}>{field.optional && <option value="">선택 안 함</option>}{field.options!.map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select>;
    }
    if (field.source === "country" || field.source === "exchange" || field.source === "currency") {
      const list = field.source === "country" ? lookup.countries : field.source === "exchange" ? lookup.exchanges : lookup.currencies;
      return <select value={value} required={!field.optional} onChange={(event) => set(event.target.value)}><option value="">{field.optional ? (field.source === "exchange" ? "전체 거래소" : "선택 안 함") : "선택하세요"}</option>{list.map((item) => <option key={item} value={item}>{item}</option>)}</select>;
    }
    const listId = field.source ? `command-${field.source}` : undefined;
    return <input className={field.type === "number" || field.source ? "mono" : undefined} list={listId} inputMode={field.type === "number" ? "decimal" : undefined} value={value} required={!field.optional} placeholder={field.placeholder} onChange={(event) => set(event.target.value)} />;
  }

  return <form onSubmit={submit}>
    {spec?.note && <p className="hint form-note">{spec.note}</p>}
    {(command === "상품생성" || command === "상품수정") && !raw && <div className="actions form-note"><button type="button" className="btn primary small" onClick={() => openProducts("market")}>종목·상품 탭 열기</button><button type="button" className="soft" onClick={() => openProducts("finance")}>금융 상품 탭 열기</button></div>}
    {!raw && spec && spec.fields.length > 0 && <div className="form-grid">{spec.fields.map((field) => <Field key={field.key} label={field.label} hint={[field.optional ? "선택" : "", field.hint ?? ""].filter(Boolean).join(" · ") || undefined}>{input(field)}</Field>)}</div>}
    {raw && <div className="form-grid"><Field label="인자 직접 입력" wide hint={usage || "인자 없음"}><input className="mono" value={rawArgs} onChange={(event) => setRawArgs(event.target.value)} placeholder={usage || "인자 없음"} /></Field></div>}
    <div className="command-bar">
      <code className="mono preview">${command}{args ? ` ${args}` : ""}</code>
      <label className="check"><input type="checkbox" checked={raw} onChange={(event) => setRaw(event.target.checked)} />직접 입력</label>
      <button className="btn primary" disabled={busy || ((command === "상품생성" || command === "상품수정") && !raw)}>{busy ? "실행 중…" : "실행"}</button>
    </div>
    <datalist id="command-product">{options.product.map((item) => <option key={item.code} value={item.code}>{item.name} · {item.kind}{item.status !== "active" ? ` · ${item.status}` : ""}</option>)}</datalist>
    <datalist id="command-stock">{options.stock.map((item) => <option key={item.code} value={item.code}>{item.name}</option>)}</datalist>
    <datalist id="command-loan">{options.loan.map((item) => <option key={item.code} value={item.code}>{item.name}</option>)}</datalist>
    <datalist id="command-els">{options.els.map((item) => <option key={item.code} value={item.code}>{item.name}</option>)}</datalist>
    <datalist id="command-dividend">{options.dividend.map((item) => <option key={item.code} value={item.code}>{item.name}</option>)}</datalist>
    <datalist id="command-user">{lookup.users.map((item) => <option key={item.userId} value={item.userId}>{item.nickname}</option>)}</datalist>
  </form>;
}
