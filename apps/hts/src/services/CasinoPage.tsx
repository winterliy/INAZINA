import { useEffect, useState } from "react";
import type { Result } from "../commandKit";
import { Outcome, won, type Kit, type ServiceProps } from "./ServiceKit";

type Round = { round_id: number; status: string; close_epoch: number; draw_epoch: number; sales_total: number; rules: { ticket_price: number; max_tickets: number; prize_percent: number };
  pools: number[]; winning: number[]; bonus: number | null; result: Record<string, { winners: number; gross_per_ticket: number }>; commitment?: string };
type Ticket = { ticket_id: number; round_id: number; numbers_json: string; price: number; created_epoch: number; rank: number | null; net: number | null };
const ROUND_STATUS: Record<string, string> = { selling: "판매 중", closed: "판매 마감", drawn: "추첨 완료", settled: "당첨금 지급 완료" };
const RANKS = ["1등", "2등", "3등", "4등", "5등"];
const at = (epoch: number) => new Date(epoch * 1000).toLocaleString("ko-KR", { month: "long", day: "numeric", weekday: "short", hour: "2-digit", minute: "2-digit" });
const balls = (numbers: number[], bonus?: number | null) => <span className="lotto-row">{numbers.map((number) => <i key={number} className={`lotto-ball small ${ball(number)}`}>{number}</i>)}{bonus ? <><b>+</b><i className={`lotto-ball small ${ball(bonus)}`}>{bonus}</i></> : null}</span>;

/** 이번 회차, 내 복권, 당첨 결과 — the lottery's read-only views laid out as a lottery site would. */
function LottoBoard({ kit, refresh }: { kit: Kit; refresh: number }) {
  const [round, setRound] = useState<Round | null>(null);
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [result, setResult] = useState<Round | null>(null);
  const [resultRound, setResultRound] = useState("");
  const [error, setError] = useState("");
  const read = async <T,>(name: string, values: Record<string, string>) => {
    const outcome = await kit.run(name, values, false);
    if (outcome && !outcome.success) setError(outcome.message);
    return (outcome?.success ? outcome.data : null) as T | null;
  };
  useEffect(() => {
    void read<Round>("복권규칙", {}).then(setRound);
    void read<Ticket[]>("내복권", { round: "" }).then((items) => setTickets(items ?? []));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refresh]);
  useEffect(() => { void read<Round>("복권결과", { round: resultRound }).then(setResult); }, [resultRound]);

  return <div className="casino-info">
    <section className="lotto-card">
      <header><h3>{round ? `제 ${round.round_id}회` : "이번 회차"}</h3>{round && <span className="lotto-status">{ROUND_STATUS[round.status] ?? round.status}</span>}</header>
      {round ? <dl className="lotto-facts">
        <div><dt>판매 마감</dt><dd>{at(round.close_epoch)}</dd></div>
        <div><dt>추첨</dt><dd>{at(round.draw_epoch)}</dd></div>
        <div><dt>1장 가격</dt><dd>{won(round.rules.ticket_price)}</dd></div>
        <div><dt>1인 최대</dt><dd>{round.rules.max_tickets}장</dd></div>
        <div><dt>누적 판매</dt><dd>{won(round.sales_total)}</dd></div>
        <div><dt>상금 비율</dt><dd>판매액의 {round.rules.prize_percent}%</dd></div>
      </dl> : <p className="lotto-empty">{error || "회차 정보를 불러오는 중…"}</p>}
      {round && round.pools.length > 0 && <ol className="lotto-pools">{round.pools.map((pool, index) => <li key={index}><span>{RANKS[index]}</span><b>{won(pool)}</b></li>)}</ol>}
    </section>
    <section className="lotto-card">
      <header><h3>내 복권</h3><span className="lotto-status">{tickets.length}장</span></header>
      {tickets.length ? <ul className="lotto-tickets">{tickets.slice(0, 30).map((ticket) => <li key={ticket.ticket_id}>
        <small>제 {ticket.round_id}회 · #{ticket.ticket_id}</small>
        {balls(JSON.parse(ticket.numbers_json))}
        <em className={ticket.rank ? "won" : ""}>{ticket.rank ? `${ticket.rank}등 · ${won(ticket.net ?? 0)}` : ticket.rank === 0 ? "낙첨" : "추첨 전"}</em>
      </li>)}</ul> : <p className="lotto-empty">이번 회차에 산 복권이 없습니다.</p>}
    </section>
    <section className="lotto-card">
      <header><h3>{result ? `제 ${result.round_id}회 당첨 결과` : "당첨 결과"}</h3>
        <span className="lotto-nav"><button type="button" disabled={!result || result.round_id <= 1} onClick={() => result && setResultRound(String(result.round_id - 1))}>‹</button><button type="button" disabled={!result || !resultRound} onClick={() => result && setResultRound(String(result.round_id + 1))}>›</button></span></header>
      {result && result.winning.length ? <>
        <div className="lotto-winning">{balls(result.winning, result.bonus)}</div>
        <table className="lotto-result"><thead><tr><th>등수</th><th>당첨</th><th>1장당</th></tr></thead><tbody>{RANKS.map((label, index) => { const row = result.result[String(index + 1)]; return <tr key={label}><td>{label}</td><td>{row ? `${row.winners}장` : "—"}</td><td>{row ? won(row.gross_per_ticket) : "—"}</td></tr>; })}</tbody></table>
      </> : <p className="lotto-empty">{result ? "아직 추첨 전입니다." : "추첨된 회차가 없습니다."}</p>}
    </section>
  </div>;
}

