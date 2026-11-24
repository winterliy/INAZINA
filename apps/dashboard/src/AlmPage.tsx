import { tabPanel } from "./tabKeys";
import { useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Card, Tabs, errorText, type Ctx, confirmAction } from "./common";
import { FundingPanel } from "./FundingPanel";
import "./alm.css";

type Status = "ok" | "warning" | "breach" | "n/a";
type Row = { category: string; balance: string; amount: string; rateBps?: number; factorBps?: number };
type Report = {
  issuer: string; jurisdiction: string; currency: string; decimalPlaces: number; asOfEpoch: number;
  balanceSheet: { cashAndReserves: string; grossLoans: string; allowance: string; netLoans: string; totalAssets: string; wholesaleFunding: string; equity: string; fundingGap: string;
    deposits: { retailStable: string; retailLessStable: string; corporate: string; total: string } };
  capital: { cet1: string; at1: string; tier1: string; tier2: string; total: string; riskWeightedAssets: string;
    ratiosPercent: Record<"cet1" | "tier1" | "total", number | null>; requirementsPercent: Record<"cet1" | "tier1" | "total", number>;
    minimumsPercent: Record<"cet1" | "tier1" | "total", number>; buffersPercent: Record<"conservation" | "countercyclical" | "systemic", number>;
    headroomPercent: Record<"cet1" | "tier1" | "total", number | null>; status: Record<"cet1" | "tier1" | "total", Status> };
  leverage: { tier1: string; exposure: string; ratioPercent: number | null; minimumPercent: number; status: Status };
  lcr: { hqla: { total: string }; outflows: Row[]; totalOutflows: string; inflows: { performingLoanPayments30d: string; rateBps: number; grossInflows: string; capBps: number; countedInflows: string };
    netOutflows: string; ratioPercent: number | null; minimumPercent: number; status: Status };
  nsfr: { availableStableFunding: string; requiredStableFunding: string; asf: Row[]; rsf: Row[]; ratioPercent: number | null; minimumPercent: number; status: Status };
  rateShock: { shocks: { basisPoints: number; contractualNii: string; depositBetaNii: string; deltaNii: string; contractualEve: string; coreDepositEve: string; deltaEve: string; deltaEvePercentOfTier1: number | null }[];
    behavioralDeposits: string; coreDeposits: string; assumptions: { depositBetaBps: number; coreDepositShareBps: number; coreDepositYears: number }; method: string };
  ftp: { applyEnabled: boolean; applications: { app_id: number; product_kind: "loan" | "deposit"; product_code: string; action: "apply" | "revert"; old_rate_bps: number; new_rate_bps: number; ftp_bps: number; margin_bps: number; actor: string; reason: string; recorded_epoch: number; reverted: boolean }[];
    liquidityPremiumPercent: string; wholesaleWeightedAverageRatePercent: string | null; applied: boolean; method: string;
    curve: { tenorDays: number; ftpPercent: string }[];
    products: { productCode: string; name: string; kind: "loan" | "deposit"; ratePercent: string; tenorDays: number; tenorBasis: string; ftpPercent: string; marginPercent: string }[] } | null;
  forwardCurves: { method: string; curves: { benchmark: string; zero: { tenorDays: number; ratePercent: string }[];
    forwards: { fromDays: number; toDays: number; forwardPercent: string; negative: boolean }[] }[] } | null;
  parameters: { values: Record<string, number>; overriddenForJurisdiction: string[] };
  method: string;
};
type BankOption = { id: string; issuer: string; nationality: string; status: string };

