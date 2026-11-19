import "./credit.css";

export type CreditFactor = { key: string; label: string; points: number };
export type CreditEvent = { type: string; reference: string; loanId: number | null; occurredEpoch: number; reason: string | null };
export type CreditReport = { score: number; anchor: number; factors: CreditFactor[]; events: CreditEvent[]; history: { previous: number; score: number; trigger: string; epoch: number }[] };

const EVENTS: Record<string, string> = {
  payment_on_time: "정상 납부", payment_missed: "납부 연체", payment_cured: "연체 회차 사후 납부", loan_closed: "대출 완납",
  acceleration: "기한이익상실", default: "부도", manual_adjustment: "관리자 정정", external_adjustment: "점수 정정 반영", legacy_baseline: "신용모델 도입",
  recompute: "점수 재산정", periodic: "정기 재산정",
};
const TIPS: Record<string, string> = {
  unpaid_installments: "미납 회차를 갚으면 현재 미납 감점이 바로 사라집니다.",
  missed: "연체 기록은 시간이 지나며 줄어들고, 미납 회차를 갚으면 일부 회복됩니다.",
  delinquent_loans: "연체 중인 대출 잔액을 상환하면 연체 상태 감점이 사라집니다.",
  acceleration: "기한이익상실 기록은 시간이 지나며 줄어듭니다. 정상 납부 실적이 회복을 돕습니다.",
  default: "부도 기록은 시간이 지나며 줄어듭니다. 남은 잔액 상환과 정상 납부가 회복을 돕습니다.",
  debt_burden: "대출 잔액을 줄이거나 연소득 정보를 갱신하면 부채수준 요인이 개선됩니다.",
  new_credit: "짧은 기간에 대출을 여러 건 받지 않으면 5일 뒤 회복됩니다.",
  legacy: "도입 전 감점은 시간이 지나며 줄어듭니다.",
};
// Bands on the 0-1000 scale, the way credit bureaus label a score range.
const BANDS = [
  { min: 900, label: "매우 우수", tone: "top" }, { min: 800, label: "우수", tone: "high" }, { min: 700, label: "양호", tone: "good" },
  { min: 600, label: "보통", tone: "mid" }, { min: 500, label: "주의", tone: "warn" }, { min: 0, label: "위험", tone: "low" },
];
export const bandOf = (score: number) => BANDS.find((band) => score >= band.min) ?? BANDS[BANDS.length - 1];

const signed = (value: number) => `${value > 0 ? "+" : ""}${Math.round(value * 10) / 10}`;
const day = (epoch: number) => Number.isFinite(epoch * 1000) ? new Intl.DateTimeFormat("ko-KR", { dateStyle: "medium", timeStyle: "short" }).format(epoch * 1000) : "—";

function Gauge({ score, anchor }: { score: number; anchor: number }) {
  const r = 120, cx = 150, cy = 150, arc = Math.PI * r;
  const at = (value: number) => { const a = Math.PI * (1 - Math.min(1000, Math.max(0, value)) / 1000); return [cx + r * Math.cos(a), cy - r * Math.sin(a)]; };
  const [mx, my] = at(anchor);
  return <svg className="credit-gauge" viewBox="0 0 300 175" role="img" aria-label={`신용점수 ${score}점`}>
    <path d={`M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${cx + r} ${cy}`} className="gauge-track" />
    <path d={`M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${cx + r} ${cy}`} className="gauge-fill" strokeDasharray={`${arc * Math.min(1, score / 1000)} ${arc}`} />
    <circle cx={mx} cy={my} r="4" className="gauge-anchor"><title>{`기준점수 ${anchor}점`}</title></circle>
    <text x={cx} y={cy - 22} textAnchor="middle" className="gauge-score">{score}</text>
    <text x={cx} y={cy + 2} textAnchor="middle" className="gauge-max">/ 1000</text>
    <text x={cx - r} y={cy + 20} textAnchor="middle" className="gauge-edge">0</text>
    <text x={cx + r} y={cy + 20} textAnchor="middle" className="gauge-edge">1000</text>
  </svg>;
}

