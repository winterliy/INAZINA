"""Typed game products: stock futures, corporate debt, NAV funds and physical spot."""

import math
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from database.user_db import TREASURY_USER_ID
from functions.admin_access import resolve_user_role
from functions.money import round_money
from functions.interest_calc import year_fraction
from managers.rate_manager import RateManager

FUND_TYPES = {"mutual_fund", "index_fund"}
FUND_CATEGORIES = {"equity", "bond", "mixed", "other"}
FUND_CATEGORY_NAMES = {
    "equity": "주식형", "bond": "채권형", "mixed": "혼합형", "other": "기타형",
}
PRODUCT_NAMES = {"stock_future": "주식 선물", "corporate_bond": "회사채", "mutual_fund": "일반 펀드",
                 "index_fund": "인덱스 펀드", "commodity_spot": "현물 원자재"}


class InvestmentManager:
    def __init__(self, db, user_manager):
        self.db, self.users = db, user_manager

    @staticmethod
    def normalize_fund_category(value):
        category = str(value or "").strip().lower()
        aliases = {
            "stock": "equity", "주식": "equity", "주식형": "equity",
            "채권": "bond", "채권형": "bond",
            "혼합": "mixed", "혼합형": "mixed",
            "기타": "other", "기타형": "other",
        }
        category = aliases.get(category, category)
        if category and category not in FUND_CATEGORIES:
            raise ValueError("펀드 유형은 equity/bond/mixed/other 중 하나여야 합니다.")
        return category or None

    @staticmethod
    def component_quote(conn, asset_code, *, active_only=True):
        """Resolve one fund component across the repository's asset tables."""
        code = str(asset_code).strip()
        status_clause = " AND status='active'" if active_only else ""
        row = conn.execute(
            "SELECT company_code code,share_price price,status,quote_currency "
            f"FROM company.company WHERE company_code=?{status_clause}", (code,),
        ).fetchone()
        if row:
            return dict(row, asset_source="company", asset_class="equity")
        row = conn.execute(
            "SELECT product_code code,unit_price price,status,quote_currency "
            "FROM product.investment WHERE product_code=? AND product_type='corporate_bond'" + status_clause,
            (code,),
        ).fetchone()
        if row:
            return dict(row, asset_source="investment", asset_class="bond")
        row = conn.execute(
            "SELECT product_code code,market_price price,status,quote_currency "
            "FROM product.treasury_bond_products WHERE product_code=?" + status_clause,
            (code,),
        ).fetchone()
        if row:
            return dict(row, asset_source="treasury_bond", asset_class="bond")
        row = conn.execute(
            "SELECT code,price,status,quote_currency,type FROM assets.market_assets "
            f"WHERE code=?{status_clause}", (code,),
        ).fetchone()
        if row:
            asset_class = {
                "stock": "equity", "주식": "equity",
                "commodity_spot": "commodity", "real_estate": "real_estate",
                "special_asset": "special_asset",
            }.get(str(row["type"]).lower())
            if asset_class:
                return dict(row, asset_source="market_asset", asset_class=asset_class)
        return None

    @classmethod
    def validate_fund_components(cls, conn, weights, requested_category=None):
        if not isinstance(weights, dict) or not weights:
            raise ValueError("components에 종목별 비중을 입력해주세요.")
        parsed, totals = [], {"equity": Decimal(0), "bond": Decimal(0), "other": Decimal(0)}
        total = Decimal(0)
        for asset_code, raw_weight in weights.items():
            try:
                if isinstance(raw_weight, bool):
                    raise InvalidOperation()
                weight = Decimal(str(raw_weight))
            except (InvalidOperation, ValueError, TypeError):
                raise ValueError("펀드 구성 비중은 양의 숫자여야 합니다.") from None
            if not weight.is_finite() or weight <= 0 or weight > 1:
                raise ValueError("펀드 구성 비중은 0보다 크고 1 이하여야 합니다.")
            quote = cls.component_quote(conn, asset_code)
            if not quote or float(quote["price"] or 0) <= 0:
                raise ValueError(f"활성 펀드 구성 자산을 확인해주세요: {asset_code}")
            if str(quote.get("quote_currency") or "NWX").upper() != "NWX":
                raise ValueError("펀드 구성 자산은 현재 NWX 결제 자산만 지원합니다.")
            bucket = quote["asset_class"] if quote["asset_class"] in {"equity", "bond"} else "other"
            totals[bucket] += weight
            total += weight
            parsed.append((str(asset_code), float(weight), float(quote["price"]),
                           quote["asset_source"], quote["asset_class"]))
        if abs(total - Decimal(1)) > Decimal("0.00000001"):
            raise ValueError("펀드 구성 비중의 합은 1이어야 합니다.")
        if totals["equity"] >= Decimal("0.60"):
            inferred = "equity"
        elif totals["bond"] >= Decimal("0.60"):
            inferred = "bond"
        elif (totals["equity"] > 0 and totals["bond"] > 0
              and totals["equity"] + totals["bond"] >= Decimal("0.60")):
            inferred = "mixed"
        else:
            inferred = "other"
        requested = cls.normalize_fund_category(requested_category)
        if requested and requested != inferred:
            raise ValueError(
                f"구성 비중은 {FUND_CATEGORY_NAMES[inferred]}이며 "
                f"{FUND_CATEGORY_NAMES[requested]} 조건을 충족하지 않습니다."
            )
        return inferred, parsed

    def issue(self, actor_id, code, name, product_type, price, supply, exchange_id,
              underlying_code=None, maturity_days=0, annual_rate=0, contract_size=1):
        if resolve_user_role(actor_id, self.db.users.get_user(str(actor_id))) != "admin":
            return False, "❌ 관리자 전용 기능입니다."
        code, name, exchange_id = str(code).strip(), str(name).strip(), str(exchange_id).strip()
        if not code or len(code) > 60 or code.startswith("__exchange_index__:") or not name or not exchange_id:
            return False, "❌ 코드(60자 이하), 상품명, 거래소를 입력해주세요."
        if product_type not in PRODUCT_NAMES:
            return False, "❌ 지원하지 않는 상품 종류입니다."
        if any(isinstance(x, bool) or not isinstance(x, int) or x <= 0 for x in (price, supply, contract_size)):
            return False, "❌ 가격·발행량·계약승수는 양의 정수여야 합니다."
        if not isinstance(maturity_days, int) or isinstance(maturity_days, bool) or not 0 <= maturity_days <= 3650:
            return False, "❌ 만기 일수는 0~3650 사이 정수여야 합니다."
        try:
            rate = float(annual_rate)
            if not math.isfinite(rate) or not 0 <= rate <= 100:
                raise ValueError()
        except (ValueError, TypeError):
            return False, "❌ 연이율은 0~100%여야 합니다."
        if price * supply * contract_size > 2**63 - 1:
            return False, "❌ 상품의 총 명목금액이 너무 큽니다."
        if product_type in {"stock_future", "corporate_bond"} and maturity_days <= 0:
            return False, "❌ 선물·회사채는 만기 일수를 입력해주세요."
        if product_type in FUND_TYPES and maturity_days != 0:
            return False, "❌ 펀드는 만기일수 0으로 등록해주세요."
        now = int(time.time())
        try:
            with self.db.atomic(("users", "accounts", "assets", "product", "company")) as conn:
                if self.db.code_locations(conn, code):
                    return False, "❌ 이미 사용 중인 코드입니다."
                if product_type == "commodity_spot":
                    conn.execute("INSERT INTO assets.market_assets (code, name, exchange_id, price, type, total_supply) "
                                 "VALUES (?, ?, ?, ?, 'commodity_spot', ?)", (code, name, exchange_id, price, supply))
                    return True, f"✅ 현물 원자재 `{code}`를 상장했습니다. 1개당 {price:,} NWX"
                underlying, issuer, index_id = None, None, None
                constituents = []
                if product_type == "stock_future":
                    underlying, _ = self.users._asset_row(conn, str(underlying_code or "").strip())
                    if not underlying or underlying["type"] not in {"stock", "주식"} or underlying["status"] != "active":
                        return False, "❌ 주식 선물의 기초에는 거래 중인 주식 코드를 입력해주세요."
                    price = int(underlying["price"])
                elif product_type == "corporate_bond":
                    issuer = conn.execute("SELECT * FROM company.company WHERE company_code = ? AND status = 'active'",
                                          (str(underlying_code or "").strip(),)).fetchone()
                    if not issuer:
                        return False, "❌ 회사채의 기초에는 발행 회사 코드를 입력해주세요."
                else:
                    index_id = str(underlying_code or exchange_id).strip()
                    constituents = conn.execute("SELECT company_code, share_price, total_shares FROM company.company "
                        "WHERE exchange_id = ? AND status = 'active' AND share_price > 0 AND total_shares > 0", (index_id,)).fetchall()
                    if not constituents:
                        return False, "❌ 펀드의 기초에는 구성 주식이 있는 거래소 ID를 입력해주세요."
                expiry = now + maturity_days * 86400 if maturity_days else None
                conn.execute("INSERT INTO product.investment "
                    "(product_code, name, product_type, total_supply, unit_price, maturity_days, manager, exchange_id, "
                    "underlying_code, issuer_code, maturity_epoch, face_value, annual_rate, contract_size, "
                    "nav_anchor, underlying_exchange_id, fund_category) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (code, name, product_type, supply, price, maturity_days, str(actor_id), exchange_id,
                     underlying["code"] if underlying else None, issuer["company_code"] if issuer else None,
                     expiry, price if issuer else 0, rate, contract_size, price,
                     index_id if product_type == "index_fund" else None,
                     "equity" if product_type in FUND_TYPES else None))
                if product_type == "mutual_fund":
                    total_cap = sum(float(row["share_price"]) * int(row["total_shares"]) for row in constituents)
                    for row in constituents:
                        weight = float(row["share_price"]) * int(row["total_shares"]) / total_cap
                        conn.execute("INSERT INTO product.fund_components "
                            "(product_code,asset_code,weight,base_price,asset_source,asset_class) "
                            "VALUES (?, ?, ?, ?, 'company', 'equity')",
                            (code, row["company_code"], weight, float(row["share_price"])))
                if product_type in FUND_TYPES:
                    from managers.fund_manager import FundManager
                    FundManager.create_empty(conn, code)
            return True, f"✅ {PRODUCT_NAMES[product_type]} `{code}`를 등록했습니다. " + (
                "/매수·/매도로 지정가 없이 NAV 기준 거래합니다." if product_type in FUND_TYPES
                else "거래소 매수·매도로 거래합니다.")
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f"❌ 상품 등록 실패: {exc}"

    @staticmethod
    def linked_price(conn, product, base_rate, now, *, prices=None, fund_components=None):
        kind = product["product_type"]
        if kind in FUND_TYPES:
            from managers.fund_manager import FundManager
            state = FundManager.snapshot_conn(conn, product['product_code'])
            if state['mode'] == 'backed':
                return max(1, round_money(state['nav']))
        if kind == "stock_future":
            expiry = int(product["maturity_epoch"] or now)
            if now >= expiry:
                cached = conn.execute(
                    "SELECT price, recorded_epoch, is_final FROM assets.market_future_expiry_prices "
                    "WHERE product_code = ? AND underlying_code = ? AND maturity_epoch = ?",
                    (product["product_code"], product["underlying_code"], expiry),
                ).fetchone()
                if cached and int(cached["is_final"]):
                    return max(1, int(cached["price"]))
                final = conn.execute(
                    "SELECT price, recorded_epoch FROM ("
                    "SELECT price, recorded_epoch, 1 AS source FROM assets.market_price_history "
                    "WHERE asset_code = ? AND recorded_epoch <= ? "
                    "UNION ALL SELECT price, recorded_epoch, 0 AS source "
                    "FROM assets.market_future_expiry_prices WHERE product_code = ? "
                    "AND underlying_code = ? AND maturity_epoch = ? AND recorded_epoch <= ?) "
                    "ORDER BY recorded_epoch DESC, source DESC LIMIT 1",
                    (product["underlying_code"], expiry, product["product_code"],
                     product["underlying_code"], expiry, expiry),
                ).fetchone()
                if final:
                    InvestmentManager._fix_future_expiry_price(
                        conn, product, expiry, int(final["price"]), int(final["recorded_epoch"])
                    )
                    return max(1, int(final["price"]))
            row = prices.get(product['underlying_code']) if prices is not None else conn.execute("SELECT share_price AS price FROM company.company WHERE company_code = ? "
                "UNION ALL SELECT price FROM assets.market_assets WHERE code = ?",
                (product["underlying_code"], product["underlying_code"])).fetchone()
            if not row or float(row["price"]) <= 0:
                return None
            if now >= expiry:
                price = max(1, round_money(float(row["price"])))
                InvestmentManager._fix_future_expiry_price(conn, product, expiry, price, expiry)
                return price
            remaining_days = max(1, math.ceil((int(product["maturity_epoch"] or now)-now)/86400))
            market_rate = RateManager.for_exchange_market(
                conn, product['exchange_id'], remaining_days, 'treasury', epoch=now
            )
            years = max(0, int(product["maturity_epoch"] or now) - now) / (365 * 86400)
            return max(1, round_money(float(row["price"]) * math.exp(market_rate / 100 * years)))
        if kind == "corporate_bond":
            remaining = max(0, int(product["maturity_epoch"] or now) - now)
            country = conn.execute(
                "SELECT nationality FROM market_exchanges WHERE exchange_id=?",
                (product["exchange_id"],),
            ).fetchone()
            quote = RateManager.bond_quote(
                conn, country["nationality"] if country else "낙원",
                int(product["face_value"]), float(product["annual_rate"]),
                remaining/86400,
                coupon_frequency_days=int(product["coupon_frequency_days"] or 0),
                spread=(float(product["credit_spread"] or 0)
                        + float(product["liquidity_spread"] or 0)),
                epoch=now,
                original_maturity_days=int(product["maturity_days"]),
                day_count_convention=product["day_count_convention"],
            )
            return max(1, round_money(quote["dirty_price"]))
        if kind == "mutual_fund":
            components = fund_components.get(product['product_code'], ()) if fund_components is not None else conn.execute("SELECT * FROM product.fund_components WHERE product_code = ?", (product["product_code"],)).fetchall()
            total, weight = 0.0, 0.0
            for component in components:
                row = prices.get(component['asset_code']) if prices is not None else InvestmentManager.component_quote(
                    conn, component["asset_code"], active_only=False
                )
                if not row or float(component["base_price"]) <= 0:
                    return None
                total += float(component["weight"]) * float(row["price"]) / float(component["base_price"])
                weight += float(component["weight"])
            return max(1, round_money(float(product["nav_anchor"]) * total / weight)) if weight > 0 else None
        return None

    @staticmethod
    def _fix_future_expiry_price(conn, product, expiry, price, observed_epoch):
        conn.execute(
            "INSERT INTO assets.market_future_expiry_prices "
            "(product_code, underlying_code, maturity_epoch, price, recorded_epoch, is_final) "
            "VALUES (?, ?, ?, ?, ?, 1) ON CONFLICT(product_code) DO UPDATE SET "
            "underlying_code = excluded.underlying_code, maturity_epoch = excluded.maturity_epoch, "
            "price = excluded.price, recorded_epoch = excluded.recorded_epoch, is_final = 1",
            (product["product_code"], product["underlying_code"], expiry, max(1, price), observed_epoch),
        )

    def trade_fund(self, user_id, code, quantity, redeem=False):
        product = self.db.products.get_investment(code)
        if not product or product["product_type"] not in FUND_TYPES:
            return False, "❌ 일반·인덱스 펀드 코드를 입력해주세요."
        if product['Fractal']:
            from managers.fractional_trading_manager import FractionalTradingManager
            from managers.order_book_manager import OrderBookManager
            return FractionalTradingManager(OrderBookManager(self.db,self.users)).trade_at_nav(str(user_id),str(code),quantity,sell=redeem)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 2**53 - 1:
            return False, "❌ 좌수는 1 이상의 정수여야 합니다."
        # UserManager's fund paths include NAV cash settlement with the game treasury.
        return (self.users.sell_asset if redeem else self.users.buy_asset)(str(user_id), str(code), quantity)

    def redeem_corporate_bonds(self, now=None):
        now = int(time.time() if now is None else now)
        self.pay_corporate_bond_coupons(now=now)
        notifications = []
        rows = self.db.products.execute("SELECT product_code FROM investment WHERE product_type = 'corporate_bond' "
            "AND status IN ('active', 'defaulted') AND maturity_epoch <= ?", (now,))
        for item in rows:
            with self.db.atomic(("users", "accounts", "assets", "product", "company", "tax")) as conn:
                product = conn.execute("SELECT * FROM product.investment WHERE product_code = ?", (item["product_code"],)).fetchone()
                if product["status"] not in {"active", "defaulted"}:
                    continue
                # Cancel resting orders before releasing the debt claim, including sell reservations.
                orders = conn.execute("SELECT * FROM assets.market_orders WHERE asset_code = ? AND order_kind = 'asset' "
                    "AND status IN ('open', 'partial')", (product["product_code"],)).fetchall()
                for order in orders:
                    if order["reserved_cash"]:
                        self.users._credit_source(conn, order["user_id"], order["funding_account"], int(order["reserved_cash"]))
                    conn.execute("UPDATE assets.market_orders SET status = 'expired', reserved_cash = 0, updated_epoch = ? WHERE order_id = ?",
                                 (now, order["order_id"]))
                holders = conn.execute("SELECT user_id, SUM(quantity) AS quantity FROM assets.user_assets "
                    "WHERE asset_code = ? GROUP BY user_id", (product["product_code"],)).fetchall()
                payments = []
                for holder in holders:
                    principal = int(holder["quantity"]) * int(product["face_value"])
                    coupon = (0 if int(product["coupon_frequency_days"] or 0) > 0 else
                              round_money(principal * float(product["annual_rate"]) / 100
                                          * int(product["maturity_days"]) / 365))
                    payments.append((holder["user_id"], int(holder["quantity"]), principal, coupon))
                due = sum(p + c for _, _, p, c in payments)
                cash_account=conn.execute('SELECT 1 FROM issuer_cash_accounts WHERE company_code=?',(product['issuer_code'],)).fetchone()
                if cash_account:
                    changed=conn.execute('UPDATE issuer_cash_accounts SET balance=balance-? WHERE company_code=? AND balance>=?',(due,product['issuer_code'],due))
                else:
                    changed = conn.execute("UPDATE company.company SET capital = capital - ? WHERE company_code = ? AND capital >= ?",
                                           (due, product["issuer_code"], due))
                if changed.rowcount != 1 and due:
                    conn.execute("UPDATE product.investment SET status = 'defaulted' WHERE product_code = ?", (product["product_code"],))
                    continue
                for user_id, quantity, principal, coupon in payments:
                    tax = self.users._tax(conn, user_id, "corporate_bond_interest", coupon)
                    self.users._credit_source(conn, user_id, None, principal + coupon - tax,
                                              event='bond_redemption', reference=product['product_code'])
                    notifications.append(dict(user_id=user_id, product_code=product["product_code"], net=principal + coupon - tax))
                conn.execute("DELETE FROM assets.user_assets WHERE asset_code = ?", (product["product_code"],))
                conn.execute("UPDATE product.investment SET status = 'redeemed' WHERE product_code = ?", (product["product_code"],))
        return notifications

    def pay_corporate_bond_coupons(self, now=None):
        """Pay due periodic coupons to the holders of record."""
        now = int(time.time() if now is None else now)
        notifications = []
        rows = self.db.products.execute(
            "SELECT product_code FROM investment WHERE product_type='corporate_bond' "
            "AND status='active' AND coupon_frequency_days>0",
        )
        for item in rows:
            with self.db.atomic(("users", "assets", "product", "company", "tax")) as conn:
                product = conn.execute(
                    "SELECT * FROM product.investment WHERE product_code=?",
                    (item["product_code"],),
                ).fetchone()
                if not product or product["status"] != "active":
                    continue
                frequency = int(product["coupon_frequency_days"] or 0)
                created = int(product["last_coupon_epoch"] or datetime.strptime(
                    product["created_at"], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc).timestamp())
                boundary = min(now, int(product["maturity_epoch"]))
                periods = max(0, (boundary-created)//(frequency*86400))
                if periods <= 0:
                    continue
                paid_until = created+periods*frequency*86400
                holders = conn.execute(
                    "SELECT user_id,SUM(quantity) quantity FROM assets.user_assets "
                    "WHERE asset_code=? GROUP BY user_id", (product["product_code"],)
                ).fetchall()
                due_total = 0
                payments = []
                for holder in holders:
                    principal = int(holder["quantity"])*int(product["face_value"])
                    coupon = round_money(
                        principal*float(product["annual_rate"])/100
                        * year_fraction(
                            periods*frequency*86400,
                            product["day_count_convention"], created,
                        )
                    )
                    payments.append((holder["user_id"], coupon))
                    due_total += coupon
                cash_account = conn.execute(
                    "SELECT 1 FROM issuer_cash_accounts WHERE company_code=?",
                    (product["issuer_code"],),
                ).fetchone()
                changed = (conn.execute(
                    "UPDATE issuer_cash_accounts SET balance=balance-? "
                    "WHERE company_code=? AND balance>=?",
                    (due_total, product["issuer_code"], due_total),
                ) if cash_account else conn.execute(
                    "UPDATE company.company SET capital=capital-? "
                    "WHERE company_code=? AND capital>=?",
                    (due_total, product["issuer_code"], due_total),
                ))
                if due_total and changed.rowcount != 1:
                    conn.execute(
                        "UPDATE product.investment SET status='defaulted' WHERE product_code=?",
                        (product["product_code"],),
                    )
                    continue
                for user_id, coupon in payments:
                    tax = self.users._tax(conn, user_id, "corporate_bond_interest", coupon)
                    self.users._credit_source(conn, user_id, None, coupon-tax,
                                              event='bond_coupon', reference=product['product_code'])
                    notifications.append({"user_id": str(user_id),
                                          "product_code": product["product_code"],
                                          "coupon": coupon, "tax": tax})
                conn.execute(
                    "UPDATE product.investment SET last_coupon_epoch=?,next_coupon_epoch=? "
                    "WHERE product_code=?",
                    (paid_until, paid_until+frequency*86400, product["product_code"]),
                )
        return notifications
