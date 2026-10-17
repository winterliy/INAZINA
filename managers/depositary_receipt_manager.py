"""Atomic simulator conversion between DRs and their underlying common shares."""

from decimal import Decimal
import math
import time


class DepositaryReceiptManager:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _add_holding(conn, user_id, asset_code, quantity, unit_cost, currency_code):
        """Add a holding while keeping its weighted cost in native currency precision."""
        from functions.money import round_currency
        row = conn.execute(
            "SELECT holding_id,quantity,average_cost FROM assets.user_assets "
            "WHERE user_id=? AND asset_code=? AND IFNULL(account_number,'')='' "
            "ORDER BY holding_id LIMIT 1", (str(user_id), str(asset_code)),
        ).fetchone()
        if row:
            old_quantity = Decimal(str(row["quantity"] or 0))
            added_quantity = Decimal(str(quantity))
            total_quantity = old_quantity + added_quantity
            average_cost = round_currency(
                conn,
                (old_quantity * Decimal(str(row["average_cost"] or 0))
                 + added_quantity * Decimal(str(unit_cost))) / total_quantity,
                currency_code,
            )
            conn.execute(
                "UPDATE assets.user_assets SET quantity=?,average_cost=? WHERE holding_id=?",
                (float(total_quantity), average_cost, row["holding_id"]),
            )
        else:
            conn.execute(
                "INSERT INTO assets.user_assets(user_id,asset_code,account_number,quantity,average_cost) "
                "VALUES (?,?,NULL,?,?)",
                (str(user_id), str(asset_code), float(quantity), unit_cost),
            )

    def convert(self, user_id, dr_code, action, quantity, reference_key, *, now=None):
        """Issue DRs against deposited shares or redeem DRs for pooled custody shares.

        Conversion is limited to whole underlying shares. The custody ledger is a
        simulator book, not evidence of real-world depositary or custodian control.
        """
        if action not in {"issue", "redeem"}:
            return False, "❌ 전환 방향은 issue 또는 redeem이어야 합니다."
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            return False, "❌ 전환 수량은 1 이상의 정수 DR 수량이어야 합니다."
        reference_key = str(reference_key or "").strip()
        if not reference_key or len(reference_key) > 100:
            return False, "❌ 중복 실행 방지 참조값을 확인해주세요."

        when = float(time.time() if now is None else now)
        try:
            with self.db.atomic() as conn:
                prior = conn.execute(
                    "SELECT user_id,dr_code,movement_type,dr_quantity,underlying_quantity FROM "
                    "assets.depositary_receipt_movements WHERE reference_key=?",
                    (reference_key,),
                ).fetchone()
                if prior:
                    if str(prior["user_id"]) != str(user_id) or str(prior["dr_code"]) != str(dr_code):
                        raise ValueError("이미 다른 DR 전환에 사용된 요청 참조값입니다.")
                    if prior["movement_type"] != action or int(prior["dr_quantity"]) != quantity:
                        raise ValueError("요청 참조값이 다른 DR 전환 내용에 이미 사용되었습니다.")
                    return True, (f"✅ 이 전환 요청은 이미 처리되었습니다. "
                                  f"{prior['movement_type']} {prior['dr_quantity']} DR")

                owner = conn.execute(
                    "SELECT status FROM users.users WHERE user_id=?", (str(user_id),)
                ).fetchone()
                if not owner or owner["status"] != "active":
                    raise ValueError("활성 사용자만 DR 전환을 이용할 수 있습니다.")
                program = conn.execute(
                    "SELECT d.*,a.status AS asset_status,a.total_supply,a.price AS dr_price,"
                    "a.quote_currency AS dr_currency,a.type AS asset_type "
                    "FROM assets.depositary_receipts d JOIN assets.market_assets a "
                    "ON a.code=d.dr_code WHERE d.dr_code=?",
                    (str(dr_code).strip(),),
                ).fetchone()
                if not program or program["asset_type"] != "depositary_receipt":
                    raise ValueError("주식예탁증서(DR) 프로그램을 찾을 수 없습니다.")
                if program["program_status"] != "active" or program["asset_status"] != "active":
                    raise ValueError("중지된 DR 프로그램은 전환할 수 없습니다.")
                underlying = conn.execute(
                    "SELECT company_code AS code,share_price AS price,quote_currency,status,share_class "
                    "FROM company WHERE company_code=?",
                    (program["underlying_code"],),
                ).fetchone()
                if not underlying or underlying["status"] != "active" or underlying["share_class"] != "common":
                    raise ValueError("기초 보통주가 거래 가능하지 않아 전환을 중지했습니다.")

                exact_shares = Decimal(quantity) * Decimal(str(program["underlying_per_receipt"]))
                shares = int(exact_shares)
                if shares <= 0 or exact_shares != Decimal(shares):
                    raise ValueError("전환 수량은 원주 정수 수량이 되도록 DR 수량을 맞춰주세요.")
                code = str(program["dr_code"])
                underlying_code = str(program["underlying_code"])
                user = str(user_id)

                if action == "issue":
                    supply = min(
                        int(program["receipt_supply"]),
                        int(program["total_supply"] or program["receipt_supply"]),
                    )
                    outstanding = conn.execute(
                        "SELECT "
                        "(SELECT COALESCE(SUM(quantity),0) FROM assets.user_assets WHERE asset_code=?) + "
                        "(SELECT COALESCE(SUM(quantity),0) FROM assets.option_collateral_lots WHERE underlying_code=?) + "
                        "(SELECT COALESCE(SUM(quantity),0) FROM accounts.loan_collateral_assets "
                        " WHERE asset_code=? AND status='pledged') + "
                        "(SELECT COALESCE(SUM(remaining_quantity),0) FROM assets.market_orders "
                        " WHERE asset_code=? AND side='buy' AND status IN ('scheduled','open','partial'))",
                        (code, code, code, code),
                    ).fetchone()[0]
                    if int(outstanding or 0) + quantity > supply:
                        raise ValueError(f"DR 프로그램 발행 한도({supply:,})를 초과합니다.")

                    lots = conn.execute(
                        "SELECT holding_id,account_number,quantity,average_cost FROM assets.user_assets "
                        "WHERE user_id=? AND asset_code=? ORDER BY holding_id",
                        (user, underlying_code),
                    ).fetchall()
                    reserved_by_account = {
                        row["funding_account"]: float(row["quantity"] or 0)
                        for row in conn.execute(
                            "SELECT funding_account,SUM(remaining_quantity) AS quantity "
                            "FROM assets.market_orders WHERE user_id=? AND asset_code=? AND side='sell' "
                            "AND status IN ('scheduled','open','partial') GROUP BY funding_account",
                            (user, underlying_code),
                        ).fetchall()
                    }
                    capacities = {}
                    for lot in lots:
                        account = lot["account_number"]
                        capacities[account] = capacities.get(account, 0.0) + float(lot["quantity"] or 0)
                    for account, reserved in reserved_by_account.items():
                        capacities[account] = max(0.0, capacities.get(account, 0.0) - reserved)
                    available = sum(math.floor(max(0.0, value) + 1e-9) for value in capacities.values())
                    if available + 1e-9 < shares:
                        raise ValueError(f"전환 가능한 {underlying_code} 원주가 부족합니다. 필요 {shares:,}, 가능 {int(available):,}주입니다.")

                    remaining = shares
                    total_cost = Decimal(0)
                    for lot in lots:
                        account = lot["account_number"]
                        capacity = capacities.get(account, 0.0)
                        old_quantity = float(lot["quantity"] or 0)
                        take = min(remaining, int(math.floor(capacity + 1e-9)),
                                   int(math.floor(old_quantity + 1e-9)))
                        if take <= 0:
                            continue
                        total_cost += Decimal(str(lot["average_cost"] or 0)) * take
                        changed = (conn.execute(
                            "DELETE FROM assets.user_assets WHERE holding_id=? AND quantity=?",
                            (lot["holding_id"], lot["quantity"]),
                        ) if math.isclose(take, old_quantity, rel_tol=0.0, abs_tol=1e-9) else conn.execute(
                            "UPDATE assets.user_assets SET quantity=quantity-? WHERE holding_id=? AND quantity=?",
                            (take, lot["holding_id"], lot["quantity"]),
                        ))
                        if changed.rowcount != 1:
                            raise RuntimeError("원주 보유량이 변경되어 DR 전환을 취소했습니다.")
                        capacities[account] = capacity - take
                        remaining -= take
                        if not remaining:
                            break
                    if remaining:
                        raise RuntimeError("예약 주문을 제외한 원주 수량이 부족해 전환을 취소했습니다.")
                    from functions.money import round_currency
                    from managers.currency_manager import CurrencyManager
                    underlying_cost = round_currency(conn, total_cost / shares, underlying["quote_currency"])
                    fx = CurrencyManager.quote(conn, underlying["quote_currency"], program["dr_currency"])
                    dr_cost = round_currency(
                        conn, Decimal(underlying_cost) * exact_shares / Decimal(quantity) *
                        Decimal(fx.numerator) / Decimal(fx.denominator), program["dr_currency"],
                    )
                    self._add_holding(conn, user, code, quantity, dr_cost, program["dr_currency"])
                    underlying_average_cost = underlying_cost
                else:
                    owned = conn.execute(
                        "SELECT COALESCE(SUM(quantity),0) FROM assets.user_assets "
                        "WHERE user_id=? AND asset_code=?", (user, code),
                    ).fetchone()[0]
                    reserved = conn.execute(
                        "SELECT COALESCE(SUM(remaining_quantity),0) FROM assets.market_orders "
                        "WHERE user_id=? AND asset_code=? AND side='sell' "
                        "AND status IN ('scheduled','open','partial')", (user, code),
                    ).fetchone()[0]
                    if float(owned or 0) - float(reserved or 0) + 1e-9 < quantity:
                        raise ValueError("미체결 매도 주문을 제외한 DR 보유 수량이 부족합니다.")
                    custody = conn.execute(
                        "SELECT COALESCE(SUM(CASE WHEN movement_type='issue' THEN underlying_quantity "
                        "ELSE -underlying_quantity END),0) AS shares, "
                        "COALESCE(SUM(CASE WHEN movement_type='issue' THEN underlying_quantity*underlying_average_cost "
                        "ELSE -underlying_quantity*underlying_average_cost END),0) AS cost "
                        "FROM assets.depositary_receipt_movements WHERE dr_code=?",
                        (code,),
                    ).fetchone()
                    if int(custody["shares"] or 0) < shares:
                        raise ValueError("상환 가능한 시뮬레이터 예탁 원주가 부족합니다.")
                    dr_lots = conn.execute(
                        "SELECT holding_id,quantity FROM assets.user_assets "
                        "WHERE user_id=? AND asset_code=? ORDER BY holding_id", (user, code),
                    ).fetchall()
                    remaining = quantity
                    for lot in dr_lots:
                        take = min(remaining, int(lot["quantity"] or 0))
                        if take <= 0:
                            continue
                        changed = (conn.execute(
                            "DELETE FROM assets.user_assets WHERE holding_id=? AND quantity=?",
                            (lot["holding_id"], lot["quantity"]),
                        ) if take == int(lot["quantity"]) else conn.execute(
                            "UPDATE assets.user_assets SET quantity=quantity-? WHERE holding_id=? AND quantity=?",
                            (take, lot["holding_id"], lot["quantity"]),
                        ))
                        if changed.rowcount != 1:
                            raise RuntimeError("DR 보유량이 변경되어 상환을 취소했습니다.")
                        remaining -= take
                        if not remaining:
                            break
                    if remaining:
                        raise RuntimeError("DR 보유 수량이 부족해 상환을 취소했습니다.")
                    from functions.money import round_currency
                    underlying_average_cost = round_currency(
                        conn, Decimal(str(custody["cost"] or 0)) / Decimal(int(custody["shares"])),
                        underlying["quote_currency"],
                    )
                    self._add_holding(conn, user, underlying_code, shares,
                                      underlying_average_cost, underlying["quote_currency"])

                conn.execute(
                    "INSERT INTO assets.depositary_receipt_movements(reference_key,dr_code,user_id,"
                    "movement_type,dr_quantity,underlying_quantity,underlying_average_cost,recorded_epoch) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (reference_key, code, user, action, quantity, shares,
                     underlying_average_cost, when),
                )
                return True, (f"✅ DR {action} 전환 완료: {quantity:,} {code} ↔ "
                              f"{shares:,} {underlying_code} 원주 (시뮬레이터 원장)")
        except (ValueError, RuntimeError, KeyError, TypeError, OverflowError) as exc:
            return False, f"❌ {exc}"
