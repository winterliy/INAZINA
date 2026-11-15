"""Per-exchange listing requirements (상장요건).

Each regular exchange sets its own rules in market_exchanges.listing_* columns:

- listing_min_shares: fewest shares a listing may offer on that venue (0: none)
- listing_min_float_percent: offered shares as a share of all issued shares (0: none)
- listing_min_market_cap: offered shares x price, in the exchange country's currency (0: none)
- listing_allow_foreign: companies of another country may list (direct foreign listing)
- listing_allow_secondary: the venue accepts secondary (dual) listings
- listing_requires_disclosure: the company must have at least one closed financial report

Over-the-counter venues have no listing requirements.
"""
from decimal import Decimal, InvalidOperation
from fractions import Fraction

FIELDS = {
    "listing_min_shares": "최소 상장 주식 수",
    "listing_min_float_percent": "최소 상장 비율(%)",
    "listing_min_market_cap": "최소 상장 시가총액",
    "listing_allow_foreign": "해외 법인 직접 상장 허용",
    "listing_allow_secondary": "2차상장 허용",
    "listing_requires_disclosure": "재무보고서 공시 필요",
}
PAYLOAD_KEYS = {
    "minShares": "listing_min_shares", "minFloatPercent": "listing_min_float_percent",
    "minMarketCap": "listing_min_market_cap", "allowForeign": "listing_allow_foreign",
    "allowSecondary": "listing_allow_secondary", "requiresDisclosure": "listing_requires_disclosure",
}
FLAGS = {"listing_allow_foreign", "listing_allow_secondary", "listing_requires_disclosure"}


def exchange_currency(conn, nationality):
    row = conn.execute("SELECT currency_code FROM country_currencies WHERE nationality=?", (nationality,)).fetchone()
    if row and row[0]:
        return str(row[0])
    from functions.money import base_currency_code
    return base_currency_code(conn)


def normalize(payload):
    """Validate requirement fields of an admin request; returns {column: value} for the keys given."""
    values = {}
    for key, column in PAYLOAD_KEYS.items():
        if key not in payload or payload[key] is None or payload[key] == "":
            continue
        raw = payload[key]
        if column in FLAGS:
            if isinstance(raw, bool):
                values[column] = int(raw)
            elif raw in (0, 1, "0", "1"):
                values[column] = int(raw)
            else:
                raise ValueError(f"{FIELDS[column]}는 예/아니오로 지정해주세요.")
            continue
        if isinstance(raw, bool):
            raise ValueError(f"{FIELDS[column]}에 숫자를 입력해주세요.")
        try:
            number = Decimal(str(raw).replace(",", "").strip())
        except InvalidOperation:
            raise ValueError(f"{FIELDS[column]}에 숫자를 입력해주세요.") from None
        if not number.is_finite() or number < 0:
            raise ValueError(f"{FIELDS[column]}는 0 이상이어야 합니다.")
        if column == "listing_min_shares":
            if number != int(number) or number > 2**53 - 1:
                raise ValueError("최소 상장 주식 수는 0 이상의 정수여야 합니다.")
            values[column] = int(number)
        elif column == "listing_min_float_percent":
            if number > 100:
                raise ValueError("최소 상장 비율은 0~100%여야 합니다.")
            values[column] = float(number)
        else:
            from functions.amount_limits import MAX_NATIVE
            if number > MAX_NATIVE:
                raise ValueError("최소 상장 시가총액이 금액 한도를 넘습니다.")
            values[column] = format(number.normalize(), "f")
    return values


def as_payload(row):
    """The requirement columns of a market_exchanges row, in API (camelCase) form."""
    keys = row.keys()
    result = {}
    for key, column in PAYLOAD_KEYS.items():
        if column not in keys:
            continue
        value = row[column]
        result[key] = bool(value) if column in FLAGS else value
    return result


def check(conn, exchange_id, company_code, listed_shares, *, secondary=False, price=None, currency=None):
    """Raise ValueError when listing ``listed_shares`` on ``exchange_id`` breaks that venue's rules."""
    venue = conn.execute("SELECT * FROM market_exchanges WHERE exchange_id=?", (exchange_id,)).fetchone()
    if not venue or venue["market_type"] != "exchange" or "listing_min_shares" not in venue.keys():
        return
    company = conn.execute(
        "SELECT company_code,nationality,total_shares,share_price,quote_currency FROM company WHERE company_code=?",
        (company_code,)).fetchone()
    if not company:
        raise ValueError("상장할 법인을 찾을 수 없습니다.")
    label = f"{exchange_id} 상장요건"
    if secondary and not int(venue["listing_allow_secondary"]):
        raise ValueError(f"{label}: 이 거래소는 2차상장을 받지 않습니다.")
    if (not int(venue["listing_allow_foreign"]) and company["nationality"]
            and str(company["nationality"]) != str(venue["nationality"])):
        raise ValueError(f"{label}: {venue['nationality']} 법인만 상장할 수 있습니다 (해외 법인 불가).")
    shares = int(listed_shares)
    minimum = int(venue["listing_min_shares"] or 0)
    if shares < minimum:
        raise ValueError(f"{label}: 최소 {minimum:,}주 이상 상장해야 합니다 (요청 {shares:,}주).")
    total = int(company["total_shares"] or 0)
    percent = float(venue["listing_min_float_percent"] or 0)
    if percent and (not total or Fraction(shares * 100, total) < Fraction(str(percent))):
        raise ValueError(f"{label}: 발행 주식의 {percent:g}% 이상 상장해야 합니다 "
                         f"(요청 {shares:,}주 / 발행 {total:,}주).")
    minimum_cap = Decimal(str(venue["listing_min_market_cap"] or 0))
    if minimum_cap > 0:
        from managers.currency_manager import CurrencyManager
        target = exchange_currency(conn, venue["nationality"])
        source = str(currency or company["quote_currency"] or target)
        unit = Fraction(str(price if price is not None else company["share_price"] or 0))
        cap = unit * shares * (CurrencyManager.quote(conn, source, target) if source != target else 1)
        if cap < Fraction(str(minimum_cap)):
            shown = Decimal(cap.numerator) / Decimal(cap.denominator)
            raise ValueError(f"{label}: 상장 시가총액이 {minimum_cap:,} {target} 이상이어야 합니다 "
                             f"(현재 {shown:,.2f} {target}).")
    if int(venue["listing_requires_disclosure"]) and not conn.execute(
            "SELECT 1 FROM financial_periods WHERE company_code=? LIMIT 1", (company_code,)).fetchone():
        raise ValueError(f"{label}: 마감된 재무보고서가 1건 이상 있어야 합니다. 법인 회계에서 기간을 마감해주세요.")
