/** Exact native decimal amounts. Number conversion is reserved for chart coordinates. */
export type Money = string | number | bigint;
type Decimal = { units: bigint; scale: number };
export function parseMoney(value: Money): Decimal {
  const match = /^([+-]?)(\d+)(?:\.(\d*))?(?:e([+-]?\d+))?$/i.exec(String(value).trim().replace(/^([+-]?)\./, "$10."));
  if (!match) throw new Error("유효한 금액을 입력해주세요.");
  const exponent = Number(match[4] || 0);
  if (!Number.isSafeInteger(exponent) || Math.abs(exponent) > 100) throw new Error("금액 지수가 너무 큽니다.");
  let units = BigInt((match[1] === "-" ? "-" : "") + match[2] + (match[3] || ""));
  let scale = (match[3]?.length || 0) - exponent;
  if (scale < 0) { units *= 10n ** BigInt(-scale); scale = 0; }
  while (scale > 0 && units % 10n === 0n) { units /= 10n; scale--; }
  if (scale > 100) throw new Error("금액 소수 자릿수가 너무 많습니다.");
  return { units, scale };
}
function decimal({ units, scale }: Decimal): string {
  const sign = units < 0n ? "-" : "";
  const raw = (units < 0n ? -units : units).toString().padStart(scale + 1, "0");
  return sign + (scale ? raw.slice(0, -scale) + "." + raw.slice(-scale) : raw);
}
export const exactMoney = (value: Money): string => decimal(parseMoney(value));
/** Format share counts without exposing long repeating precision from path estimates. */
export function formatQuantity(value: Money, maxFractionDigits = 2): string {
  try {
    if (!Number.isInteger(maxFractionDigits) || maxFractionDigits < 0 || maxFractionDigits > 100) {
      throw new Error("유효하지 않은 소수 자릿수입니다.");
    }
    let { units, scale } = parseMoney(value);
    if (scale > maxFractionDigits) {
      const divisor = 10n ** BigInt(scale - maxFractionDigits);
      const negative = units < 0n;
      const absolute = negative ? -units : units;
      const rounded = (absolute + divisor / 2n) / divisor;
      units = negative ? -rounded : rounded;
      scale = maxFractionDigits;
    }
    const [whole, fraction] = decimal({ units, scale }).split(".");
    const trimmedFraction = fraction?.replace(/0+$/, "");
    const groupedWhole = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return groupedWhole + (trimmedFraction ? `.${trimmedFraction}` : "");
  } catch {
    return String(value ?? "—");
  }
}
export function minorMoney(value: Money, places: number): string {
  const parsed = parseMoney(value);
  return decimal({ units: parsed.units, scale: parsed.scale + places });
}
export function moneyCompare(a: Money, b: Money): number {
  const left = parseMoney(a), right = parseMoney(b), scale = Math.max(left.scale, right.scale);
  const delta = left.units * 10n ** BigInt(scale - left.scale) - right.units * 10n ** BigInt(scale - right.scale);
  return delta < 0n ? -1 : delta > 0n ? 1 : 0;
}
export function moneyAdd(a: Money, b: Money, subtract = false): string {
  const left = parseMoney(a), right = parseMoney(b), scale = Math.max(left.scale, right.scale);
  return decimal({ units: left.units * 10n ** BigInt(scale - left.scale) + (subtract ? -1n : 1n) * right.units * 10n ** BigInt(scale - right.scale), scale });
}
export function moneyMultiply(a: Money, b: Money): string {
  const left = parseMoney(a), right = parseMoney(b);
  return decimal({ units: left.units * right.units, scale: left.scale + right.scale });
}
/** Positive price division, rounded down once at the currency boundary. */
export function moneyDivideFloor(value: Money, divisor: number, places: number): string {
  if (!Number.isSafeInteger(divisor) || divisor <= 0) return "0";
  const parsed = parseMoney(value);
  const units = parsed.units * 10n ** BigInt(places) / (10n ** BigInt(parsed.scale) * BigInt(divisor));
  return minorMoney(units > 0n ? units : 1n, places);
}
export function validMoney(value: Money, positive = false): boolean {
  try { const amount = parseMoney(value); return amount.scale <= 6 && moneyCompare(value, positive ? 0 : '-1000000000000000000000000') >= (positive ? 1 : 0) && moneyCompare(value, '1000000000000000000000000') <= 0; } catch { return false; }
}
/** Large values include both a readable magnitude and every authoritative digit. */
export function formatMoney(value: Money, _digits = 6): string {
  try {
    const full = exactMoney(value), [whole, fraction] = full.split('.');
    const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (fraction ? '.' + fraction : '');
    if (whole.replace('-', '').length < 17) return grouped;
    const magnitude = exactMoney(minorMoney(whole, 16).replace(/(\.\d{4})\d+$/, '$1'));
    return `${magnitude}경 (${grouped})`;
  } catch { return String(value ?? '—'); }
}

/** Format a native currency amount at its declared minor-unit precision. */
export function formatMoneyFixed(value: Money, digits = 0): string {
  try {
    if (!Number.isInteger(digits) || digits < 0 || digits > 6) throw new Error("유효하지 않은 통화 정밀도입니다.");
    const parsed = parseMoney(value);
    const targetScale = Math.max(parsed.scale, digits);
    let units = parsed.units * 10n ** BigInt(targetScale - parsed.scale);
    if (targetScale > digits) {
      const divisor = 10n ** BigInt(targetScale - digits);
      const absolute = units < 0n ? -units : units;
      const rounded = (absolute + divisor / 2n) / divisor;
      units = units < 0n ? -rounded : rounded;
    }
    const fixed = decimal({ units, scale: digits });
    const [whole, fraction] = fixed.split(".");
    const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return digits ? `${grouped}.${fraction}` : grouped;
  } catch {
    return String(value ?? "—");
  }
}

/** Decimal division rounded once to the requested currency precision. */
export function moneyDivide(value: Money, divisor: Money, places = 6): string {
  const left = parseMoney(value), right = parseMoney(divisor);
  if (right.units === 0n) throw new Error("0으로 나눌 수 없습니다.");
  const numerator = left.units * 10n ** BigInt(right.scale + places);
  const denominator = right.units * 10n ** BigInt(left.scale);
  const negative = (numerator < 0n) !== (denominator < 0n);
  const absolute = numerator < 0n ? -numerator : numerator;
  const base = denominator < 0n ? -denominator : denominator;
  const units = absolute / base + (absolute % base * 2n >= base ? 1n : 0n);
  return minorMoney(negative ? -units : units, places);
}
