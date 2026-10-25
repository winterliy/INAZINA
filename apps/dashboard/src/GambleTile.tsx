import { useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import type { Ctx } from "./common";

type Row = { key: string; value: string };

/** 도박: win chance as a gauge, the payout, and the citizen's expected value, editable in place. */
export function GambleTile({ ctx }: { ctx: Ctx }) {
  const { busy, run, say } = ctx;
  const [chance, setChance] = useState("");
  const [payout, setPayout] = useState("");
  const [saved, setSaved] = useState({ chance: 0, payout: 0 });

  const load = useCallback(async () => {
    const rows = (await api<{ rows: Row[] }>("/api/admin/db/tables/settings?search=gamble")).rows;
    const value = (key: string) => Number(rows.find((row) => row.key === key)?.value ?? 0);
    setSaved({ chance: value("gamble_chance"), payout: value("gamble_return") });
    setChance(String(value("gamble_chance")));
    setPayout(String(value("gamble_return")));
  }, []);
  useEffect(() => { load().catch(() => undefined); }, [load]);

  function apply() {
    const changes: [string, string][] = [];
    if (Number(chance) !== saved.chance) changes.push(["도박확률", chance]);
    if (Number(payout) !== saved.payout) changes.push(["도박리턴값", payout]);
    if (!changes.length || !window.confirm(changes.map(([name, value]) => `$${name} ${value}`).join("\n") + "\n\n실행할까요?")) return;
    void run(async () => {
      for (const [command, args] of changes) {
        const result = await api<{ message: string; success: boolean }>("/api/admin/commands", { method: "POST", body: JSON.stringify({ command, arguments: args }) });
        if (!result.success) throw new Error(result.message);
      }
    }, "도박 설정을 바꾸지 못했습니다.", () => { say("도박 설정을 바꿨습니다."); void load(); });
  }

  const odds = Math.max(0, Math.min(100, Number(chance) || 0));
  // A win returns the stake plus payout%: expected value = chance × (1 + payout) − 1.
  const expected = odds / 100 * (1 + (Number(payout) || 0) / 100) - 1;
  return <section className="tile gamble-tile" aria-label="도박 설정">
    <p className="tile-label">도박 · 승률과 배당</p>
    <div className="gamble-body">
      <svg viewBox="0 0 120 120" role="img" aria-label={`당첨 확률 ${odds}%`}><circle cx="60" cy="60" r="50" className="gauge-track" /><circle cx="60" cy="60" r="50" className="gauge-fill" strokeDasharray={`${odds / 100 * 314} 314`} transform="rotate(-90 60 60)" /><text x="60" y="66" textAnchor="middle">{odds}%</text></svg>
      <div className="gamble-fields">
        <label>당첨 확률 %<input inputMode="decimal" value={chance} onChange={(event) => setChance(event.target.value)} /></label>
        <label>당첨 시 리턴 %<input inputMode="decimal" value={payout} onChange={(event) => setPayout(event.target.value)} /></label>
        <p className={expected >= 0 ? "gamble-ev up" : "gamble-ev"}>시민 기대값 {expected >= 0 ? "+" : ""}{(expected * 100).toFixed(1)}%</p>
        <button className="btn primary" type="button" disabled={busy} onClick={apply}>적용</button>
      </div>
    </div>
  </section>;
}
