import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../../shared/api";
import { Card, Field, changed, number, plain, show, text, type Ctx } from "./common";

type KindField = { key: string; label: string; optional: boolean; text: boolean };
type AdvancedField = { key: string; label: string; type: string };
type Kind = { kind: string; token: string; label: string; group: "market" | "finance"; fields: KindField[]; advanced: AdvancedField[]; example: Record<string, unknown>; editable: boolean };
type Product = { code: string; name: string; kind: string; status: string; exchangeId: string | null; currency: string | null; value: number | null };
type Detail = { code: string; kind: string; status: string; values: Record<string, unknown>; row: Record<string, unknown> };
type Editor = { mode: "create" | "edit"; kind: string; code: string; values: Record<string, string>; advanced: Record<string, string>; original: Detail | null };

// Corporate actions and price changes stay commands; the list links to them.
const RELATED: Record<string, [string, string][]> = {
  stock: [["상품가격수정", "가격"], ["자본변경", "증자·감자"], ["액면분할병합", "분할·병합"], ["정기배당수정", "배당"], ["정기배당활성", "배당 켜기/끄기"], ["특별배당실시", "특별배당"], ["합병", "합병"], ["법인잔고수정", "법인 현금"], ["법인청산", "청산"], ["상품분위기", "분위기"], ["소수점거래관리", "소수점"]],
  preferred: [["상품가격수정", "가격"], ["정기배당수정", "배당"], ["정기배당활성", "배당 켜기/끄기"], ["특별배당실시", "특별배당"]],
  asset: [["상품가격수정", "가격"], ["정기배당수정", "배당"], ["상품분위기", "분위기"], ["소수점거래관리", "소수점"]],
  etf: [["상품가격수정", "가격"], ["소수점거래관리", "소수점"]],
  fund: [["상품가격수정", "가격"], ["정기배당수정", "배당"], ["소수점거래관리", "소수점"]],
  indexfund: [["상품가격수정", "가격"], ["소수점거래관리", "소수점"]],
  commodity: [["상품가격수정", "가격"], ["상품분위기", "분위기"], ["소수점거래관리", "소수점"]],
  future: [["상품가격수정", "가격"]],
  corporatebond: [["상품가격수정", "가격"]],
  bond: [["상품가격수정", "가격"]],
  option: [["상품가격수정", "프리미엄"]],
  els: [["ELS취소", "모집 취소"]],
  loan: [["상품이율수정", "이율"]],
  currency: [["환율수정", "환율"]],
};

