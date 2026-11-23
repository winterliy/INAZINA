/** Wording for the console's 서비스 page. */
export function formatUptime(seconds: number): string {
  const minutes = Math.floor(Math.max(0, seconds) / 60);
  if (minutes < 1) return "1분 미만";
  const days = Math.floor(minutes / 1440), hours = Math.floor((minutes % 1440) / 60), rest = minutes % 60;
  if (days) return hours ? `${days}일 ${hours}시간` : `${days}일`;
  if (hours) return rest ? `${hours}시간 ${rest}분` : `${hours}시간`;
  return `${rest}분`;
}

const LABELS: Record<string, string> = { api: "API 서버", data: "데이터 서비스", caddy: "도메인 서버 (Caddy)", hts: "나루 화면", dashboard: "콘솔 화면", site: "공식 사이트 화면", wl: "위키 뷰어 (wl.localhost)" };
export function serviceLabel(name: string): string {
  const worker = /^data-(\d+)$/.exec(name);
  return worker ? `${LABELS.data} ${worker[1]}` : LABELS[name] ?? name;
}

type Row = { name: string; port: number; open: boolean };
const SCREENS = new Set(["hts", "dashboard", "site"]);
export function serviceState(row: Row, settings: Record<string, string>): { text: string; ok: boolean } {
  if (row.open) return { text: "켜짐", ok: true };
  if (SCREENS.has(row.name) && settings.INAZINA_FRONTEND === "static") return { text: "Caddy 가 직접 제공", ok: true };
  return { text: "꺼짐", ok: false };
}
