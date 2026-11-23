import { useCallback, useEffect, useState } from "react";
import { api } from "../../shared/api";
import { Card, errorText, type Ctx } from "./common";
import { formatUptime, serviceLabel, serviceState } from "./opsFormat";
import "./security.css";
import "./ops.css";

type Ops = {
  api: { pid: number; uptimeSeconds: number; node: string; mode: "build" | "watch" };
  services: { name: string; port: number; open: boolean }[];
  bot: { running: boolean };
  settings: Record<string, string>;
  logs: { dir: string; services: string[] };
};
type LogTail = { service: string; missing: boolean; lines: string[] };

const SETTING_LABELS: Record<string, string> = {
  INAZINA_PYTHON_WORKERS: "데이터 서비스 워커 수", INAZINA_MAX_HTTP_THREADS: "워커당 동시 요청 수", INAZINA_FRONTEND: "화면 제공 방식 (dev·static)",
  INAZINA_WL: "위키 뷰어", INAZINA_DOMAIN: "도메인", INAZINA_GUEST_RATE_PER_MINUTE: "비회원 분당 요청 한도",
  INAZINA_READ_CACHE_SECONDS: "읽기 캐시(초)", INAZINA_DB_BACKEND: "데이터베이스",
};
const LOG_LABELS: Record<string, string> = { api: "API 서버 · 데이터 서비스", bot: "디스코드 봇", caddy: "도메인 서버 (Caddy)", hts: "나루 화면", dashboard: "콘솔 화면", site: "공식 사이트 화면", wl: "위키 뷰어" };

/** 서비스: which services answer, the settings that matter and the end of each log (read-only; start_all runs the services in a terminal). */
export function OpsPage({ ctx }: { ctx: Ctx }) {
  const { say } = ctx;
  const [ops, setOps] = useState<Ops | null>(null);
  const [service, setService] = useState("api");
  const [lines, setLines] = useState(200);
  const [tail, setTail] = useState<LogTail | null>(null);
  const [busy, setBusy] = useState(false);

  const loadStatus = useCallback(async () => setOps(await api<Ops>("/api/admin/ops")), []);
  const loadLog = useCallback(async () => setTail(await api<LogTail>(`/api/admin/ops/log?service=${encodeURIComponent(service)}&lines=${lines}`)), [service, lines]);
  const refresh = useCallback(async () => {
    setBusy(true);
    try { await Promise.all([loadStatus(), loadLog()]); } catch (cause) { say(errorText(cause, "서비스 정보를 불러오지 못했습니다."), true); } finally { setBusy(false); }
  }, [loadStatus, loadLog, say]);
  useEffect(() => { void refresh(); }, [refresh]);

  if (!ops) return <p className="cap">불러오는 중…</p>;
  const settings = ops.settings;
  return <>
    <Card title="서비스 상태" caption="각 서비스의 포트가 응답하는지 확인합니다. 켜고 끄는 일은 터미널의 start_all 에서 합니다." actions={<button className="btn" disabled={busy} onClick={() => void refresh()}>{busy ? "확인 중…" : "새로고침"}</button>}>
      <div className="scroll-x"><table><thead><tr><th>서비스</th><th>포트</th><th>상태</th></tr></thead><tbody>
        {ops.services.map((row) => { const state = serviceState(row, settings); return <tr key={row.name}><td>{serviceLabel(row.name)}</td><td className="mono">{row.port}</td>
          <td><span className={`ops-dot ${state.ok ? "on" : "off"}`} aria-hidden="true" /> {state.text}</td></tr>; })}
        <tr><td>디스코드 봇</td><td className="mono">—</td><td><span className={`ops-dot ${ops.bot.running ? "on" : "off"}`} aria-hidden="true" /> {ops.bot.running ? "실행 중" : "꺼짐"}</td></tr>
      </tbody></table></div>
      <p className="cap">API 서버 · {ops.api.mode === "build" ? "빌드본 실행" : "개발 모드"} · 가동 {formatUptime(ops.api.uptimeSeconds)} · Node {ops.api.node} · PID {ops.api.pid}</p>
    </Card>

    <Card title="설정값" caption="보기만 가능합니다. 바꾸려면 .env 를 고치고 start_all 을 다시 시작하세요. 비밀값은 표시하지 않습니다.">
      <dl className="security-facts">
        {Object.entries(settings).map(([key, value]) => <div key={key}><dt>{SETTING_LABELS[key] ?? key}</dt><dd><span className="mono">{value}</span> <span className="cap">{key}</span></dd></div>)}
      </dl>
    </Card>

    <Card title="로그" caption={`start_all 이 ${ops.logs.dir}/ 폴더에 서비스별로 남긴 파일의 끝부분입니다.`} actions={<>
      <select aria-label="서비스" value={service} onChange={(event) => setService(event.target.value)}>
        {ops.logs.services.map((name) => <option key={name} value={name}>{LOG_LABELS[name] ?? name}</option>)}
      </select>
      <select aria-label="줄 수" value={lines} onChange={(event) => setLines(Number(event.target.value))}>
        {[50, 200, 500].map((count) => <option key={count} value={count}>{count}줄</option>)}
      </select></>}>
      {tail?.missing ? <p className="empty">아직 로그 파일이 없습니다. start_all 로 시작하면 생깁니다.</p>
        : !tail?.lines.length ? <p className="empty">비어 있습니다.</p>
        : <pre className="ops-log" tabIndex={0} aria-label={`${LOG_LABELS[service] ?? service} 로그`}>{tail.lines.join("\n")}</pre>}
    </Card>
  </>;
}
