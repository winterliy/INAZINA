import { useEffect, useRef, useState } from "react";
import { api, type ApiUser } from "../../shared/api";
import type { Drawing } from "./PriceChart";
import { errorText } from "./appShared";

/** Chart drawings are saved per user and instrument (after a 600ms pause) once the server copy is loaded. */
export function useDrawings(selectedCode: string, user: ApiUser | null, setNotice: (text: string) => void) {
  const [drawings, setDrawings] = useState<Drawing[]>([]);
  const drawingsLoadedFor = useRef("");
  const savedDrawings = useRef("[]");

  useEffect(() => {
    if (!selectedCode || !user) return;
    let cancelled = false;
    drawingsLoadedFor.current = "";
    setDrawings([]);
    api<{ drawings: Drawing[] }>(`/api/market/drawings/${encodeURIComponent(selectedCode)}`)
      .then((result) => {
        if (cancelled) return;
        savedDrawings.current = JSON.stringify(result.drawings);
        drawingsLoadedFor.current = selectedCode;
        setDrawings(result.drawings);
      })
      .catch((cause) => { if (!cancelled) setNotice(errorText(cause, "차트 선을 불러오지 못했습니다.")); });
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedCode, user]);

  useEffect(() => {
    const code = drawingsLoadedFor.current;
    if (!code || code !== selectedCode) return;
    const serialized = JSON.stringify(drawings);
    if (serialized === savedDrawings.current) return;
    const timer = window.setTimeout(() => {
      api<{ drawings: Drawing[] }>(`/api/market/drawings/${encodeURIComponent(code)}`, { method: "POST", body: JSON.stringify({ drawings }) })
        .then(() => { savedDrawings.current = serialized; })
        .catch((cause) => setNotice(errorText(cause, "차트 선을 저장하지 못했습니다.")));
    }, 600);
    return () => window.clearTimeout(timer);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drawings, selectedCode]);

  return [drawings, setDrawings] as const;
}