export function ProductsPage({ group, ctx }: { group: "market" | "finance"; ctx: Ctx }) {
  const { busy, run, say, query, openCommand } = ctx;
  const [kinds, setKinds] = useState<Kind[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [filter, setFilter] = useState("all");
  const [editor, setEditor] = useState<Editor | null>(null);
  const [showInactive, setShowInactive] = useState(true);

  const load = useCallback(async () => {
    const [catalog, list] = await Promise.all([
      api<{ kinds: Kind[] }>("/api/admin/products/catalog"),
      api<{ products: Product[] }>(`/api/admin/products?group=${group}`),
    ]);
    setKinds(catalog.kinds.filter((item) => item.group === group));
    setProducts(list.products);
  }, [group]);

  useEffect(() => {
    setEditor(null);
    setFilter("all");
    load().catch((cause) => say(cause instanceof Error ? cause.message : "상품을 불러오지 못했습니다.", true));
  }, [load]);

  const kindOf = (kind: string) => kinds.find((item) => item.kind === kind);
  const visible = useMemo(() => {
    const term = query.trim().toLocaleLowerCase();
    return products.filter((item) => (filter === "all" || item.kind === filter) && (showInactive || item.status === "active") &&
      (!term || `${item.code} ${item.name} ${kindOf(item.kind)?.label ?? ""} ${item.exchangeId ?? ""}`.toLocaleLowerCase().includes(term)));
  }, [products, filter, query, showInactive, kinds]);

  function startCreate(kind: string) {
    const spec = kindOf(kind);
    if (!spec) return;
    setEditor({ mode: "create", kind, code: "", original: null,
      values: Object.fromEntries(spec.fields.map((field) => [field.key, text(spec.example[field.key])])), advanced: {} });
  }

  async function startEdit(item: Product) {
    await run(() => api<Detail>(`/api/admin/products/${encodeURIComponent(item.code)}`), "상품 정보를 불러오지 못했습니다.", (detail) => {
      const spec = kindOf(detail.kind);
      setEditor({ mode: "edit", kind: detail.kind, code: detail.code, original: detail,
        values: Object.fromEntries((spec?.fields ?? []).map((field) => [field.key, text(detail.values[field.key])])),
        advanced: Object.fromEntries((spec?.advanced ?? []).map((field) => [field.key, text(detail.row[field.key])])) });
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  }

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editor) return;
    const spec = kindOf(editor.kind)!;
    const fields = editor.mode === "edit" ? changed(editor.values, editor.original?.values ?? {}) : editor.values;
    const advanced = editor.mode === "edit" ? changed(editor.advanced, editor.original?.row ?? {}) : Object.fromEntries(Object.entries(editor.advanced).filter(([, value]) => value !== ""));
    if (editor.mode === "edit" && !Object.keys(fields).length && !Object.keys(advanced).length) {
      say("변경된 값이 없습니다.", true);
      return;
    }
    const summary = editor.mode === "edit"
      ? [...Object.entries(fields).map(([key, value]) => `${spec.fields.find((field) => field.key === key)?.label ?? key}: ${show(editor.original?.values[key])} → ${show(value)}`),
         ...Object.entries(advanced).map(([key, value]) => `${key}: ${show(editor.original?.row[key])} → ${show(value)}`)].join("\n")
      : `${spec.label} ${editor.code}`;
    if (!window.confirm(`${editor.mode === "create" ? "상품을 생성할까요?" : `${editor.code} 상품을 수정할까요?`}\n\n${summary}`)) return;
    void run(() => api<{ message: string; success: boolean }>("/api/admin/products", {
      method: "POST", body: JSON.stringify({ operation: editor.mode, kind: editor.kind, code: editor.code.trim(), fields, advanced }),
    }), "상품을 저장하지 못했습니다.", async (result) => {
      say(plain(result.message), !result.success);
      if (result.success) { setEditor(null); await load(); }
    });
  }

  function toggle(item: Product) {
    const active = item.status !== "active";
    const label = item.kind === "els" && !active ? "모집 취소(청약금 환불)" : active ? "재개" : "거래 중지";
    if (!window.confirm(`${item.name} (${item.code})을 ${label}할까요?`)) return;
    void run(() => api<{ message: string; success: boolean }>(`/api/admin/products/${encodeURIComponent(item.code)}/status`, { method: "POST", body: JSON.stringify({ active }) }),
      "상태를 바꾸지 못했습니다.", async (result) => { say(plain(result.message), !result.success); await load(); });
  }

  const spec = editor ? kindOf(editor.kind) : undefined;
  const counts = Object.fromEntries(kinds.map((kind) => [kind.kind, products.filter((item) => item.kind === kind.kind).length]));

  return <>
    <Card title="새 상품" caption={`${kinds.map((kind) => kind.label).join(" · ")} — 종류를 고르면 $상품생성과 같은 입력 칸이 나옵니다.`}>
      <div className="kind-grid">{kinds.map((kind) => <button key={kind.kind} type="button" className={editor?.mode === "create" && editor.kind === kind.kind ? "command-chip active" : "command-chip"} onClick={() => startCreate(kind.kind)}><b>{kind.label}</b><small>{kind.token} · {counts[kind.kind] ?? 0}개</small></button>)}</div>
    </Card>

    {editor && spec && <Card title={editor.mode === "create" ? `${spec.label} 생성` : `${editor.code} 수정`} caption={editor.mode === "create" ? "선택 항목은 비워 둘 수 있습니다. 배당주기는 초 또는 1일·1주·1달, 배당은 5 또는 5%처럼 입력합니다." : "바꾼 값만 저장합니다. 결제 통화와 발행된 계약 조건처럼 바꿀 수 없는 값은 서버가 거부합니다."}
      actions={<button className="soft" type="button" onClick={() => setEditor(null)}>닫기</button>}>
      <form className="form-grid" onSubmit={save}>
        <Field label="상품 코드" hint={editor.mode === "edit" ? "변경 불가" : "공백 없이"}><input className="mono" value={editor.code} readOnly={editor.mode === "edit"} required={editor.kind !== "option"} onChange={(event) => setEditor({ ...editor, code: event.target.value })} /></Field>
        {spec.fields.map((field) => <Field key={field.key} label={field.label} hint={`${field.key}${field.optional ? " · 선택" : ""}`} wide={field.key === "components" || field.key === "underlyings"}>
          <input className={field.text ? undefined : "mono"} value={editor.values[field.key] ?? ""} onChange={(event) => setEditor({ ...editor, values: { ...editor.values, [field.key]: event.target.value } })} />
        </Field>)}
        <button className="btn primary" disabled={busy}>{busy ? "저장 중…" : editor.mode === "create" ? "생성" : "저장"}</button>
      </form>
      {spec.advanced.length > 0 && editor.kind !== "els" && <details className="help advanced"><summary>고급 설정 ({spec.advanced.length}개 열)</summary>
        <p className="hint">{editor.mode === "create" ? "필요한 항목만 입력하세요. 비우면 기본값을 씁니다." : "현재 값입니다. 바꾼 칸만 저장합니다."}</p>
        <div className="form-grid">{spec.advanced.map((field) => <Field key={field.key} label={field.label} hint={`${field.key} · ${field.type || "ANY"}`}>
          <input className="mono" value={editor.advanced[field.key] ?? ""} onChange={(event) => setEditor({ ...editor, advanced: { ...editor.advanced, [field.key]: event.target.value } })} />
        </Field>)}</div>
      </details>}
    </Card>}

    <Card title={group === "market" ? "종목 · 상품" : "금융 상품"} caption={`${number(visible.length)}개 표시 · 중지는 기록을 지우지 않고 거래만 멈춥니다.`}
      actions={<><select aria-label="종류" value={filter} onChange={(event) => setFilter(event.target.value)}><option value="all">전체 종류</option>{kinds.map((kind) => <option key={kind.kind} value={kind.kind}>{kind.label} ({counts[kind.kind] ?? 0})</option>)}</select>
        <label className="check"><input type="checkbox" checked={showInactive} onChange={(event) => setShowInactive(event.target.checked)} />중지 포함</label></>}>
      <div className="scroll-x tall"><table><thead><tr><th>코드</th><th>이름</th><th>종류</th><th>거래소</th><th>통화</th><th className="r">{group === "market" ? "가격" : "이율·환율"}</th><th>상태</th><th /></tr></thead><tbody>
        {visible.map((item) => <tr key={`${item.kind}-${item.code}`}>
          <td className="mono">{item.code}</td><td>{item.name}</td><td>{kindOf(item.kind)?.label ?? item.kind}</td><td>{show(item.exchangeId)}</td><td>{show(item.currency)}</td>
          <td className="r mono">{item.value === null ? "—" : number(item.value)}</td>
          <td><span className={item.status === "active" ? "tag ok" : "tag warn"}>{item.status}</span></td>
          <td className="row-actions">
            {(RELATED[item.kind] ?? []).length > 0 && <select className="mini-select" aria-label="관련 명령어" value="" onChange={(event) => { if (event.target.value) openCommand(event.target.value, `${item.code} `); }}><option value="">명령어…</option>{RELATED[item.kind].map(([command, label]) => <option key={command} value={command}>{label} (${command})</option>)}</select>}
            {kindOf(item.kind)?.editable !== false && <button className="soft small" disabled={busy} onClick={() => void startEdit(item)}>수정</button>}
            {!["matured", "settled", "cancelled", "expired"].includes(item.status) && <button className="soft small" disabled={busy} onClick={() => toggle(item)}>{item.status === "active" ? (item.kind === "els" ? "취소" : "중지") : "재개"}</button>}
          </td>
        </tr>)}
      </tbody></table>{!visible.length && <p className="empty">표시할 상품이 없습니다.</p>}</div>
    </Card>
  </>;
}
