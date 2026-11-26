import { useEffect, useState } from "react";
import { api } from "../../shared/api";
import { AnnouncementCard } from "./AnnouncementCard";
import type { Announcement } from "./announcements";
import "./announcements.css";

/** The notice list inside 나루 (the bell notification leads here): every recent announced change with its comparison. */
export function AnnouncementsPage() {
  const [items, setItems] = useState<Announcement[] | null>(null);
  useEffect(() => { api<{ announcements: Announcement[] }>("/api/me/announcements/all").then((result) => setItems(result.announcements)).catch(() => setItems([])); }, []);
  return <div className="announce-page">
    <h2>약관·방침 개정 안내</h2>
    <p className="announce-lead">최근의 개정 안내입니다. 공식 사이트의 공지 페이지에서도 볼 수 있습니다.</p>
    {items === null ? <p role="status">불러오는 중…</p> : items.length ? items.map((item) => <AnnouncementCard key={item.id} item={item} />) : <p className="announce-none">아직 개정 안내가 없습니다.</p>}
  </div>;
}