const STATUS = { ok: ["ok", "충족"], warning: ["warn", "여유 적음"], breach: ["danger", "미달"], "n/a": ["warn", "계산 불가"] } as const;
const CATEGORY: Record<string, string> = {
  retail_stable_deposits: "개인 예금 · 안정(보호 한도 이내)", retail_less_stable_deposits: "개인 예금 · 덜 안정(한도 초과·미보호)", corporate_deposits: "법인 예금",
  equity: "자본", wholesale_1y_plus: "도매조달 · 1년 이상", wholesale_6m_to_1y: "도매조달 · 6개월~1년", wholesale_under_6m: "도매조달 · 6개월 미만",
  cash_and_reserves: "현금 · 지급준비", loans_under_1y: "대출 · 1년 미만", loans_1y_plus: "대출 · 1년 이상", defaulted_loans: "부실 대출",
};
const PARAMETER: Record<string, string> = {
  cet1_min_bps: "보통주자본 최소", tier1_min_bps: "기본자본 최소", total_min_bps: "총자본 최소", conservation_buffer_bps: "자본보전 완충", countercyclical_buffer_bps: "경기대응 완충",
  systemic_buffer_bps: "systemic 완충", leverage_min_bps: "레버리지 최소", lcr_min_bps: "LCR 최소", nsfr_min_bps: "NSFR 최소", t2_general_provision_cap_bps: "Tier 2 일반충당금 한도(RWA 대비)",
  runoff_retail_stable_bps: "유출률 · 개인 안정 예금", runoff_retail_less_stable_bps: "유출률 · 개인 덜 안정", runoff_corporate_bps: "유출률 · 법인 예금",
  runoff_unsecured_wholesale_bps: "유출률 · 무담보 도매", runoff_repo_bps: "유출률 · RP", runoff_central_bank_bps: "유출률 · 중앙은행",
  inflow_performing_loan_bps: "유입률 · 정상 대출 상환", inflow_cap_bps: "유입 한도(유출 대비)",
  asf_equity_bps: "ASF · 자본", asf_retail_stable_bps: "ASF · 개인 안정", asf_retail_less_stable_bps: "ASF · 개인 덜 안정", asf_corporate_bps: "ASF · 법인",
  asf_wholesale_1y_plus_bps: "ASF · 도매 1년 이상", asf_wholesale_6m_to_1y_bps: "ASF · 도매 6개월~1년", asf_wholesale_under_6m_bps: "ASF · 도매 6개월 미만",
  deposit_beta_bps: "금리 충격 · 예금 금리베타", core_deposit_share_bps: "금리 충격 · 핵심예금 비중", core_deposit_years: "금리 충격 · 핵심예금 행동만기(년)",
  ftp_liquidity_premium_bps: "FTP · 유동성 프리미엄", funding_auto_maturity_flag: "도매조달 자동 만기 처리 (옵트인)", ftp_apply_enabled: "FTP 상품 금리 적용 (옵트인)",
  rsf_cash_bps: "RSF · 현금", rsf_loan_under_1y_bps: "RSF · 대출 1년 미만", rsf_loan_1y_plus_bps: "RSF · 대출 1년 이상", rsf_defaulted_loan_bps: "RSF · 부실 대출",
};

const pct = (value: number | null | undefined, digits = 2) => value === null || value === undefined ? "—" : `${value.toFixed(digits)}%`;
const bps = (value: number) => `${(value / 100).toFixed(2)}%`;
const parameterText = (key: string, value: number) => key === "funding_auto_maturity_flag" || key === "ftp_apply_enabled" ? (value === 1 ? "켜짐" : "꺼짐") : key.endsWith("_bps") ? bps(value) : `${value}년`;
const BENCHMARK: Record<string, string> = { funding: "조달금리", bank_bond: "은행채", treasury: "국채" };
const rate2 = (text: string | null) => text === null ? "—" : `${Number(text).toFixed(2)}%`;
const tenorLabel = (days: number) => days < 365 ? `${days}일` : `${(days / 365).toFixed(days % 365 ? 1 : 0)}년`;
const signed = (text: string, money: (text: string) => string) => `${Number(text) > 0 ? "+" : ""}${money(text)}`;

function StatusTag({ status, empty }: { status: Status; empty?: boolean }) {
  const [tone, label] = STATUS[status] ?? STATUS["n/a"];
  return <span className={`tag ${tone}`}>{empty && status === "ok" ? "해당 없음" : label}</span>;
}