const CHIPS = [1_000, 10_000, 50_000, 100_000];
const BALLS = Array.from({ length: 45 }, (_, index) => index + 1);
const ball = (number: number) => number <= 10 ? "b1" : number <= 20 ? "b2" : number <= 30 ? "b3" : number <= 40 ? "b4" : "b5";

/** 복권 · 게임 as a casino lobby: 럭키 베팅 with chips and a 6/45 slip. Same 도박 and 복권 rules as Discord. */
export function CasinoPage({ kit, cash, homeCurrency, homeDigits }: ServiceProps) {
  const [bet, setBet] = useState(0);
  const [spin, setSpin] = useState<Result | null>(null);
  const [picked, setPicked] = useState<number[]>([]);
  const [tickets, setTickets] = useState("1");
  const [slip, setSlip] = useState<Result | null>(null);
  const [refresh, setRefresh] = useState(0);

  async function play() {
    if (bet <= 0) return;
    const outcome = await kit.run("도박", { amount: String(bet) });
    if (outcome) setSpin(outcome);
  }
  async function buy(auto: boolean) {
    const numbers = auto ? "" : [...picked].sort((a, b) => a - b).join(",");
    if (!auto && picked.length !== 6) return;
    const outcome = await kit.run("복권구매", { quantity: tickets || "1", numbers });
    if (outcome) { setSlip(outcome); if (outcome.success) { setPicked([]); setRefresh((value) => value + 1); } }
  }
  // 도박 answers 🎉 당첨 on a win and 💀 꽝 (success false) on a loss; anything else is an error.
  const outcome = !spin ? "idle" : spin.success && spin.message.includes("당첨") ? "win" : spin.message.includes("꽝") ? "lose" : "error";

  return <div className="casino">
    <section className="casino-hero">
      <p>INAZINA CASINO</p>
      <h2>오늘의 행운을 시험해 보세요</h2>
      <span>보유 {won(cash, homeDigits)} {homeCurrency}</span>
    </section>

    <div className="casino-grid">
      <section className="casino-table" aria-label="럭키 베팅">
        <header><h3>럭키 베팅</h3><p>관리자가 정한 확률로 당첨되면 리턴 비율만큼 받고, 지면 건 금액은 국고로 갑니다.</p></header>
        <div className={`casino-wheel ${outcome}`} aria-live="polite">
          <b>{{ idle: "BET", win: "WIN", lose: "LOSE", error: "!" }[outcome]}</b>
          {spin && <small>{spin.message}</small>}
        </div>
        <div className="casino-bet"><span>베팅 금액</span><b>{won(bet)} <small>{homeCurrency}</small></b></div>
        <div className="casino-chips">{CHIPS.map((chip) => <button type="button" key={chip} className="chip" onClick={() => setBet(bet + chip)}>{chip >= 10_000 ? `${chip / 10_000}만` : `${chip / 1000}천`}</button>)}<button type="button" className="chip all" onClick={() => setBet(Math.floor(cash))}>올인</button><button type="button" className="chip clear" onClick={() => setBet(0)}>초기화</button></div>
        <button type="button" className="casino-play" disabled={kit.busy || bet <= 0 || bet > cash} onClick={() => void play()}>{kit.busy ? "결과 확인 중…" : "베팅하기"}</button>
        {bet > cash && <p className="casino-warn">보유 금액보다 많이 걸 수 없습니다.</p>}
      </section>

      <section className="casino-lotto" aria-label="로또 6/45">
        <header><h3>로또 6/45</h3><p>1~45 중 6개를 고르거나 자동으로 구매하세요. 매주 추첨합니다.</p></header>
        <div className="lotto-picked" aria-live="polite">{Array.from({ length: 6 }, (_, index) => [...picked].sort((a, b) => a - b)[index]).map((number, index) => <span key={index} className={number ? `lotto-ball ${ball(number)}` : "lotto-ball empty"}>{number ?? ""}</span>)}</div>
        <div className="lotto-grid">{BALLS.map((number) => <button type="button" key={number} aria-pressed={picked.includes(number)} className={picked.includes(number) ? `on ${ball(number)}` : ""} disabled={!picked.includes(number) && picked.length >= 6} onClick={() => setPicked(picked.includes(number) ? picked.filter((item) => item !== number) : [...picked, number])}>{number}</button>)}</div>
        <div className="lotto-buy">
          <label>장 수<input inputMode="numeric" value={tickets} onChange={(event) => setTickets(event.target.value.replace(/\D/g, ""))} /></label>
          <button type="button" onClick={() => setPicked([])} disabled={!picked.length}>지우기</button>
          <button type="button" className="casino-play" disabled={kit.busy || picked.length !== 6} onClick={() => void buy(false)}>선택 번호로 구매</button>
          <button type="button" className="casino-play alt" disabled={kit.busy} onClick={() => void buy(true)}>자동 구매</button>
        </div>
        <Outcome result={slip} />
      </section>
    </div>

    <LottoBoard kit={kit} refresh={refresh} />
  </div>;
}