function Trend({ history }: { history: CreditReport["history"] }) {
  const rows = [...history].sort((a, b) => a.epoch - b.epoch).slice(-30);
  if (rows.length < 2) return <p className="credit-empty">점수 변동 기록이 쌓이면 추이가 표시됩니다.</p>;
  const w = 640, h = 180, pad = 28, values = rows.map((row) => row.score), lo = Math.min(...values), hi = Math.max(...values), span = Math.max(1, hi - lo);
  const x = (i: number) => pad + (w - pad * 2) * i / (rows.length - 1), y = (v: number) => h - pad - (h - pad * 2) * (v - lo) / span;
  const line = rows.map((row, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(row.score).toFixed(1)}`).join(" ");
  return <svg className="credit-trend" viewBox={`0 0 ${w} ${h}`} role="img" aria-label="신용점수 변동 추이" preserveAspectRatio="none">
    <line x1={pad} x2={w - pad} y1={h - pad} y2={h - pad} className="trend-axis" />
    <path d={line} className="trend-line" />
    {rows.map((row, i) => <circle key={i} cx={x(i)} cy={y(row.score)} r="3" className="trend-dot"><title>{`${day(row.epoch)} · ${row.score}점`}</title></circle>)}
    <text x={pad} y={14} className="trend-label">{hi}</text><text x={pad} y={h - 8} className="trend-label">{lo}</text>
  </svg>;
}

export function CreditPage({ credit }: { credit: CreditReport }) {
  const band = bandOf(credit.score), diff = credit.score - credit.anchor;
  const events = credit.events.filter((event) => event.type !== "legacy_baseline");
  const negatives = credit.factors.filter((factor) => factor.points < 0 && TIPS[factor.key]);
  const reach = Math.max(1, ...credit.factors.map((factor) => Math.abs(factor.points)));
  return <div className="credit-page">
    <header className="credit-head"><p className="eyebrow">NARU · 나루</p><h1>신용점수</h1></header>
    <section className="credit-summary" aria-label="내 신용점수">
      <Gauge score={credit.score} anchor={credit.anchor} />
      <div className="credit-summary-text">
        <span className={`credit-band ${band.tone}`}>{band.label}</span>
        <p>기준점수 <b>{credit.anchor}점</b> 대비 <b className={diff < 0 ? "minus" : "plus"}>{signed(diff)}점</b></p>
        <ol className="credit-scale" aria-label="점수 구간">
          {[...BANDS].reverse().map((item) => <li key={item.label} className={item.label === band.label ? `on ${item.tone}` : item.tone}><span>{item.label}</span><small>{item.min}{item.min === 900 ? "~" : "~"}</small></li>)}
        </ol>
      </div>
    </section>
    <div className="credit-columns">
      <section className="credit-card"><h2>점수에 영향을 준 요인</h2>
        {credit.factors.length ? <ul className="credit-factors">{credit.factors.map((factor) => <li key={factor.key}>
          <span>{factor.label}</span>
          <i className={factor.points < 0 ? "minus" : "plus"}><em style={{ width: `${Math.round(Math.abs(factor.points) / reach * 100)}%` }} /></i>
          <b className={factor.points < 0 ? "minus" : "plus"}>{signed(factor.points)}</b>
        </li>)}</ul> : <p className="credit-empty">아직 점수에 반영된 신용 이력이 없습니다.</p>}
      </section>
      <section className="credit-card"><h2>점수 변동 추이</h2><Trend history={credit.history} /></section>
    </div>
    {negatives.length > 0 && <section className="credit-card"><h2>점수를 올리려면</h2>
      <ul className="credit-tips">{negatives.map((factor) => <li key={factor.key}><b>{factor.label}</b><span>{TIPS[factor.key]}</span></li>)}</ul></section>}
    <section className="credit-card"><h2>신용 이력</h2>
      {events.length ? <ul className="credit-timeline">{events.slice(0, 20).map((event) => <li key={`${event.type}:${event.reference}`}>
        <b>{EVENTS[event.type] ?? event.type}{event.loanId ? ` · 대출 #${event.loanId}` : ""}</b>
        <span>{day(event.occurredEpoch)}{event.reason ? ` · ${event.reason}` : ""}</span></li>)}</ul>
        : <p className="credit-empty">표시할 신용 이력이 없습니다.</p>}
    </section>
  </div>;
}
