/**
 * Chart scales that re-plan themselves for the space they get, like an HTS:
 * the price axis picks round steps for its height, the time axis picks calendar
 * steps for its width (aligned to KST minutes, hours, days, months), and the axis
 * and lower panels size themselves from what they have to show.
 */

const KST_OFFSET = 9 * 3600;
/** Roughly one price label per this many pixels of height, one time label per this many of width. */
export const PRICE_LABEL_PX = 46;
export const TIME_LABEL_PX = 96;

/** Round ticks covering [min, max]: steps of 1, 2, 2.5 or 5 × 10ⁿ, about `count` of them. */
export function priceTicks(min: number, max: number, count: number): { step: number; ticks: number[]; decimals: number } {
  const span = max - min;
  if (!(span > 0) || !Number.isFinite(span)) return { step: 1, ticks: [min], decimals: 0 };
  const raw = span / Math.max(1, count);
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((unit) => unit * power).find((value) => value >= raw) ?? 10 * power;
  const decimals = Math.max(0, -Math.floor(Math.log10(step) + 1e-9) + (step / power === 2.5 ? 1 : 0));
  const ticks: number[] = [];
  for (let value = Math.ceil(min / step) * step; value <= max + step * 1e-9; value += step) {
    ticks.push(Number(value.toFixed(decimals)));
  }
  return { step, ticks, decimals };
}

const MINUTE = 60, HOUR = 3600, DAY = 86400;
// Calendar steps an HTS time axis uses; months are handled separately.
const TIME_STEPS = [MINUTE, 2 * MINUTE, 5 * MINUTE, 10 * MINUTE, 15 * MINUTE, 30 * MINUTE, HOUR, 2 * HOUR, 3 * HOUR,
  6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 14 * DAY];
const MONTH_STEPS = [1, 2, 3, 6, 12];

const kstParts = (time: number) => {
  const date = new Date((time + KST_OFFSET) * 1000);
  return { year: date.getUTCFullYear(), month: date.getUTCMonth(), day: date.getUTCDate(), hour: date.getUTCHours(), minute: date.getUTCMinutes() };
};
const kstMonthStart = (year: number, month: number) => Date.UTC(year, month, 1) / 1000 - KST_OFFSET;

/** Time ticks for [minTime, maxTime] at about `count` labels, on KST calendar boundaries, with their labels. */
export function timeTicks(minTime: number, maxTime: number, count: number): { step: number; ticks: { time: number; label: string }[] } {
  const span = maxTime - minTime;
  if (!(span > 0)) return { step: MINUTE, ticks: [] };
  const raw = span / Math.max(1, count);
  const fixed = TIME_STEPS.find((step) => step >= raw);
  const ticks: { time: number; label: string }[] = [];
  if (fixed !== undefined) {
    // Days and longer start at KST midnight; shorter steps at round KST clock times.
    let time = Math.ceil((minTime + KST_OFFSET) / fixed) * fixed - KST_OFFSET;
    if (fixed === 7 * DAY || fixed === 14 * DAY) time = Math.ceil((minTime + KST_OFFSET) / DAY) * DAY - KST_OFFSET;
    for (; time <= maxTime; time += fixed) ticks.push({ time, label: timeLabel(time, fixed, ticks.at(-1)?.time) });
    return { step: fixed, ticks };
  }
  const months = MONTH_STEPS.find((step) => step * 30 * DAY >= raw) ?? 12 * Math.ceil(raw / (365 * DAY));
  const first = kstParts(minTime);
  let index = first.year * 12 + first.month;
  index = Math.ceil(index / months) * months;
  for (let time = kstMonthStart(Math.floor(index / 12), index % 12); time <= maxTime; index += months, time = kstMonthStart(Math.floor(index / 12), index % 12)) {
    if (time >= minTime) ticks.push({ time, label: timeLabel(time, months * 30 * DAY, ticks.at(-1)?.time) });
  }
  return { step: months * 30 * DAY, ticks };
}

/** "09:30", "11/2" at midnight, "11/2 15:00" when the day changes, "2026.11" for months. */
function timeLabel(time: number, step: number, previous?: number): string {
  const at = kstParts(time);
  const pad = (value: number) => String(value).padStart(2, "0");
  if (step >= 28 * DAY) return at.month === 0 || previous === undefined ? `${at.year}.${at.month + 1}` : `${at.month + 1}월`;
  if (step >= DAY) return `${at.month + 1}/${at.day}`;
  const clock = `${pad(at.hour)}:${pad(at.minute)}`;
  const newDay = previous === undefined || kstParts(previous).day !== at.day;
  return newDay && (at.hour || at.minute) ? `${at.month + 1}/${at.day} ${clock}` : at.hour || at.minute ? clock : `${at.month + 1}/${at.day}`;
}

/** Price axis width that fits its longest label. */
export function axisWidth(labels: string[]): number {
  const longest = labels.reduce((max, text) => Math.max(max, text.length), 4);
  return Math.min(120, Math.max(60, 16 + longest * 7.2));
}

/** Height of each lower panel: a share of the chart, never squeezing the price plot under half. */
export function panelHeight(height: number, panels: number): number {
  if (!panels) return 0;
  const share = Math.min(120, Math.max(44, height * 0.16));
  return Math.max(36, Math.min(share, (height * 0.5) / panels - 10));
}