/** 자금 · ALM: capital, leverage, LCR and NSFR of a bank, with the jurisdiction's thresholds (read-only except the threshold overrides). */
export function AlmPage({ ctx }: { ctx: Ctx }) {
  const { busy, run, say } = ctx;
  const [banks, setBanks] = useState<BankOption[]>([]);
  const [bankId, setBankId] = useState("");
  const [report, setReport] = useState<Report | null>(null);
  const [edit, setEdit] = useState({ key: "lcr_min_bps", value: "" });
  const [ftpForm, setFtpForm] = useState({ margin: "100", reason: "" });
  const [tab, setTab] = useState<"metrics" | "funding">("metrics");

  const money = (text: string) => {
    const places = report?.decimalPlaces ?? 0;
    return Number(text).toLocaleString("ko-KR", { minimumFractionDigits: places, maximumFractionDigits: places });
  };
  const loadReport = useCallback((id: string) => {
    if (!id) return;
    void run(() => api<Report>(`/api/admin/alm?bank_id=${encodeURIComponent(id)}`), "ALM 지표를 불러오지 못했습니다.", setReport);
  }, [run]);
  useEffect(() => {
    api<{ banks: BankOption[] }>("/api/admin/banks").then((data) => {
      setBanks(data.banks);
      if (data.banks[0]) { setBankId(data.banks[0].id); loadReport(data.banks[0].id); }
    }).catch((cause) => say(errorText(cause, "은행 목록을 불러오지 못했습니다."), true));
  }, []);

  async function saveParameter() {
    const value = Number(edit.value);
    if (!report || !Number.isInteger(value) || value < 0) { say("값은 0 이상의 정수(베이시스포인트, 100 = 1%)로 입력해주세요.", true); return; }
    const autoWarning = edit.key === "funding_auto_maturity_flag" && value === 1
      ? "\n켜면 이 관할 은행의 만기 도래 도매조달이 10분마다 자동 상환되고, 상환 이자가 은행 손익·자본에 반영됩니다." : "";
    if (!await confirmAction(`${report.jurisdiction} 관할의 ‘${PARAMETER[edit.key]}’을(를) ${parameterText(edit.key, value)}(으)로 바꿀까요? 이 관할 모든 은행에 반영됩니다.${autoWarning}`)) return;
    void run(() => api<{ success: boolean }>("/api/admin/compliance", { method: "POST",
      body: JSON.stringify({ action: "prudential_parameter_set", nationality: report.jurisdiction, key: edit.key, valueBps: value }) }),
    "기준을 저장하지 못했습니다.", () => { say("기준을 저장했습니다."); setEdit({ ...edit, value: "" }); loadReport(bankId); });
  }

  async function applyFtp(row: NonNullable<Report["ftp"]>["products"][number]) {
    const margin = Number(ftpForm.margin), reason = ftpForm.reason.trim();
    if (!Number.isInteger(margin) || margin < 0 || margin > 5000 || !reason) { say("마진은 0~5000bp 정수, 사유는 필수입니다.", true); return; }
    const target = Number(row.ftpPercent) + (row.kind === "loan" ? margin : -margin) / 100;
    if (!await confirmAction(`${row.name}(${row.productCode})의 제안 금리를 ${Number(row.ratePercent).toFixed(2)}%에서 약 ${target.toFixed(2)}%(FTP ${row.kind === "loan" ? "+" : "−"} 마진 ${margin}bp)로 바꿀까요? 신규 계약에만 적용되며 기존 계약 금리는 그대로입니다. 적용 이력에 남고 되돌릴 수 있습니다.`)) return;
    void run(() => api<{ success: boolean }>("/api/admin/compliance", { method: "POST",
      body: JSON.stringify({ action: "ftp_rate_apply", bankId, kind: row.kind, productCode: row.productCode, marginBps: margin, reason }) }),
    "FTP 금리를 적용하지 못했습니다.", () => { say("신규 계약 제안 금리에 적용했습니다."); loadReport(bankId); });
  }
  async function revertFtp(appId: number) {
    const reason = ftpForm.reason.trim();
    if (!reason) { say("되돌리는 사유를 입력해주세요.", true); return; }
    if (!await confirmAction(`적용 #${appId}을(를) 되돌려 이전 제안 금리로 복원할까요? 이력에는 되돌림 행이 추가됩니다.`)) return;
    void run(() => api<{ success: boolean }>("/api/admin/compliance", { method: "POST",
      body: JSON.stringify({ action: "ftp_rate_revert", bankId, applicationId: appId, reason }) }),
    "되돌리지 못했습니다.", () => { say("되돌렸습니다."); loadReport(bankId); });
  }

  const gauge = (key: "cet1" | "tier1" | "total", label: string) => {
    const capital = report!.capital, value = capital.ratiosPercent[key], need = capital.requirementsPercent[key];
    const scale = Math.max(need * 2, (value ?? 0) * 1.1, 1);
    return <div className="alm-gauge" key={key}>
      <div className="row"><span>{label}</span><b>{pct(value)}</b></div>
      <div className="alm-track"><i className={capital.status[key] === "ok" ? "" : capital.status[key] === "warning" ? "warn" : "danger"} style={{ width: `${Math.max(0, Math.min(100, ((value ?? 0) / scale) * 100))}%` }} /><s style={{ left: `${(need / scale) * 100}%` }} /></div>
      <div className="row"><span className="cap">필요 {pct(need)} (최소 {pct(capital.minimumsPercent[key])} + 완충 {pct(Object.values(capital.buffersPercent).reduce((a, b) => a + b, 0))})</span><StatusTag status={capital.status[key]} /></div>
    </div>;
  };
  const rows = (items: Row[], rateKey: "rateBps" | "factorBps") => items.map((row) => <tr key={row.category}>
    <td>{CATEGORY[row.category] ?? row.category.replace(/^funding_(.+)_due_30d$/, "도매조달 · $1 · 30일 내 만기")}</td>
    <td className="alm-num">{money(row.balance)}</td><td className="alm-num">{bps(row[rateKey] ?? 0)}</td><td className="alm-num">{money(row.amount)}</td></tr>);

  return <>
    <div className="alm-picker">
      <select aria-label="은행" value={bankId} onChange={(event) => { setBankId(event.target.value); loadReport(event.target.value); }}>
        {banks.map((bank) => <option key={bank.id} value={bank.id}>{bank.issuer} · {bank.id} ({bank.nationality})</option>)}
      </select>
      <button type="button" className="outline" disabled={busy || !bankId} onClick={() => loadReport(bankId)}>다시 계산</button>
      {report && <span className="cap">{report.jurisdiction} 관할 · 기준통화 {report.currency}</span>}
    </div>
    <Tabs group="alm" value={tab} onChange={setTab} label="자금 · ALM 화면" options={[["metrics", "규제 지표"], ["funding", "도매조달"]]} />
    <div className="tab-pass" {...tabPanel("alm", tab)}>
    {tab === "funding" ? (bankId ? <FundingPanel ctx={ctx} bankId={bankId} currency={report?.currency ?? "NWX"} /> : <p className="cap">은행을 고르면 조달 내역을 불러옵니다.</p>)
      : !report ? <p className="cap">은행을 고르면 지표를 계산합니다.</p> : <>
      <div className="alm-two">
        <Card title="자본 비율" caption="위험가중자산 대비. 막대의 세로선이 완충자본을 더한 필요 수준입니다.">
          <div className="alm-gauges">{gauge("cet1", "보통주자본(CET1)")}{gauge("tier1", "기본자본(Tier 1)")}{gauge("total", "총자본")}</div>
          <div className="scroll-x"><table className="alm-small"><tbody>
            <tr><td>CET1</td><td className="alm-num">{money(report.capital.cet1)}</td><td>AT1</td><td className="alm-num">{money(report.capital.at1)}</td></tr>
            <tr><td>Tier 2</td><td className="alm-num">{money(report.capital.tier2)}</td><td>위험가중자산</td><td className="alm-num">{money(report.capital.riskWeightedAssets)}</td></tr>
          </tbody></table></div>
        </Card>
        <Card title="유동성 · 레버리지" caption="LCR(30일 유동성)과 NSFR(1년 안정적 자금), 레버리지 비율">
          <div className="alm-tiles">
            <div><span>LCR</span><b>{pct(report.lcr.ratioPercent, 1)}</b><StatusTag status={report.lcr.status} empty={report.lcr.ratioPercent === null} /></div>
            <div><span>NSFR</span><b>{pct(report.nsfr.ratioPercent, 1)}</b><StatusTag status={report.nsfr.status} empty={report.nsfr.ratioPercent === null} /></div>
            <div><span>레버리지</span><b>{pct(report.leverage.ratioPercent)}</b><StatusTag status={report.leverage.status} /></div>
          </div>
          <p className="cap">최소 기준: LCR {pct(report.lcr.minimumPercent, 0)} · NSFR {pct(report.nsfr.minimumPercent, 0)} · 레버리지 {pct(report.leverage.minimumPercent)}</p>
        </Card>
      </div>

      <Card title="금리 충격 (ΔNII · ΔEVE)" caption={`평행 이동 충격. 가정: 예금 금리베타 ${bps(report.rateShock.assumptions.depositBetaBps)}, 핵심예금 ${bps(report.rateShock.assumptions.coreDepositShareBps)}를 ${report.rateShock.assumptions.coreDepositYears}년 행동만기로 봄 (설명용 가정, 관할 기준에서 조정)`}>
        <div className="scroll-x"><table><thead><tr><th className="alm-num">충격</th><th className="alm-num">ΔNII 계약(대출−변동예금)</th><th className="alm-num">ΔNII 예금 금리베타</th><th className="alm-num">ΔNII 12개월</th><th className="alm-num">ΔEVE 고정금리</th><th className="alm-num">ΔEVE 핵심예금</th><th className="alm-num">ΔEVE</th><th className="alm-num">ΔEVE / Tier 1</th></tr></thead>
          <tbody>{report.rateShock.shocks.map((row) => <tr key={row.basisPoints}><td className="alm-num">{row.basisPoints > 0 ? "+" : ""}{row.basisPoints}bp</td>
            <td className="alm-num">{signed(row.contractualNii, money)}</td><td className="alm-num">{signed(row.depositBetaNii, money)}</td><td className="alm-num"><b>{signed(row.deltaNii, money)}</b></td>
            <td className="alm-num">{signed(row.contractualEve, money)}</td><td className="alm-num">{signed(row.coreDepositEve, money)}</td><td className="alm-num"><b>{signed(row.deltaEve, money)}</b></td>
            <td className="alm-num">{pct(row.deltaEvePercentOfTier1, 1)}</td></tr>)}</tbody></table></div>
        <p className="alm-note">{report.rateShock.method}</p>
      </Card>

      {report.ftp && <Card title="FTP(자금이전가격) 미리보기" caption={`조달 곡선 + 유동성 프리미엄 ${rate2(report.ftp.liquidityPremiumPercent)}. 참고: 도매조달 가중평균 ${rate2(report.ftp.wholesaleWeightedAverageRatePercent)}. 미리보기일 뿐 상품 금리는 바뀌지 않습니다.`}>
        <div className="scroll-x"><table><thead><tr><th>상품</th><th>구분</th><th className="alm-num">상품 금리</th><th className="alm-num">FTP 만기</th><th className="alm-num">FTP</th><th className="alm-num">마진</th>{report.ftp.applyEnabled && <th />}</tr></thead>
          <tbody>{report.ftp.products.length === 0 ? <tr><td colSpan={6} className="cap">이 은행의 상품이 없습니다.</td></tr> : report.ftp.products.map((row) => <tr key={`${row.kind}-${row.productCode}`}>
            <td>{row.name} <span className="cap">{row.productCode}</span></td><td>{row.kind === "loan" ? "대출" : "예금"}</td><td className="alm-num">{rate2(row.ratePercent)}</td>
            <td className="alm-num">{tenorLabel(row.tenorDays)} <span className="cap">({row.tenorBasis === "contract term" ? "계약" : row.tenorBasis === "behavioural maturity assumption" ? "행동만기 가정" : "기본 365일"})</span></td>
            <td className="alm-num">{rate2(row.ftpPercent)}</td><td className="alm-num"><b className={Number(row.marginPercent) < 0 ? "alm-neg" : ""}>{rate2(row.marginPercent)}</b></td>
            {report.ftp?.applyEnabled && <td><button type="button" className="outline" disabled={busy} onClick={() => void applyFtp(row)}>적용</button></td>}</tr>)}</tbody></table></div>
        {report.ftp.applyEnabled ? <div className="alm-edit">
          <input inputMode="numeric" aria-label="목표 마진(bp)" placeholder="목표 마진 bp" value={ftpForm.margin} onChange={(event) => setFtpForm({ ...ftpForm, margin: event.target.value })} />
          <input aria-label="사유" placeholder="사유 (필수)" value={ftpForm.reason} onChange={(event) => setFtpForm({ ...ftpForm, reason: event.target.value })} />
        </div> : <p className="alm-note"><span className="tag warn">적용 꺼짐</span> 관할 기준 ‘FTP 상품 금리 적용’을 켜면 신규 계약 제안 금리에 적용할 수 있습니다.</p>}
        {report.ftp.applications.length > 0 && <div className="scroll-x"><table><thead><tr><th>#</th><th>상품</th><th>동작</th><th className="alm-num">이전 → 이후</th><th>행위자</th><th>사유</th><th /></tr></thead>
          <tbody>{report.ftp.applications.map((a) => <tr key={a.app_id}><td>{a.app_id}</td><td>{a.product_code}</td><td>{a.action === "apply" ? (a.reverted ? "적용(되돌려짐)" : "적용") : "되돌림"}</td>
            <td className="alm-num">{bps(a.old_rate_bps)} → {bps(a.new_rate_bps)}</td><td>{a.actor}</td><td>{a.reason}</td>
            <td>{a.action === "apply" && !a.reverted && <button type="button" className="outline" disabled={busy} onClick={() => void revertFtp(a.app_id)}>되돌리기</button>}</td></tr>)}</tbody></table></div>}
        <p className="alm-note">{report.ftp.method}</p>
      </Card>}

      {report.forwardCurves && <Card title="곡선 · 선도금리" caption="관할의 제로 곡선에서 구간별 내재 선도금리를 계산합니다. 설명용 값이며 거래 가능한 금리가 아닙니다.">
        <div className="scroll-x"><table><thead><tr><th>곡선</th>{report.forwardCurves.curves[0]?.forwards.map((f) => <th key={f.fromDays} className="alm-num">{tenorLabel(f.fromDays)}→{tenorLabel(f.toDays)}</th>)}</tr></thead>
          <tbody>{report.forwardCurves.curves.map((curve) => <tr key={curve.benchmark}><td>{BENCHMARK[curve.benchmark] ?? curve.benchmark}</td>
            {curve.forwards.map((f) => <td key={f.fromDays} className="alm-num">{f.negative && <span className="tag warn">음수</span>} {rate2(f.forwardPercent)}</td>)}</tr>)}</tbody></table></div>
        <p className="alm-note">{report.forwardCurves.method}</p>
      </Card>}

      <Card title="LCR 상세" caption={`현금성 자산(HQLA) ${money(report.lcr.hqla.total)} ÷ 30일 순유출 ${money(report.lcr.netOutflows)}`}>
        <div className="scroll-x"><table><thead><tr><th>유출 항목</th><th className="alm-num">잔액</th><th className="alm-num">유출률</th><th className="alm-num">유출액</th></tr></thead>
          <tbody>{rows(report.lcr.outflows, "rateBps")}
            <tr className="alm-total"><td>총 유출</td><td /><td /><td className="alm-num">{money(report.lcr.totalOutflows)}</td></tr>
            <tr><td>정상 대출 상환 유입 (30일, 유입률 {bps(report.lcr.inflows.rateBps)}, 한도 유출의 {bps(report.lcr.inflows.capBps)})</td><td className="alm-num">{money(report.lcr.inflows.performingLoanPayments30d)}</td><td /><td className="alm-num">−{money(report.lcr.inflows.countedInflows)}</td></tr>
            <tr className="alm-total"><td>순유출</td><td /><td /><td className="alm-num">{money(report.lcr.netOutflows)}</td></tr></tbody></table></div>
      </Card>

      <div className="alm-two">
        <Card title="NSFR · 가용 안정자금(ASF)" caption={`합계 ${money(report.nsfr.availableStableFunding)}`}>
          <div className="scroll-x"><table><thead><tr><th>항목</th><th className="alm-num">잔액</th><th className="alm-num">계수</th><th className="alm-num">ASF</th></tr></thead><tbody>{rows(report.nsfr.asf, "factorBps")}</tbody></table></div>
        </Card>
        <Card title="NSFR · 필요 안정자금(RSF)" caption={`합계 ${money(report.nsfr.requiredStableFunding)} · 대출은 충당금을 뺀 장부가액`}>
          <div className="scroll-x"><table><thead><tr><th>항목</th><th className="alm-num">잔액</th><th className="alm-num">계수</th><th className="alm-num">RSF</th></tr></thead><tbody>{rows(report.nsfr.rsf, "factorBps")}</tbody></table></div>
        </Card>
      </div>

      <div className="alm-two">
        <Card title="대차대조표 모형" caption="이 시뮬레이터에는 은행의 자산 장부가 없어서, 대출 외의 자산은 예금·조달·자본이 마련한 만큼의 현금·지급준비로 봅니다.">
          <div className="scroll-x"><table><tbody>
            <tr><td>현금 · 지급준비 (HQLA 1)</td><td className="alm-num">{money(report.balanceSheet.cashAndReserves)}</td></tr>
            <tr><td>대출 총액</td><td className="alm-num">{money(report.balanceSheet.grossLoans)}</td></tr>
            <tr><td>충당금</td><td className="alm-num">−{money(report.balanceSheet.allowance)}</td></tr>
            <tr className="alm-total"><td>총자산</td><td className="alm-num">{money(report.balanceSheet.totalAssets)}</td></tr>
            <tr><td>예금 · 개인 안정</td><td className="alm-num">{money(report.balanceSheet.deposits.retailStable)}</td></tr>
            <tr><td>예금 · 개인 덜 안정</td><td className="alm-num">{money(report.balanceSheet.deposits.retailLessStable)}</td></tr>
            <tr><td>예금 · 법인</td><td className="alm-num">{money(report.balanceSheet.deposits.corporate)}</td></tr>
            <tr><td>도매조달</td><td className="alm-num">{money(report.balanceSheet.wholesaleFunding)}</td></tr>
            <tr><td>자본</td><td className="alm-num">{money(report.balanceSheet.equity)}</td></tr>
          </tbody></table></div>
          {Number(report.balanceSheet.fundingGap) > 0 && <p className="alm-note"><span className="tag danger">자금 부족</span> 대출이 예금·조달·자본을 합친 것보다 {money(report.balanceSheet.fundingGap)} 많습니다. 현금은 0으로 계산했습니다.</p>}
        </Card>
        <Card title="관할 기준" caption={`${report.jurisdiction} 관할에 적용된 기준입니다. 기본값은 Basel식이며, 바꾼 항목은 이 관할의 모든 은행에 적용됩니다.`}>
          <div className="scroll-x"><table><thead><tr><th>기준</th><th className="alm-num">값</th><th /></tr></thead><tbody>
            {Object.entries(report.parameters.values).map(([key, value]) => <tr key={key}><td>{PARAMETER[key] ?? key}</td><td className="alm-num">{parameterText(key, value)}</td>
              <td>{report.parameters.overriddenForJurisdiction.includes(key) ? <span className="tag warn">변경됨</span> : <span className="cap">기본</span>}</td></tr>)}
          </tbody></table></div>
          <div className="alm-edit">
            <select aria-label="기준 항목" value={edit.key} onChange={(event) => setEdit({ ...edit, key: event.target.value })}>
              {Object.keys(report.parameters.values).map((key) => <option key={key} value={key}>{PARAMETER[key] ?? key}</option>)}
            </select>
            <input inputMode="numeric" aria-label="값(베이시스포인트)" placeholder={edit.key.endsWith("_bps") ? "bp (100 = 1%)" : edit.key === "funding_auto_maturity_flag" || edit.key === "ftp_apply_enabled" ? "0 = 끔, 1 = 켬" : "년(정수)"} value={edit.value} onChange={(event) => setEdit({ ...edit, value: event.target.value })} />
            <button type="button" className="btn primary" disabled={busy || !edit.value} onClick={saveParameter}>기준 저장</button>
          </div>
        </Card>
      </div>
      <p className="alm-note">{report.method}</p>
    </>}
    </div>
  </>;
}
