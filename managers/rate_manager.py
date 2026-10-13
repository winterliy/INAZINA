"""Game policy rates and nationality-specific legal interest-rate caps."""

import math
import time
from contextlib import nullcontext

from functions.interest_calc import YEAR_SECONDS
from functions.admin_access import resolve_user_role


RATE_CHART_PREFIX = "__rate__:"
RATE_NAMES = {"base_rate": "기준금리", "legal_max_rate": "법정최고이자"}
MARKET_RATE_NAMES = {"funding": "조달금리", "bank_bond": "은행채금리", "treasury": "국채금리"}


def rate_key_from_code(code):
    """Resolve reserved chart codes and the two Korean rate names."""
    if not isinstance(code, str):
        return None
    value = code.strip()
    if value.startswith(RATE_CHART_PREFIX):
        key = value[len(RATE_CHART_PREFIX):]
        return key if key in RATE_NAMES else None
    return next((key for key, name in RATE_NAMES.items() if value == name), None)


class RateManager:
    KEYS = RATE_NAMES
    RATE_CHART_PREFIX = RATE_CHART_PREFIX
    rate_key_from_code = staticmethod(rate_key_from_code)

    def __init__(self, db):
        self.db = db

    @staticmethod
    def chart_code(key):
        key = key if key in RATE_NAMES else rate_key_from_code(key)
        if key is None:
            raise ValueError("지원하지 않는 금리입니다.")
        return RATE_CHART_PREFIX + key

    def get_rate_history(self, key, since, now=None):
        """Return a right-continuous annual-rate series, with both plot boundaries.

        Unlike disposable price movement rows, dated rates are also needed for
        interest accrual. This is a read-only view of their original history.
        Fractional change timestamps and percentages are deliberately preserved.
        """
        key = key if key in self.KEYS else rate_key_from_code(key)
        if key is None:
            raise ValueError("지원하지 않는 금리입니다.")
        start = float(since)
        end = float(time.time() if now is None else now)
        if not math.isfinite(start) or not math.isfinite(end) or end < start:
            raise ValueError("조회 기간은 시작 시각 이후의 유효한 UNIX 시각이어야 합니다.")
        rate = self.get_rate(key)
        return [(start, rate)] if start == end else [(start, rate), (end, rate)]

    @staticmethod
    def from_connection(conn, key, nationality=None, *, require_country=False):
        if key not in RATE_NAMES:
            raise ValueError("지원하지 않는 금리입니다.")
        if nationality is not None:
            row = conn.execute(
                f"SELECT {key} rate FROM country_currencies WHERE nationality=?",
                (str(nationality),),
            ).fetchone()
            if not row and require_country:
                raise ValueError("국가 ID를 확인해주세요.")
            if row:
                rate = float(row["rate"])
            else:
                fallback = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
                rate = float(fallback["value"] if fallback else 0)
        else:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            rate = float(row["value"] if row else 0)
        low = -100 if key == "base_rate" else 0
        if not math.isfinite(rate) or not low <= rate <= 100:
            raise ValueError(f"{RATE_NAMES[key]}는 {low}~100%의 유한한 숫자여야 합니다.")
        return rate

    def get_rate(self, key, nationality=None):
        if key not in self.KEYS:
            raise ValueError("지원하지 않는 금리입니다.")
        with self.db.readonly() as conn:
            return self.from_connection(conn, key, nationality, require_country=nationality is not None)

    @staticmethod
    def for_exchange(conn, key, exchange_id):
        row = conn.execute(
            "SELECT nationality FROM market_exchanges WHERE exchange_id=?",
            (str(exchange_id or ''),),
        ).fetchone()
        return RateManager.from_connection(
            conn, key, row['nationality'] if row and row['nationality'] else None
        )

    def set_rate(self, key, percent, actor_id, now=None, conn=None):
        if resolve_user_role(actor_id, self.db.users.get_user(str(actor_id))) != "admin":
            return False, "❌ 금리 설정은 관리자 전용입니다."
        if key not in self.KEYS:
            return False, "❌ 지원하지 않는 금리입니다."
        try:
            rate = float(percent)
            low = -100 if key == "base_rate" else 0
            if isinstance(percent, bool) or not math.isfinite(rate) or not low <= rate <= 100:
                raise ValueError(f"{self.KEYS[key]}는 {low}~100%여야 합니다.")
            now = float(time.time() if now is None else now)
            if not math.isfinite(now) or now < 0:
                raise ValueError("적용 시각은 유효한 UNIX 시각이어야 합니다.")
            with (self.db.atomic(("system",)) if conn is None else nullcontext(conn)) as conn:
                conn.execute("INSERT INTO system.settings (key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(rate)))
            return True, f"✅ {self.KEYS[key]}를 연 {rate:g}%로 설정했습니다. 변경 시점 이후부터 적용됩니다."
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f"❌ {exc}"

    def set_country_rate(self, key, nationality, percent, actor_id, now=None, conn=None):
        """Set one country's rate without changing any other nationality."""
        if resolve_user_role(actor_id, self.db.users.get_user(str(actor_id))) != "admin":
            return False, "❌ 금리 설정은 관리자 전용입니다."
        if key not in self.KEYS:
            return False, "❌ 지원하지 않는 금리입니다."
        try:
            rate = float(percent)
            low = -100 if key == "base_rate" else 0
            if isinstance(percent, bool) or not math.isfinite(rate) or not low <= rate <= 100:
                raise ValueError(f"{self.KEYS[key]}는 {low}~100%여야 합니다.")
            wall_now = time.time()
            now = float(wall_now if now is None else now)
            if not math.isfinite(now) or now < 0:
                raise ValueError("적용 시각은 유효한 UNIX 시각이어야 합니다.")
            country = str(nationality or "").strip()
            if not country:
                raise ValueError("국적을 입력해주세요.")
            with (self.db.atomic(("system",)) if conn is None else nullcontext(conn)) as conn:
                updated = conn.execute(
                    f"UPDATE country_currencies SET {key}=? WHERE nationality=?",
                    (rate, country),
                )
                if updated.rowcount != 1:
                    raise ValueError("통화와 연결된 국적을 확인해주세요.")
                # The SQL-browser trigger records ordinary direct edits.  This
                # managed path replaces that wall-clock row with the caller's
                # precise effective timestamp (important for deterministic jobs/tests).
                conn.execute(
                    "DELETE FROM country_rate_history WHERE nationality=? AND rate_key=? "
                    "AND annual_percent=? AND changed_by='sql' AND effective_epoch BETWEEN ? AND ?",
                    (country, key, rate, wall_now-2, wall_now+2),
                )
                conn.execute(
                    "INSERT INTO country_rate_history"
                    "(nationality,rate_key,annual_percent,effective_epoch,changed_by) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(nationality,rate_key,effective_epoch) "
                    "DO UPDATE SET annual_percent=excluded.annual_percent,changed_by=excluded.changed_by",
                    (country, key, rate, now, str(actor_id)),
                )
                if key == "base_rate":
                    conn.execute(
                        "DELETE FROM market_rate_curve_history WHERE nationality=? "
                        "AND changed_by='sql' AND effective_epoch BETWEEN ? AND ?",
                        (country, wall_now-2, wall_now+2),
                    )
                    conn.execute(
                        "UPDATE market_rate_curve SET updated_epoch=? WHERE nationality=?",
                        (now, country),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO market_rate_curve_history "
                        "SELECT nationality,benchmark_key,tenor_days,annual_rate,?,? "
                        "FROM market_rate_curve WHERE nationality=?",
                        (now, str(actor_id), country),
                    )
                if key in {"base_rate", "legal_max_rate"}:
                    self.reprice_floating_products(conn, country)
            suffix = (" 기존 초과 상품 이율은 새 상한으로 조정했습니다."
                      if key == "legal_max_rate" else "")
            return True, (
                f"✅ {country} {self.KEYS[key]}를 연 {rate:g}%로 설정했습니다. "
                f"연동된 변동금리 상품도 재산정했습니다.{suffix}"
            )
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f"❌ {exc}"

    @staticmethod
    def legal_max_for_currency(conn, currency_code):
        """Return the strictest country cap for a currency shared by countries."""
        rows = conn.execute(
            "SELECT nationality, legal_max_rate FROM country_currencies "
            "WHERE currency_code=? ORDER BY nationality",
            (str(currency_code or "NWX").upper(),),
        ).fetchall()
        if rows:
            row = min(rows, key=lambda item: float(item["legal_max_rate"]))
            return float(row["legal_max_rate"]), row["nationality"]
        fallback = RateManager.from_connection(conn, "legal_max_rate")
        return fallback, "기본"

    @staticmethod
    def legal_max_for_exchange(conn, exchange_id, quote_currency="NWX"):
        row = conn.execute(
            "SELECT c.nationality, c.legal_max_rate FROM market_exchanges e "
            "JOIN country_currencies c ON c.nationality=e.nationality "
            "WHERE e.exchange_id=?",
            (str(exchange_id or ""),),
        ).fetchone()
        if row:
            return float(row["legal_max_rate"]), row["nationality"]
        return RateManager.legal_max_for_currency(conn, quote_currency)

    @staticmethod
    def require_legal_rate(conn, annual_rate, *, nationality=None,
                           currency_code=None, exchange_id=None):
        """Validate an interest-bearing product rate against its country cap."""
        rate = float(annual_rate)
        if not math.isfinite(rate) or rate < 0:
            raise ValueError("상품 연이율은 0 이상의 유한한 숫자여야 합니다.")
        if nationality is not None:
            cap = RateManager.from_connection(
                conn, "legal_max_rate", str(nationality), require_country=True
            )
            country = str(nationality)
        elif exchange_id is not None:
            cap, country = RateManager.legal_max_for_exchange(
                conn, exchange_id, currency_code or "NWX"
            )
        else:
            cap, country = RateManager.legal_max_for_currency(
                conn, currency_code or "NWX"
            )
        if rate > cap:
            raise ValueError(
                f"상품 연이율 {rate:g}%는 {country} 법정최고이자 {cap:g}%를 넘을 수 없습니다."
            )
        return rate

    @staticmethod
    def rate_at(conn, key, nationality, epoch):
        """Return the policy rate that was effective at a historical instant."""
        row = conn.execute(
            "SELECT annual_percent FROM country_rate_history "
            "WHERE nationality=? AND rate_key=? AND effective_epoch<=? "
            "ORDER BY effective_epoch DESC LIMIT 1",
            (str(nationality), key, float(epoch)),
        ).fetchone()
        if row:
            return float(row["annual_percent"])
        return RateManager.from_connection(
            conn, key, str(nationality), require_country=True
        )

    @staticmethod
    def market_rate(conn, nationality, benchmark_key="policy", tenor_days=0, *, epoch=None):
        """Return a policy or interpolated zero rate for one maturity.

        Curve points are continuously-compounded zero rates.  Between quoted
        maturities we interpolate log discount factors instead of percentages;
        this keeps discount factors positive and prevents internally
        inconsistent present values.
        """
        country = str(nationality or "").strip()
        key = str(benchmark_key or "policy").strip().lower()
        if not country:
            raise ValueError("시장금리 조회에는 국적이 필요합니다.")
        if key == "policy":
            return (RateManager.rate_at(conn, "base_rate", country, epoch)
                    if epoch is not None else RateManager.from_connection(
                        conn, "base_rate", country, require_country=True
                    ))
        if key not in MARKET_RATE_NAMES:
            raise ValueError("기준지표는 policy/funding/bank_bond/treasury 중 하나여야 합니다.")
        try:
            target = max(1, int(tenor_days or 1))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("기준지표 만기는 1일 이상의 정수여야 합니다.")
        if epoch is None:
            rows = conn.execute(
                "SELECT tenor_days,annual_rate FROM market_rate_curve "
                "WHERE nationality=? AND benchmark_key=? ORDER BY tenor_days",
                (country, key),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT h.tenor_days,h.annual_rate FROM market_rate_curve_history h "
                "WHERE h.nationality=? AND h.benchmark_key=? AND h.effective_epoch=("
                "SELECT MAX(h2.effective_epoch) FROM market_rate_curve_history h2 "
                "WHERE h2.nationality=h.nationality AND h2.benchmark_key=h.benchmark_key "
                "AND h2.tenor_days=h.tenor_days AND h2.effective_epoch<=?) "
                "ORDER BY h.tenor_days",
                (country, key, float(epoch)),
            ).fetchall()
        if not rows:
            # A newly created country's curve can briefly be absent during a
            # migration; policy is the safe observable fallback.
            return RateManager.market_rate(conn, country, "policy", target, epoch=epoch)
        points = [(int(row["tenor_days"]), float(row["annual_rate"])) for row in rows]
        if target <= points[0][0]:
            return points[0][1]
        if target >= points[-1][0]:
            return points[-1][1]
        for (left_days, left_rate), (right_days, right_rate) in zip(points, points[1:]):
            if left_days <= target <= right_days:
                weight = (target-left_days)/(right_days-left_days)
                left_time, right_time = left_days / 365.0, right_days / 365.0
                target_time = target / 365.0
                left_log_discount = -(left_rate / 100.0) * left_time
                right_log_discount = -(right_rate / 100.0) * right_time
                target_log_discount = (
                    left_log_discount
                    + (right_log_discount-left_log_discount)*weight
                )
                return -target_log_discount / target_time * 100.0
        return points[-1][1]

    @staticmethod
    def discount_factor(conn, nationality, benchmark_key, tenor_days, *, epoch=None,
                        spread=0.0):
        """Discount one cash flow from the matching zero-rate curve."""
        days = max(0.0, float(tenor_days or 0))
        if days == 0:
            return 1.0
        rate = RateManager.market_rate(
            conn, nationality, benchmark_key, max(1, math.ceil(days)), epoch=epoch
        ) + float(spread or 0)
        exponent = -(rate / 100.0) * days / 365.0
        if exponent > 700:
            raise OverflowError("할인계수 계산 결과가 너무 큽니다.")
        return math.exp(exponent)

    @staticmethod
    def for_exchange_market(conn, exchange_id, tenor_days, benchmark_key="treasury", *, epoch=None):
        row = conn.execute(
            "SELECT nationality FROM market_exchanges WHERE exchange_id=?",
            (str(exchange_id or ""),),
        ).fetchone()
        country = row["nationality"] if row and row["nationality"] else "낙원"
        return RateManager.market_rate(
            conn, country, benchmark_key, tenor_days, epoch=epoch
        )

    @staticmethod
    def institution_components(conn, issuer):
        row = conn.execute(
            "SELECT funding_spread,operating_cost_spread,liquidity_spread,target_margin,"
            "competition_adjustment,equity_capital,target_capital_ratio,"
            "target_loan_deposit_ratio,funding_pressure_sensitivity,"
            "capital_pressure_sensitivity,competition_sensitivity "
            "FROM financial_institutions WHERE issuer=?",
            (str(issuer or ""),),
        ).fetchone()
        defaults = {
            "funding_spread": 0.0, "operating_cost_spread": 0.5,
            "liquidity_spread": 0.2, "target_margin": 1.0,
            "competition_adjustment": 0.0,
            "equity_capital": 1_000_000.0, "target_capital_ratio": 0.10,
            "target_loan_deposit_ratio": 0.90,
            "funding_pressure_sensitivity": 0.50,
            "capital_pressure_sensitivity": 1.00,
            "competition_sensitivity": 0.25,
        }
        return {
            key: float(row[key] if row and row[key] is not None else default)
            for key, default in defaults.items()
        }

    @staticmethod
    def institution_balance_sheet(conn, issuer):
        """Return a compact, live balance-sheet view used for contract pricing."""
        issuer = str(issuer or "")
        deposits = conn.execute(
            "SELECT COALESCE(SUM(a.balance),0) amount FROM accounts a "
            "JOIN saving p ON p.product_code=a.product_code "
            "WHERE p.issuer=? AND a.account_type IN ('saving','currency_saving')",
            (issuer,),
        ).fetchone()
        loans = conn.execute(
            "SELECT COALESCE(SUM(l.outstanding_balance),0) amount FROM loans l "
            "JOIN liability p ON p.product_code=l.product_code "
            "WHERE p.issuer=? AND l.status IN ('active','defaulted')",
            (issuer,),
        ).fetchone()
        total_deposits = conn.execute(
            "SELECT COALESCE(SUM(balance),0) amount FROM accounts "
            "WHERE account_type IN ('saving','currency_saving')"
        ).fetchone()
        total_loans = conn.execute(
            "SELECT COALESCE(SUM(outstanding_balance),0) amount FROM loans "
            "WHERE status IN ('active','defaulted')"
        ).fetchone()
        return {
            "deposits": max(0.0, float(deposits["amount"] or 0)),
            "loans": max(0.0, float(loans["amount"] or 0)),
            "total_deposits": max(0.0, float(total_deposits["amount"] or 0)),
            "total_loans": max(0.0, float(total_loans["amount"] or 0)),
        }

    @staticmethod
    def dynamic_institution_adjustment(conn, issuer, product_kind):
        """Translate funding, capital and competitive pressure into a spread."""
        terms = RateManager.institution_components(conn, issuer)
        sheet = RateManager.institution_balance_sheet(conn, issuer)
        deposits, loans = sheet["deposits"], sheet["loans"]
        loan_deposit = loans / max(1.0, deposits)
        funding_pressure = max(
            0.0, loan_deposit-float(terms["target_loan_deposit_ratio"])
        ) * float(terms["funding_pressure_sensitivity"])
        risk_weighted_assets = loans * 0.75
        capital_ratio = (float(terms["equity_capital"])/risk_weighted_assets
                         if risk_weighted_assets > 0 else 1.0)
        capital_pressure = max(
            0.0, float(terms["target_capital_ratio"])-capital_ratio
        ) * 100.0 * float(terms["capital_pressure_sensitivity"])
        total = sheet["total_loans"] if product_kind == "loan" else sheet["total_deposits"]
        own = loans if product_kind == "loan" else deposits
        market_share = own / total if total > 0 else 0.0
        competition = float(terms["competition_sensitivity"])*(1.0-market_share)
        if product_kind == "deposit":
            # Funding pressure and weak deposit share make funding more valuable.
            return min(10.0, funding_pressure+competition)
        # Loan margins widen under funding/capital pressure, while competition
        # for borrowers partially offsets the widening.
        return max(-10.0, min(25.0, funding_pressure+capital_pressure-competition))

    @staticmethod
    def product_spread(conn, product, product_kind):
        """Combine product and institution costs without borrower-specific risk."""
        def value(key, default=0):
            try:
                found = product[key]
            except (KeyError, IndexError):
                found = default
            return default if found is None else found

        institution = RateManager.institution_components(conn, value("issuer", ""))
        total = (float(value("base_rate_spread"))
                 + float(value("maturity_spread"))
                 + float(value("promotional_adjustment"))
                 + institution["competition_adjustment"])
        if product_kind == "loan":
            total += (institution["funding_spread"] + institution["operating_cost_spread"]
                      + institution["liquidity_spread"] + institution["target_margin"])
        total += RateManager.dynamic_institution_adjustment(
            conn, value("issuer", ""), product_kind
        )
        return total

    @staticmethod
    def effective_annual_rate(nominal_percent, cycle_seconds=YEAR_SECONDS):
        nominal = float(nominal_percent)/100.0
        cycle = float(cycle_seconds or YEAR_SECONDS)
        if not math.isfinite(nominal) or not math.isfinite(cycle) or cycle <= 0:
            raise ValueError("유효금리 산정값을 확인해주세요.")
        periodic = nominal*cycle/YEAR_SECONDS
        if periodic <= -1:
            return -100.0
        periods = YEAR_SECONDS/cycle
        return math.expm1(periods*math.log1p(periodic))*100

    @staticmethod
    def annual_percentage_rate(nominal_percent, fee_amount, proceeds, maturity_days,
                               *, cashflows=None, principal=None):
        """Return cash-flow IRR/APR rather than an additive fee approximation.

        ``cashflows`` is an iterable of ``(days_from_disbursement, payment)``.
        When omitted, a bullet repayment is inferred for backward compatibility.
        """
        nominal = float(nominal_percent)
        fee = float(fee_amount or 0)
        net = float(proceeds or 0)
        days = float(maturity_days or 365)
        if net <= 0 or days <= 0:
            return nominal
        if cashflows is None:
            gross = float(principal if principal is not None else net+fee)
            payment = gross*(1.0+nominal/100.0*days/365.0)
            cashflows = ((days, payment),)
        flows = [(float(day), float(payment)) for day, payment in cashflows
                 if float(day) > 0 and float(payment) > 0]
        if not flows:
            return nominal

        def present_value(rate):
            log_base = math.log1p(rate)
            return sum(payment*math.exp(-log_base*day/365.0)
                       for day, payment in flows)-net

        low, high = -0.999999, 1.0
        try:
            low_value, high_value = present_value(low), present_value(high)
            while high_value > 0 and high < 1_000_000:
                high = high*2+1
                high_value = present_value(high)
            if low_value < 0 or high_value > 0:
                return nominal
            for _ in range(120):
                middle = (low+high)/2
                if present_value(middle) > 0:
                    low = middle
                else:
                    high = middle
            return ((low+high)/2)*100.0
        except (OverflowError, ValueError):
            return nominal

    def set_market_rate(self, nationality, benchmark_key, tenor_days, percent, actor_id,
                        now=None, conn=None):
        if resolve_user_role(actor_id, self.db.users.get_user(str(actor_id))) != "admin":
            return False, "❌ 시장금리 설정은 관리자 전용입니다."
        try:
            key = str(benchmark_key or "").strip().lower()
            if key not in MARKET_RATE_NAMES:
                raise ValueError("기준지표는 funding/bank_bond/treasury 중 하나여야 합니다.")
            tenor, rate = int(tenor_days), float(percent)
            when = float(time.time() if now is None else now)
            if tenor <= 0 or not math.isfinite(rate) or not -100 <= rate <= 100:
                raise ValueError("만기는 양의 정수, 시장금리는 -100~100%여야 합니다.")
            with (self.db.atomic(("system", "product")) if conn is None else nullcontext(conn)) as db_conn:
                updated = db_conn.execute(
                    "UPDATE market_rate_curve SET annual_rate=?,updated_epoch=? "
                    "WHERE nationality=? AND benchmark_key=? AND tenor_days=?",
                    (rate, when, str(nationality), key, tenor),
                )
                if updated.rowcount != 1:
                    raise ValueError("해당 국가·지표·만기 구간을 찾을 수 없습니다.")
                db_conn.execute(
                    "INSERT OR REPLACE INTO market_rate_curve_history VALUES (?,?,?,?,?,?)",
                    (str(nationality), key, tenor, rate, when, str(actor_id)),
                )
                self.reprice_floating_products(db_conn, str(nationality))
            return True, f"✅ {nationality} {MARKET_RATE_NAMES[key]} {tenor}일물을 연 {rate:g}%로 설정했습니다."
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f"❌ {exc}"

    @staticmethod
    def floating_rate(conn, nationality, spread=0, floor=0, cap=None, *, epoch=None,
                      benchmark_key="policy", tenor_days=0, apply_legal_max=True):
        """Price a floating contract from its named benchmark and reset terms."""
        country = str(nationality or "").strip()
        if not country:
            raise ValueError("변동금리 상품에는 기준금리 적용 국적이 필요합니다.")
        base = RateManager.market_rate(
            conn, country, benchmark_key, tenor_days, epoch=epoch
        )
        legal = (RateManager.rate_at(conn, "legal_max_rate", country, epoch)
                 if epoch is not None else RateManager.from_connection(
                     conn, "legal_max_rate", country, require_country=True
                 ))
        spread = float(spread or 0)
        floor = -100.0 if floor is None else float(floor)
        ceiling = 100.0 if cap is None else float(cap)
        if apply_legal_max:
            ceiling = min(legal, ceiling)
        values = (base, legal, spread, floor, ceiling)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("금리 산정값은 유한한 숫자여야 합니다.")
        if (not -100 <= spread <= 100 or not -100 <= floor <= 100
                or not 0 <= ceiling <= 100):
            raise ValueError("금리 스프레드·하한·상한 설정을 확인해주세요.")
        if floor > ceiling:
            raise ValueError("상품 금리 하한은 적용 상한보다 클 수 없습니다.")
        return min(ceiling, max(floor, base + spread))

    @staticmethod
    def reprice_floating_products(conn, nationality):
        """Refresh advertised rates; signed contracts reset on their own schedule."""
        country = str(nationality)
        for table in ("saving", "liability"):
            rows = conn.execute(
                f"SELECT * "
                f"FROM {table} WHERE rate_type='floating' AND rate_nationality=?",
                (country,),
            ).fetchall()
            for row in rows:
                rate = RateManager.floating_rate(
                    conn, country, RateManager.product_spread(
                        conn, row, "deposit" if table == "saving" else "loan"
                    ), (None if table == "saving" and row["negative_rate_allowed"]
                        else row["rate_floor"]), row["rate_cap"],
                    benchmark_key=row["benchmark_key"],
                    tenor_days=row["benchmark_tenor_days"] or row["maturity_days"],
                    apply_legal_max=table == "liability",
                )
                conn.execute(
                    f"UPDATE {table} SET interest_rate=? WHERE product_code=?",
                    (rate, row["product_code"]),
                )

    @staticmethod
    def interest_between(conn, key, principal, start, end, spread=0, compound=False,
                         nationality=None):
        """Integrate dated annual rates; never apply a new rate to old elapsed time."""
        if end <= start or principal <= 0:
            return 0.0
        rate = RateManager.from_connection(conn, key, nationality)
        return float(principal) * (rate + spread) / 100 * (end - start) / YEAR_SECONDS
