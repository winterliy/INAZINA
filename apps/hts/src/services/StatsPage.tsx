import { useCallback, useEffect, useState } from "react";
import { api } from "../../../shared/api";
import { RefreshRateButton } from "../RefreshRateButton";
import { formatAmount } from "../notificationFormat";
import type { ServiceProps } from "./ServiceKit";
import "../stats.css";

type Period = "all" | "year" | "30d" | "7d";
type Source = { key: string; label: string; income: string; loss: string; net: string; count: number; gainsOnly: boolean };
type Stats = {
  period: Period; asOf: number; home: string;
  totals: { income: string; loss: string; tax: string; net: string };
  sources: Source[];
  taxes: { key: string; label: string; amount: string; count: number }[];
  unrealized: { total: string; items: { code: string; name: string; currency: string; pnl: string; returnPercent: number }[] };
  trades: { fills: number; buys: number; sells: number; results: number; wins: number; winRate: number | null; avgGain: string | null; avgLoss: string | null; maxGain: string | null; maxLoss: string | null };
  recordingSince: number | null; notRecorded: string[];
};

const PERIODS: [Period, string][] = [["all", "전체"], ["year", "올해"], ["30d", "최근 30일"], ["7d", "최근 7일"]];
const tone = (value: string) => Number(value) > 0 ? "stats-up" : Number(value) < 0 ? "stats-down" : "";
/** "-20" -> "−20", "60" -> "+60" (when a sign is wanted), zero stays plain. */
const signed = (value: string, plus = true) => {
  const n = Number(value);
  return n < 0 ? `−${formatAmount(value.replace("-", ""))}` : n > 0 ? `${plus ? "+" : ""}${formatAmount(value)}` : formatAmount(value);
};
const clock = (epoch: number) => new Date(epoch * 1000).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });

