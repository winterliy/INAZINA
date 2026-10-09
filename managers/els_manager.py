"""Fully funded NWX step-down ELS for the Inazina game economy."""

import json
import math
import time
from contextlib import nullcontext
from decimal import Decimal

from functions.admin_access import resolve_user_role
from functions.money import parse_money, round_money
from database.user_db import TREASURY_USER_ID
from managers.market_session_manager import MarketSessionManager

DAY = 86400
MAX_MONEY = 2**53 - 1
INDEX_PREFIX = "__exchange_index__:"
OPEN_STATES = ("subscription", "active", "observation_pending", "redemption_pending")


class ELSManager:
    def __init__(self, db, user_manager=None):
        from managers.user_manager import UserManager
        self.db = db
        # Observation runs inside the market's write transaction. Creating a
        # UserManager there would run treasury migrations on another connection.
        self.users = user_manager or UserManager
        self.last_settlement_errors = []

    @staticmethod
    def _integer(value, label, low=1, high=MAX_MONEY):
        number = parse_money(value, minimum=low)
        if number > high:
            raise ValueError(f"{label}은 {low:,}~{high:,} 범위로 입력해주세요.")
        return number

    @staticmethod
    def _percent(value, label, *, positive=False):
        if isinstance(value, bool):
            raise ValueError(f"{label}은 숫자 %로 입력해주세요.")
        try:
            number = float(str(value).strip().removesuffix("%"))
        except (TypeError, ValueError):
            raise ValueError(f"{label}은 숫자 %로 입력해주세요.") from None
        if not math.isfinite(number) or not (0 < number <= 100 if positive else 0 <= number <= 100):
            raise ValueError(f"{label}은 {'0 초과' if positive else '0'}~100%로 입력해주세요.")
        return number

    @staticmethod
    def _list(value):
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, (list, tuple)):
            return list(value)
        raise ValueError("기초자산·평가일·상환기준은 쉼표 목록으로 입력해주세요.")

    @staticmethod
    def _require_admin(conn, actor):
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (str(actor),)).fetchone()
        if resolve_user_role(actor, user) != "admin":
            raise ValueError("관리자만 ELS를 발행·취소할 수 있습니다.")

    @staticmethod
    def _underlying(conn, code, prices=None):
        if code.startswith(INDEX_PREFIX):
            exchange = code[len(INDEX_PREFIX):]
            constituents = conn.execute("SELECT 1 FROM company WHERE exchange_id=? AND status='active' "
                "AND share_price>0 AND total_shares>0 LIMIT 1", (exchange,)).fetchone()
            if not constituents:
                return None
            row = conn.execute("SELECT index_value FROM market_exchange_indices WHERE exchange_id=?", (exchange,)).fetchone()
            name, value = f"{exchange} 거래소 지수", row["index_value"] if row else None
            if prices is not None and code in prices:
                cached = prices[code]
                value = cached.get("price", cached.get("index_value")) if isinstance(cached, dict) else cached
            return dict(code=code, name=name, kind="index", price=value)
        row = conn.execute("SELECT name, share_price, status FROM company WHERE company_code=?", (code,)).fetchone()
        if not row or row["status"] != "active":
            return None
        value = row["share_price"]
        if prices is not None:
            cached = prices.get(code)
            if cached is None:
                value = None
            else:
                try:
                    value = cached["price"]
                except (TypeError, KeyError, IndexError):
                    value = cached
        return dict(code=code, name=row["name"], kind="stock", price=value)

    @staticmethod
    def _valid_price(value):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) and number > 0 else None

    def create(self, actor, code, fields, conn=None):
        if conn is None:
            with self.db.atomic() as transaction:
                return self.create(actor, code, fields, conn=transaction)
        self._require_admin(conn, actor)
        code = str(code).strip()
        if not code or len(code) > 60 or code.startswith("__"):
            raise ValueError("ELS 코드는 60자 이하로 입력해주세요.")
        if self.db.code_locations(conn, code):
            raise ValueError("이미 사용 중인 상품 코드입니다.")
        name = str(fields.get("name", "")).strip()
        exchange = str(fields.get("exchange_id", "")).strip()
        if not name or not exchange:
            raise ValueError("상품명과 거래소 ID를 입력해주세요.")
        if str(fields.get("quote_currency", "NWX")).upper() != "NWX":
            raise ValueError("ELS는 NWX로만 발행할 수 있습니다.")
        face = self._integer(fields.get("face_value"), "액면가")
        supply = self._integer(fields.get("total_supply"), "발행좌수")
        maturity = self._integer(fields.get("maturity_days"), "만기일수", high=3650)
        subscription = self._integer(fields.get("subscription_days"), "모집일수", high=3650)
        coupon = self._percent(fields.get("annual_coupon"), "연 쿠폰")
        maturity_level = self._percent(fields.get("maturity_level"), "만기상환 기준", positive=True) / 100
        knock_in = self._percent(fields.get("knock_in_level"), "낙인 기준", positive=True) / 100
        if knock_in > maturity_level:
            raise ValueError("낙인 기준은 만기상환 기준 이하여야 합니다.")
        codes = [str(value).strip() for value in self._list(fields.get("underlyings"))]
        if not 1 <= len(codes) <= 3 or len(set(codes)) != len(codes):
            raise ValueError("기초자산은 서로 다른 주식·지수 1~3개를 입력해주세요.")
        infos = [self._underlying(conn, value) for value in codes]
        if any(info is None for info in infos):
            raise ValueError("기초자산에 거래 중인 주식 또는 거래소 지수 코드를 입력해주세요.")
        days = [self._integer(value, "평가일", high=maturity - 1)
                for value in self._list(fields.get("observation_days", []))]
        levels = [self._percent(value, "조기상환 기준", positive=True) / 100
                  for value in self._list(fields.get("redemption_levels", []))]
        if len(days) != len(levels) or days != sorted(set(days)):
            raise ValueError("조기평가일은 만기 전의 증가하는 날짜이며 상환기준과 개수가 같아야 합니다.")
        if any(a < b for a, b in zip(levels, levels[1:])):
            raise ValueError("스텝다운 상환기준은 뒤 회차가 앞 회차 이하여야 합니다.")
        issuer = str(fields.get("issuer_code") or TREASURY_USER_ID).strip()
        if issuer != TREASURY_USER_ID:
            company = conn.execute("SELECT share_class FROM company WHERE company_code=? AND status='active'", (issuer,)).fetchone()
            if not company or company["share_class"] == "preferred":
                raise ValueError("발행법인은 거래 중인 보통주 법인 코드를 입력해주세요.")
            conn.execute("INSERT OR IGNORE INTO issuer_cash_accounts(company_code,balance,locked_amount) VALUES (?,0,0)", (issuer,))
        total = face * supply
        if total + self._coupon(total, coupon, maturity) > MAX_MONEY:
            raise ValueError("원금과 최대 쿠폰 합계가 허용 금액을 초과합니다.")
        now = int(time.time())
        conn.execute("INSERT INTO investment(product_code,name,product_type,total_supply,unit_price,maturity_days,"
            "manager,exchange_id,status,is_fixed,issuer_code,face_value,annual_rate,quote_currency) "
            "VALUES (?,?,'els',?,?,?,?,?,'active',1,?,?,?,'NWX')",
            (code, name, supply, face, maturity, str(actor), exchange, issuer, face, coupon))
        conn.execute("INSERT INTO els_terms(product_code,issuer_code,face_value,annual_coupon,maturity_days,"
            "maturity_level,knock_in_level,subscription_end_epoch,created_epoch) VALUES (?,?,?,?,?,?,?,?,?)",
            (code, issuer, face, coupon, maturity, maturity_level, knock_in, now + subscription * DAY, now))
        conn.executemany("INSERT INTO els_underlyings(product_code,underlying_code,underlying_kind) VALUES (?,?,?)",
            [(code, info["code"], info["kind"]) for info in infos])
        conditions = [(code, i + 1, day, level, 0) for i, (day, level) in enumerate(zip(days, levels))]
        conditions.append((code, len(days) + 1, maturity, maturity_level, 1))
        conn.executemany("INSERT INTO els_observations(product_code,sequence,observation_day,redemption_level,is_maturity) "
            "VALUES (?,?,?,?,?)", conditions)
        conn.execute("INSERT INTO els_reserves(product_code,issuer_code) VALUES (?,?)", (code, issuer))
        return f"✅ ELS `{code}`를 등록했습니다. 액면 {face:,} NWX · 연 쿠폰 {coupon:g}% · 모집 {subscription}일"

    @staticmethod
    def _coupon(principal, annual_coupon, days):
        return round_money(Decimal(principal) * Decimal(str(annual_coupon)) * Decimal(days) / Decimal(36500))

    @staticmethod
    def _reserve_coupon(conn, issuer, coupon, total):
        from managers.financial_manager import FinancialManager as Finance
        if issuer != TREASURY_USER_ID:Finance.ensure(conn,issuer)
        if issuer == TREASURY_USER_ID:
            changed = conn.execute("UPDATE users SET cash=cash-? WHERE user_id=? AND cash>=?", (coupon, issuer, coupon))
        else:
            changed = conn.execute("UPDATE issuer_cash_accounts SET balance=balance-?,locked_amount=locked_amount+? "
                "WHERE company_code=? AND balance>=? AND locked_amount+?<=?",
                (coupon, total, issuer, coupon, total, MAX_MONEY))
        if changed.rowcount != 1:
            raise ValueError("발행자의 최대 쿠폰 예치 재원이 부족합니다.")

        if issuer != TREASURY_USER_ID:
            Finance.event(conn,issuer,'ELS 예치',[('cash',-coupon),('restricted_cash',total),('els_liability',coupon-total)],'investing',currency='NWX')

    @staticmethod
    def _release_reserve(conn, issuer, locked, refund, principal):
        from managers.financial_manager import FinancialManager as Finance
        if issuer != TREASURY_USER_ID:Finance.ensure(conn,issuer)
        if issuer == TREASURY_USER_ID:
            changed = conn.execute("UPDATE users SET cash=cash+? WHERE user_id=? AND cash+?<=?",
                (refund, issuer, refund, MAX_MONEY))
        else:
            changed = conn.execute("UPDATE issuer_cash_accounts SET balance=balance+?,locked_amount=locked_amount-? "
                "WHERE company_code=? AND locked_amount>=? AND balance+?<=?",
                (refund, locked, issuer, locked, refund, MAX_MONEY))
        if changed.rowcount != 1:
            raise ValueError("발행자 예치금 원장을 확인할 수 없어 정산을 보류합니다.")

        if issuer != TREASURY_USER_ID:
            Finance.event(conn,issuer,'ELS 예치 해제',[('cash',refund),('restricted_cash',-locked),('els_liability',principal),('other_expense',locked-refund-principal)],'investing',currency='NWX')

    def subscribe(self, user_id, code, quantity, account_number=None, now=None):
        user_id, code = str(user_id), str(code).strip()
        try:
            quantity = self._integer(quantity, "청약좌수")
            now = int(time.time() if now is None else now)
            self._settle_interest(user_id)
            with self.db.atomic() as conn:
                error = MarketSessionManager(self.db).trading_error(conn=conn, now=now)
                if error:
                    return False, error
                terms = conn.execute("SELECT t.*,i.total_supply FROM els_terms t JOIN investment i USING(product_code) "
                    "WHERE t.product_code=? AND i.status='active'", (code,)).fetchone()
                if not terms or terms["state"] != "subscription" or now >= terms["subscription_end_epoch"]:
                    return False, "❌ 모집 중인 ELS만 청약할 수 있습니다."
                user = conn.execute("SELECT status FROM users WHERE user_id=?", (user_id,)).fetchone()
                if not user or (user["status"] != "active" and user_id != TREASURY_USER_ID):
                    return False, "❌ 등록된 활성 사용자만 청약할 수 있습니다."
                sold = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM els_subscriptions "
                    "WHERE product_code=? AND status='subscribed'", (code,)).fetchone()[0]
                if sold + quantity > terms["total_supply"]:
                    return False, "❌ 남은 모집좌수보다 청약 수량이 많습니다."
                principal = quantity * terms["face_value"]
                coupon = self._coupon(principal, terms["annual_coupon"], terms["maturity_days"])
                if principal + coupon > MAX_MONEY:
                    return False, "❌ 청약금과 최대 쿠폰 합계가 허용 금액을 초과합니다."
                if account_number is None:
                    funds = self.users._choose_funds(conn, user_id)
                    if funds is None or funds[0] < principal:
                        return False, "❌ 청약금이 부족합니다."
                    account_number = funds[1]
                if not self.users._debit_source(conn, user_id, account_number, principal):
                    raise ValueError("청약금이 부족하거나 본인 계좌가 아닙니다.")
                self._reserve_coupon(conn, terms["issuer_code"], coupon, principal + coupon)
                changed = conn.execute("UPDATE els_reserves SET principal_amount=principal_amount+?,coupon_amount=coupon_amount+?,"
                    "locked_amount=locked_amount+? WHERE product_code=? AND locked_amount+?<=?",
                    (principal, coupon, principal + coupon, code, principal + coupon, MAX_MONEY))
                if changed.rowcount != 1:
                    raise ValueError("계약 총예치금이 허용 금액을 초과합니다.")
                conn.execute("INSERT INTO els_subscriptions(product_code,user_id,account_number,quantity,principal,"
                    "coupon_reserved,created_epoch) VALUES (?,?,?,?,?,?,?)",
                    (code, user_id, account_number, quantity, principal, coupon, now))
                holding = conn.execute("SELECT holding_id,quantity FROM user_assets WHERE user_id=? "
                    "AND asset_code=? AND account_number IS ?", (user_id, code, account_number)).fetchone()
                if holding:
                    conn.execute("UPDATE user_assets SET quantity=quantity+? WHERE holding_id=?", (quantity, holding["holding_id"]))
                else:
                    conn.execute("INSERT INTO user_assets(user_id,asset_code,account_number,quantity,average_cost) VALUES (?,?,?,?,?)",
                        (user_id, code, account_number, quantity, terms["face_value"]))
            return True, f"✅ ELS `{code}` {quantity:,}좌 청약 · 원금 {principal:,} NWX. 모집 종료 후 기준가가 확정됩니다."
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f"❌ ELS 청약 실패: {exc}"

    def _settle_interest(self, user_id):
        # Finish accrued savings before _debit_source advances its cursor. This
        # must happen outside the ELS transaction because interest has its own.
        has_interest = self.db.accounts.execute_one(
            "SELECT 1 FROM accounts WHERE user_id=? AND account_type='saving' "
            "UNION ALL SELECT 1 FROM loans WHERE user_id=? AND status='active' LIMIT 1",
            (user_id, user_id))
        if has_interest:
            from managers.interest_manager import InterestManager
            InterestManager(self.db).process_user_interests(user_id)

    def cancel(self, actor, code, now=None, conn=None):
        now = int(time.time() if now is None else now)
        if conn is not None:
            self._require_admin(conn, actor)
            terms = conn.execute("SELECT * FROM els_terms WHERE product_code=?", (code,)).fetchone()
            if not terms or terms["state"] != "subscription":
                return False, "❌ 발행 전 모집 중인 ELS만 취소할 수 있습니다."
            self._cancel(conn, terms, now)
            return True, f"✅ ELS `{code}` 발행을 취소하고 모든 청약금을 환불했습니다."
        try:
            with self.db.atomic() as conn:
                return self.cancel(actor, code, now=now, conn=conn)
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f"❌ ELS 발행 취소 실패: {exc}"

    def _safe_credit(self, conn, user_id, account, amount):
        row = conn.execute("SELECT balance FROM accounts WHERE account_number=? AND user_id=?", (account, user_id)).fetchone() if account else None
        if not row:
            account = None
            row = conn.execute("SELECT cash AS balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row or int(row["balance"]) + amount > MAX_MONEY:
            raise ValueError("수령 계정이 없거나 허용 잔액을 초과하여 정산을 보류합니다.")
        self.users._credit_source(conn, user_id, account, amount)

    def _cancel(self, conn, terms, now):
        code = terms["product_code"]
        reserve = conn.execute("SELECT * FROM els_reserves WHERE product_code=?", (code,)).fetchone()
        for subscription in conn.execute("SELECT * FROM els_subscriptions WHERE product_code=? AND status='subscribed'", (code,)).fetchall():
            self._safe_credit(conn, subscription["user_id"], None, subscription["principal"])
        self._release_reserve(conn, terms["issuer_code"], reserve["locked_amount"], reserve["coupon_amount"], reserve["principal_amount"])
        conn.execute("UPDATE els_subscriptions SET status='cancelled' WHERE product_code=?", (code,))
        conn.execute("DELETE FROM user_assets WHERE asset_code=?", (code,))
        conn.execute("UPDATE els_reserves SET locked_amount=0,principal_amount=0,coupon_amount=0 WHERE product_code=?", (code,))
        conn.execute("UPDATE els_terms SET state='cancelled',settled_epoch=? WHERE product_code=?", (now, code))
        conn.execute("UPDATE investment SET status='cancelled' WHERE product_code=?", (code,))

    def observe(self, conn, now, prices=None):
        """Record only real price ticks; never reconstruct a missing historical path."""
        now = int(now)
        if not MarketSessionManager(self.db).prices_running(conn=conn, now=now):
            return []
        recorded = []
        for terms in conn.execute("SELECT * FROM els_terms WHERE state IN ('subscription','active','observation_pending')").fetchall():
            code = terms["product_code"]
            if terms["state"] == "subscription" and now < terms["subscription_end_epoch"]:
                continue
            if terms["issue_epoch"] is None:
                sold = conn.execute("SELECT 1 FROM els_subscriptions WHERE product_code=? AND status='subscribed' LIMIT 1", (code,)).fetchone()
                if not sold:
                    self._cancel(conn, terms, now)
                    continue
            underlying_rows = conn.execute("SELECT * FROM els_underlyings WHERE product_code=?", (code,)).fetchall()
            infos = [self._underlying(conn, row["underlying_code"], prices) for row in underlying_rows]
            values = [self._valid_price(info["price"]) if info else None for info in infos]
            if terms["issue_epoch"] is None:
                if not values or any(value is None for value in values):
                    continue
                conn.execute("UPDATE els_terms SET issue_epoch=?,maturity_epoch=?,state='active' WHERE product_code=?",
                    (now, now + terms["maturity_days"] * DAY, code))
                conn.execute("UPDATE investment SET maturity_epoch=? WHERE product_code=?", (now + terms["maturity_days"] * DAY, code))
                conn.execute("UPDATE els_observations SET scheduled_epoch=?+observation_day*? WHERE product_code=?", (now, DAY, code))
                conn.execute("UPDATE els_subscriptions SET status='issued',confirmed_epoch=? WHERE product_code=?", (now, code))
                conn.executemany("UPDATE els_underlyings SET initial_price=?,last_price=?,last_epoch=?,min_ratio=1 WHERE product_code=? AND underlying_code=?",
                    [(value, value, now, code, row["underlying_code"]) for row, value in zip(underlying_rows, values)])
                recorded.append(dict(product_code=code, kind="issued", recorded_epoch=now))
                continue
            ratios = [value / row["initial_price"]
                if value is not None and self._valid_price(row["initial_price"]) is not None else None
                for row, value in zip(underlying_rows, values)]
            knocked = bool(terms["knocked_in"])
            for row, value, ratio in zip(underlying_rows, values, ratios):
                if ratio is None:
                    continue
                hit = ratio <= terms["knock_in_level"]
                knocked = knocked or hit
                conn.execute("UPDATE els_underlyings SET last_price=?,last_epoch=?,min_ratio=MIN(COALESCE(min_ratio,?),?),"
                    "knocked_in=MAX(knocked_in,?),knock_in_epoch=CASE WHEN ?=1 THEN COALESCE(knock_in_epoch,?) ELSE knock_in_epoch END "
                    "WHERE product_code=? AND underlying_code=?",
                    (value, now, ratio, ratio, int(hit), int(hit), now, code, row["underlying_code"]))
            missing = not ratios or any(ratio is None for ratio in ratios)
            conn.execute("UPDATE els_terms SET knocked_in=?,state=? WHERE product_code=?",
                (int(knocked), "observation_pending" if missing else "active", code))
            if missing:
                continue
            due = conn.execute("SELECT * FROM els_observations WHERE product_code=? AND outcome='pending' "
                "AND scheduled_epoch<=? ORDER BY sequence", (code, now)).fetchall()
            if not due:
                continue
            # After downtime, use only the latest due round; previous rounds are not backfilled.
            observation = due[-1]
            for skipped in due[:-1]:
                conn.execute("UPDATE els_observations SET outcome='skipped' WHERE product_code=? AND sequence=?", (code, skipped["sequence"]))
            ratio = min(ratios)
            redeem = ratio >= observation["redemption_level"] or (observation["is_maturity"] and not knocked)
            outcome = "redeem" if redeem else "loss" if observation["is_maturity"] else "continue"
            snapshot = {row["underlying_code"]: value for row, value in zip(underlying_rows, values)}
            conn.execute("UPDATE els_observations SET observed_epoch=?,prices_json=?,assessment_ratio=?,outcome=? "
                "WHERE product_code=? AND sequence=?", (now, json.dumps(snapshot, ensure_ascii=False), ratio, outcome, code, observation["sequence"]))
            recorded.append(dict(product_code=code, kind=outcome, sequence=observation["sequence"], ratio=ratio))
            if outcome in {"redeem", "loss"}:
                conn.execute("UPDATE els_terms SET state='redemption_pending',final_ratio=?,final_sequence=?,settlement_kind=? WHERE product_code=?",
                    (ratio, observation["sequence"], "maturity_loss" if outcome == "loss" else "maturity" if observation["is_maturity"] else "early", code))
        return recorded

    def process_due(self, now=None):
        """Pay previously observed contracts atomically, retaining snapshots on failure."""
        now = int(time.time() if now is None else now)
        notifications = []
        self.last_settlement_errors = []
        codes = self.db.products.execute("SELECT product_code FROM els_terms WHERE state='redemption_pending'")
        for item in codes:
            try:
                with self.db.atomic() as conn:
                    terms = conn.execute("SELECT * FROM els_terms WHERE product_code=?", (item["product_code"],)).fetchone()
                    if terms["state"] != "redemption_pending":
                        continue
                    observation = conn.execute("SELECT * FROM els_observations WHERE product_code=? AND sequence=?",
                        (terms["product_code"], terms["final_sequence"])).fetchone()
                    if not observation or observation["outcome"] not in {"redeem", "loss"}:
                        continue
                    paid = self._settle(conn, terms, observation, now)
                notifications.extend(paid)
            except ValueError as exc:
                # A recipient limit or damaged reserve must not hold up other
                # issuers. The failed contract's transaction is fully rolled back.
                self.last_settlement_errors.append(dict(product_code=item["product_code"], error=str(exc)))
        return notifications

    def _settle(self, conn, terms, observation, now):
        code = terms["product_code"]
        reserve = conn.execute("SELECT * FROM els_reserves WHERE product_code=?", (code,)).fetchone()
        subscriptions = conn.execute("SELECT * FROM els_subscriptions WHERE product_code=? AND status='issued' ORDER BY subscription_id", (code,)).fetchall()
        payments = []
        for subscription in subscriptions:
            principal = int(subscription["principal"])
            gross = (round_money(Decimal(principal) * Decimal(str(observation["assessment_ratio"])))
                if observation["outcome"] == "loss" else principal + self._coupon(principal, terms["annual_coupon"], observation["observation_day"]))
            payments.append((subscription, gross))
        gross_total = sum(gross for _, gross in payments)
        if not reserve or gross_total > reserve["locked_amount"]:
            raise ValueError("ELS 예치금이 부족하여 정산을 보류합니다.")
        orders = conn.execute("SELECT * FROM market_orders WHERE asset_code=? AND status IN ('open','partial')", (code,)).fetchall()
        for order in orders:
            self._safe_credit(conn, order["user_id"], order["funding_account"], order["reserved_cash"])
            conn.execute("UPDATE market_orders SET status='expired',reserved_cash=0,updated_epoch=? WHERE order_id=?", (now, order["order_id"]))
        notifications = []
        for subscription, gross in payments:
            tax = self.users._tax(conn, subscription["user_id"], "els_profit", max(0, gross - subscription["principal"]))
            net = gross - tax
            self._safe_credit(conn, subscription["user_id"], None, net)
            conn.execute("INSERT INTO els_settlements(product_code,sequence,subscription_id,user_id,quantity,principal,"
                "gross_amount,tax_amount,net_amount,recorded_epoch) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (code, observation["sequence"], subscription["subscription_id"], subscription["user_id"], subscription["quantity"],
                 subscription["principal"], gross, tax, net, now))
            conn.execute("INSERT INTO product_settlements(user_id,product_code,kind,quantity,gross_amount,tax_amount,recorded_epoch) "
                "VALUES (?,?,?,?,?,?,?)", (subscription["user_id"], code, f"els_{terms['settlement_kind']}", subscription["quantity"], gross, tax, now))
            conn.execute("UPDATE els_subscriptions SET status='settled' WHERE subscription_id=?", (subscription["subscription_id"],))
            notifications.append(dict(user_id=subscription["user_id"], product_code=code, net=net, gross=gross, tax=tax,
                                      reason=f"els_{terms['settlement_kind']}"))
        self._release_reserve(conn, terms["issuer_code"], reserve["locked_amount"], reserve["locked_amount"] - gross_total, reserve["principal_amount"])
        conn.execute("UPDATE els_reserves SET locked_amount=0,principal_amount=0,coupon_amount=0 WHERE product_code=?", (code,))
        conn.execute("DELETE FROM user_assets WHERE asset_code=?", (code,))
        conn.execute("UPDATE investment SET status='redeemed' WHERE product_code=?", (code,))
        conn.execute("UPDATE els_terms SET state='redeemed',settled_epoch=? WHERE product_code=?", (now, code))
        return notifications

    def get_details(self, code, user_id=None, *, conn=None):
        with (self.db.readonly() if conn is None else nullcontext(conn)) as conn:
            row = conn.execute("SELECT t.*,i.name,i.total_supply FROM els_terms t JOIN investment i USING(product_code) WHERE product_code=?", (code,)).fetchone()
            if not row:
                return None
            details = dict(row)
            details["status"] = details["state"]
            details["code"] = details["product_code"]
            details["underlyings"] = []
            for underlying in conn.execute("SELECT * FROM els_underlyings WHERE product_code=? ORDER BY underlying_code", (code,)).fetchall():
                info = self._underlying(conn, underlying["underlying_code"])
                price = underlying["last_price"] if underlying["initial_price"] else self._valid_price(info["price"]) if info else None
                details["underlyings"].append(dict(code=underlying["underlying_code"], name=info["name"] if info else underlying["underlying_code"],
                    initial_price=underlying["initial_price"], current_price=price,
                    ratio=price / underlying["initial_price"] if price and underlying["initial_price"] else None,
                    min_ratio=underlying["min_ratio"], knocked_in=bool(underlying["knocked_in"])))
            next_round = conn.execute("SELECT * FROM els_observations WHERE product_code=? AND outcome='pending' ORDER BY sequence LIMIT 1", (code,)).fetchone()
            details["next_observation_epoch"] = next_round["scheduled_epoch"] if next_round else None
            details["next_redemption_level"] = next_round["redemption_level"] if next_round else None
            details["observations"] = [dict(value) for value in conn.execute("SELECT * FROM els_observations WHERE product_code=? ORDER BY sequence", (code,)).fetchall()]
            details["subscribed_quantity"] = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM els_subscriptions WHERE product_code=? AND status IN ('subscribed','issued')", (code,)).fetchone()[0]
            if user_id is not None:
                position = conn.execute("SELECT COALESCE(SUM(quantity),0) quantity,COALESCE(SUM(principal),0) principal FROM els_subscriptions "
                    "WHERE product_code=? AND user_id=? AND status IN ('subscribed','issued')", (code, str(user_id))).fetchone()
                details.update(quantity=position["quantity"], principal=position["principal"],
                    value_nwx=position["principal"],
                    settlements=[dict(value) for value in conn.execute("SELECT * FROM els_settlements WHERE product_code=? AND user_id=? ORDER BY settlement_id", (code, str(user_id))).fetchall()])
            return details

    def get_user_positions(self, user_id, *, conn=None):
        with (self.db.readonly() if conn is None else nullcontext(conn)) as conn:
            codes = conn.execute("SELECT DISTINCT product_code FROM els_subscriptions WHERE user_id=? "
                "AND status IN ('subscribed','issued')", (str(user_id),)).fetchall()
            return [self.get_details(item["product_code"], user_id, conn=conn) for item in codes]

    @staticmethod
    def ensure_unreferenced(conn, code):
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='els_terms'").fetchone()
        if not exists:
            return
        linked = conn.execute("SELECT t.product_code FROM els_terms t LEFT JOIN els_underlyings u USING(product_code) "
            "WHERE t.state IN ('subscription','active','observation_pending','redemption_pending') "
            "AND (t.issuer_code=? OR u.underlying_code=?) LIMIT 1", (code, code)).fetchone()
        if linked:
            raise ValueError(f"미상환 ELS `{linked['product_code']}`의 기초자산·발행법인은 삭제하거나 합병할 수 없습니다.")
        if ELSManager.index_removal_blocked(conn, [code]):
            raise ValueError("미상환 ELS의 기초 거래소 지수에 마지막 유효 구성종목을 남겨야 합니다.")

    @staticmethod
    def index_removal_blocked(conn, codes):
        """Would removing these companies eliminate a live ELS index basis?"""
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='els_terms'").fetchone()
        if not exists:
            return False
        codes = [codes] if isinstance(codes, str) else list(codes)
        if not codes:
            return False
        placeholders = ','.join('?' for _ in codes)
        exchanges = conn.execute(f"SELECT DISTINCT exchange_id FROM company WHERE company_code IN ({placeholders})", codes).fetchall()
        for exchange in exchanges:
            index_code = INDEX_PREFIX + str(exchange['exchange_id'] or '')
            linked = conn.execute("SELECT 1 FROM els_terms t JOIN els_underlyings u USING(product_code) "
                "WHERE t.state IN ('subscription','active','observation_pending','redemption_pending') "
                "AND u.underlying_code=? LIMIT 1", (index_code,)).fetchone()
            if not linked:
                continue
            remaining = conn.execute(f"SELECT 1 FROM company WHERE exchange_id=? AND status='active' "
                f"AND share_price>0 AND total_shares>0 AND company_code NOT IN ({placeholders}) LIMIT 1",
                [exchange['exchange_id'], *codes]).fetchone()
            if not remaining:
                return True
        return False
