import { useEffect, useRef, useState } from "react";

const WEEKDAYS = ["일", "월", "화", "수", "목", "금", "토"];
const pad = (value: number) => String(value).padStart(2, "0");
const dayStart = (date: Date) => new Date(date.getFullYear(), date.getMonth(), date.getDate());

/**
 * A button that opens a month calendar with hour/minute selects.
 * `value` and `onChange` use epoch seconds (null: no date). Days outside
 * [min, max] cannot be picked, and the chosen time is kept inside them.
 */
export function DateTimePicker({ value, onChange, min, max, placeholder = "날짜 선택" }: {
  value: number | null; onChange: (epoch: number | null) => void; min: number; max: number; placeholder?: string;
}) {
  const [open, setOpen] = useState(false);
  const initial = new Date((value ?? min + 3600) * 1000);
  const [month, setMonth] = useState(new Date(initial.getFullYear(), initial.getMonth(), 1));
  const [day, setDay] = useState<Date>(dayStart(initial));
  const [hour, setHour] = useState(initial.getHours());
  const [minute, setMinute] = useState(Math.floor(initial.getMinutes() / 5) * 5);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") { event.stopPropagation(); setOpen(false); } };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", escape, true);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", escape, true); };
  }, [open]);

  const minDay = dayStart(new Date(min * 1000)), maxDay = dayStart(new Date(max * 1000));
  const first = new Date(month.getFullYear(), month.getMonth(), 1);
  const cells = Array.from({ length: 42 }, (_, index) => new Date(first.getFullYear(), first.getMonth(), index - first.getDay() + 1));
  const canPrev = new Date(month.getFullYear(), month.getMonth(), 0) >= minDay;
  const canNext = new Date(month.getFullYear(), month.getMonth() + 1, 1) <= maxDay;
  const chosen = () => Math.floor(new Date(day.getFullYear(), day.getMonth(), day.getDate(), hour, minute).getTime() / 1000);
  const label = value ? new Intl.DateTimeFormat("ko-KR", { dateStyle: "medium", timeStyle: "short" }).format(value * 1000) : placeholder;

  function confirm() {
    onChange(Math.min(max, Math.max(min, chosen())));
    setOpen(false);
  }

  return <div className="picker" ref={root}>
    <button type="button" className={value ? "picker-button set" : "picker-button"} aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(!open)}>📅 {label}</button>
    {value !== null && <button type="button" className="picker-clear" aria-label="날짜 지우기" onClick={() => onChange(null)}>✕</button>}
    {open && <div className="picker-pop" role="dialog" aria-label="날짜와 시간 선택">
      <div className="picker-head">
        <button type="button" disabled={!canPrev} aria-label="이전 달" onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() - 1, 1))}>‹</button>
        <b>{month.getFullYear()}년 {month.getMonth() + 1}월</b>
        <button type="button" disabled={!canNext} aria-label="다음 달" onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() + 1, 1))}>›</button>
      </div>
      <div className="picker-grid" role="grid">
        {WEEKDAYS.map((name) => <span key={name} className="picker-weekday">{name}</span>)}
        {cells.map((date) => {
          const outside = date.getMonth() !== month.getMonth();
          const disabled = date < minDay || date > maxDay;
          const selected = date.getTime() === day.getTime();
          return <button key={date.getTime()} type="button" disabled={disabled} aria-pressed={selected}
            className={["picker-day", outside ? "outside" : "", selected ? "selected" : "", date.getTime() === dayStart(new Date()).getTime() ? "today" : ""].join(" ")}
            onClick={() => setDay(date)}>{date.getDate()}</button>;
        })}
      </div>
      <div className="picker-time">
        <label>시<select value={hour} onChange={(event) => setHour(Number(event.target.value))}>{Array.from({ length: 24 }, (_, value) => <option key={value} value={value}>{pad(value)}</option>)}</select></label>
        <label>분<select value={minute} onChange={(event) => setMinute(Number(event.target.value))}>{Array.from({ length: 12 }, (_, index) => index * 5).map((value) => <option key={value} value={value}>{pad(value)}</option>)}</select></label>
      </div>
      <p className="picker-preview">{new Intl.DateTimeFormat("ko-KR", { dateStyle: "full", timeStyle: "short" }).format(chosen() * 1000)}{chosen() < min || chosen() > max ? " (허용 범위로 맞춰집니다)" : ""}</p>
      <div className="picker-actions"><button type="button" onClick={() => setOpen(false)}>취소</button><button type="button" className="board-primary" onClick={confirm}>확인</button></div>
    </div>}
  </div>;
}