/** 통계: earned, lost and paid in tax, from the records the system keeps, at today's exchange rates. */
export function StatsPage(_: ServiceProps) {
  const [period, setPeriod] = useState<Period>("all");
  const [stats, setStats] = useState<Stats | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async (which: Period) => {
    setBusy(true);
    try {
      setStats(await api<Stats>(`/api/me/stats?period=${which}`));
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "통계를 불러오지 못했습니다.");
    } finally {
      setBusy(false);
    }
  }, []);
  useEffect(() => { void load(period); }, [load, period]);

  if (!stats) return <p className="svc-empty">{error || "불러오는 중…"}</p>;
  const empty = stats.sources.length === 0 && stats.taxes.length === 0;
  const max = Math.max(1, ...stats.taxes.map((item) => Number(item.amount)));
  const t = stats.trades;
  return <div className="stats-page">
    <div className="stats-bar">
      <div className="stats-seg" role="group" aria-label="기간">
        {PERIODS.map(([key, label]) => <button key={key} type="button" aria-pressed={period === key} onClick={() => setPeriod(key)}>{label}</button>)}
      </div>
      <span className="stats-rate">내 통화({stats.home})로 환산 · 실시간 환율 {clock(stats.asOf)} 기준
        <RefreshRateButton busy={busy} onRefresh={() => void load(period)} />
      </span>
    </div>
    {error && <p className="account-error" role="alert">{error}</p>}

    <div className="stats-tiles">
      <div className="stats-tile"><span>순손익</span><b className={tone(stats.totals.net)}>{signed(stats.totals.net)} {stats.home}</b><small>총수익 − 총손실 − 총세액</small></div>
      <div className="stats-tile"><span>총수익</span><b className="stats-up">{signed(stats.totals.income)} {stats.home}</b><small>확정 이익 · 배당 · 이자 · 당첨</small></div>
      <div className="stats-tile"><span>총손실</span><b className={Number(stats.totals.loss) ? "stats-down" : ""}>{Number(stats.totals.loss) ? "−" : ""}{formatAmount(stats.totals.loss)} {stats.home}</b><small>확정 손실 · 대출 이자 · 수수료</small></div>
      <div className="stats-tile"><span>총세액</span><b>{formatAmount(stats.totals.tax)} {stats.home}</b><small>납부한 세금 전체</small></div>
    </div>

    {empty ? <p className="stats-empty">이 기간에 기록된 손익과 세금이 없습니다.</p> : <>
      <section className="stats-panel" aria-labelledby="stats-src">
        <header><h2 id="stats-src">손익 구성</h2><span className="stats-note">어디서 벌고 잃었는지</span></header>
        <div className="stats-scroll"><table className="stats-table">
          <thead><tr><th>구분</th><th>수익</th><th>손실</th><th>순손익</th><th>건수</th></tr></thead>
          <tbody>{stats.sources.map((row) => <tr key={row.key}>
            <td>{row.label}{row.gainsOnly && <span className="stats-chip" title="기록이 시작되기 전에는 이익만 세금 기록으로 복원됩니다">이익만 집계</span>}</td>
            <td className={Number(row.income) ? "stats-up" : ""}>{Number(row.income) ? signed(row.income) : "–"}</td>
            <td className={Number(row.loss) ? "stats-down" : ""}>{Number(row.loss) ? `−${formatAmount(row.loss)}` : "–"}</td>
            <td className={tone(row.net)}>{signed(row.net)}</td>
            <td>{row.count}건</td>
          </tr>)}</tbody>
        </table></div>
      </section>
    </>}

    <div className="stats-two">
      <section className="stats-panel" aria-labelledby="stats-tax">
        <header><h2 id="stats-tax">세액 내역</h2><span className="stats-note">세목별</span></header>
        {stats.taxes.length === 0 ? <p className="stats-note">낸 세금이 없습니다.</p> : <div className="stats-scroll"><table className="stats-table">
          <thead><tr><th>세목</th><th>납부액</th><th>건수</th></tr></thead>
          <tbody>{stats.taxes.map((row) => <tr key={row.key}>
            <td><div>{row.label}</div><div className="stats-meter"><i style={{ width: `${Number(row.amount) / max * 100}%` }} /></div></td>
            <td>{formatAmount(row.amount)} {stats.home}</td><td>{row.count}건</td>
          </tr>)}</tbody>
        </table></div>}
      </section>
      <section className="stats-panel" aria-labelledby="stats-hold">
        <header><h2 id="stats-hold">보유 중 평가손익</h2><span className="stats-note">아직 팔지 않은 손익</span></header>
        <div className="stats-tile" style={{ boxShadow: "none", background: "var(--surface)", marginBottom: 10 }}>
          <span>평가손익 합계</span><b className={tone(stats.unrealized.total)}>{signed(stats.unrealized.total)} {stats.home}</b>
        </div>
        {stats.unrealized.items.length === 0 ? <p className="stats-note">보유 중인 종목이 없습니다.</p> : <div className="stats-scroll"><table className="stats-table">
          <thead><tr><th>종목</th><th>평가손익</th><th>수익률</th></tr></thead>
          <tbody>{stats.unrealized.items.map((row) => <tr key={row.code}>
            <td>{row.name}</td>
            <td className={tone(row.pnl)}>{signed(row.pnl)} {row.currency}</td>
            <td className={tone(String(row.returnPercent))}>{row.returnPercent > 0 ? "+" : row.returnPercent < 0 ? "−" : ""}{Math.abs(row.returnPercent)}%</td>
          </tr>)}</tbody>
        </table></div>}
      </section>
    </div>

    <section className="stats-panel" aria-labelledby="stats-trade">
      <header><h2 id="stats-trade">거래 통계</h2><span className="stats-note">체결 · 확정 손익 기록 기준</span></header>
      <div className="stats-kv">
        <div><span>체결 횟수</span><b>{t.fills}회 (매수 {t.buys} · 매도 {t.sells})</b></div>
        <div><span>수익 거래 비율</span><b>{t.winRate === null ? "–" : `${t.winRate}%`}</b></div>
        <div><span>평균 수익</span><b className="stats-up">{t.avgGain === null ? "–" : `${signed(t.avgGain)} ${stats.home}`}</b></div>
        <div><span>평균 손실</span><b className="stats-down">{t.avgLoss === null ? "–" : `${signed(t.avgLoss)} ${stats.home}`}</b></div>
        <div><span>최대 이익 거래</span><b className="stats-up">{t.maxGain === null ? "–" : `${signed(t.maxGain)} ${stats.home}`}</b></div>
        <div><span>최대 손실 거래</span><b className="stats-down">{t.maxLoss === null ? "–" : `${signed(t.maxLoss)} ${stats.home}`}</b></div>
      </div>
    </section>

    <ul className="stats-defs">
      <li>순손익 = 총수익 − 총손실 − 총세액. 보유 중인 자산의 평가손익은 순손익에 넣지 않고 따로 보여 줍니다.</li>
      <li>총수익은 팔아서 확정된 이익, 배당, 이자, 당첨금입니다. 총손실은 확정된 손실, 대출 이자, 수수료입니다.</li>
      <li>"이익만 집계" 표시: 확정 손익 기록은 {stats.recordingSince ? `${new Date(stats.recordingSince * 1000).toLocaleDateString("ko-KR")}부터` : "아직 시작 전이라"} 쌓이고, 그 전에는 세금 기록에 남은 이익만 복원됩니다.</li>
      {stats.notRecorded.map((note) => <li key={note}>{note}</li>)}
    </ul>
  </div>;
}
