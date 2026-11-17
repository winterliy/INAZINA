"""Payments to a citizen who holds no account in the payment's currency.

The receiver chooses between exchanging the amount into their own currency at the live rate
and a temporary account issued by the payer. The amount sits in a ``payout_requests`` row until
they decide. A temporary account can only be withdrawn into the receiver's own account in that
currency (a 3% custody fee goes to the payer); it is never an account, so it cannot be sent
from, deposited to or otherwise used. Undecided requests and unwithdrawn temporary accounts
return to the payer after the hold period.
"""
import time
from decimal import Decimal, ROUND_HALF_UP

from functions.money import currency_precision, from_minor_units, to_minor_units
from managers.notification_manager import NotificationManager

DAY = 86400
HOLD_DAYS = 30
FEE_PERCENT = Decimal(3)


class PayoutManager:
    @staticmethod
    def _row(conn, payout_id, user_id=None):
        row = conn.execute("SELECT * FROM payout_requests WHERE payout_id=?", (int(payout_id),)).fetchone()
        if not row or (user_id is not None and str(row["user_id"]) != str(user_id)):
            raise ValueError("지급 내역을 찾을 수 없습니다.")
        return row

    @staticmethod
    def _receive(conn, user_id, row, reference, minor_units, now):
        """Pay ``minor_units`` of the payout's currency to ``user_id``, keeping the exchange record when it was exchanged."""
        from managers.currency_manager import CurrencyManager
        settlement = CurrencyManager.receive(conn, user_id, row["currency_code"], int(minor_units))
        if settlement:
            CurrencyManager.record_fx_settlement(
                conn, f"payout:{row['kind']}", reference, user_id, row["currency_code"], int(minor_units),
                settlement, now)

    @staticmethod
    def fee_minor_units(minor_units):
        return int((Decimal(int(minor_units)) * FEE_PERCENT / 100).to_integral_value(rounding=ROUND_HALF_UP))

    @classmethod
    def view(cls, row):
        places = int(row["decimal_places"])
        minor = int(row["minor_units"])
        fee = cls.fee_minor_units(minor)
        return {"payout_id": int(row["payout_id"]), "status": row["status"], "kind": row["kind"],
                "currency": row["currency_code"], "decimal_places": places,
                "amount": str(from_minor_units(minor, places)),
                "withdraw_fee": str(from_minor_units(fee, places)),
                "withdraw_net": str(from_minor_units(minor - fee, places)),
                "created_epoch": int(row["created_epoch"]), "expires_epoch": int(row["expires_epoch"]),
                "temp_number": row["temp_number"], "payer_id": row["payer_id"]}

    @classmethod
    def offer(cls, conn, user_id, payer_id, currency, amount, kind, reference, now=None):
        """Hold ``amount`` (native units) for ``user_id`` until they choose; returns the payout id."""
        now = int(now if now is not None else time.time())
        currency = str(currency).upper()
        places = currency_precision(conn, currency)
        minor = to_minor_units(amount, places)
        if minor <= 0:
            raise ValueError("지급액은 0보다 커야 합니다.")
        existing = conn.execute("SELECT payout_id FROM payout_requests WHERE kind=? AND reference=?",
                                (str(kind), str(reference))).fetchone()
        if existing:
            return int(existing[0])
        payout_id = int(conn.execute(
            "INSERT INTO payout_requests(user_id,payer_id,currency_code,decimal_places,minor_units,kind,"
            "reference,created_epoch,expires_epoch) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(user_id), str(payer_id), currency, places, minor, str(kind), str(reference), now,
             now + HOLD_DAYS * DAY)).lastrowid)
        shown = from_minor_units(minor, places)
        NotificationManager.add(
            conn, user_id, "payout_choice", "지급 방법을 선택해주세요",
            f"{currency} 통장이 없어 {shown:,} {currency}를 바로 받을 수 없습니다. "
            f"실시간 환율로 환전하거나 임시계좌로 받을 수 있습니다. "
            f"{HOLD_DAYS}일 안에 선택하지 않으면 지급한 곳으로 귀속됩니다.",
            action_path=f"/payouts/{payout_id}", payload={"payout_id": payout_id, "currency": currency},
            dedupe_key=f"payout:{payout_id}:offer", now=now)
        return payout_id

    @classmethod
    def convert(cls, conn, user_id, payout_id, now=None):
        """Exchange into the receiver's own currency at the live quote (or credit their account if they have since opened one)."""
        from managers.accounting_journal import AccountingJournal
        now = int(now if now is not None else time.time())
        row = cls._row(conn, payout_id, user_id)
        if row["status"] != "pending" or int(row["expires_epoch"]) <= now:
            raise ValueError("이미 처리됐거나 기한이 지난 지급입니다.")
        mark = AccountingJournal.cash_mark(conn)
        cls._receive(conn, row["user_id"], row, f"payout:{row['payout_id']}", row["minor_units"], now)
        conn.execute("UPDATE payout_requests SET status='converted',decided_epoch=? WHERE payout_id=?",
                     (now, int(payout_id)))
        AccountingJournal.link_cash(conn, row["kind"], f"payout:{payout_id}:convert", mark, now)
        return cls.view(cls._row(conn, payout_id))

    @classmethod
    def open_temp(cls, conn, user_id, payout_id, now=None):
        """Issue the temporary account; its hold period restarts at issue."""
        now = int(now if now is not None else time.time())
        row = cls._row(conn, payout_id, user_id)
        if row["status"] != "pending" or int(row["expires_epoch"]) <= now:
            raise ValueError("이미 처리됐거나 기한이 지난 지급입니다.")
        number = f"TMP{int(payout_id):08d}"
        conn.execute("UPDATE payout_requests SET status='temp_open',decided_epoch=?,temp_number=?,expires_epoch=? "
                     "WHERE payout_id=?", (now, number, now + HOLD_DAYS * DAY, int(payout_id)))
        NotificationManager.add(
            conn, user_id, "payout_temp_account", "임시계좌가 발급되었습니다",
            f"임시계좌 {number}에서 {HOLD_DAYS}일 안에 인출하세요. 인출하려면 {row['currency_code']} 통장이 있어야 하고, "
            f"인출 시 보관 수수료 {FEE_PERCENT}%가 차감됩니다.",
            action_path=f"/payouts/{payout_id}", payload={"payout_id": int(payout_id), "temp_number": number},
            dedupe_key=f"payout:{payout_id}:temp", now=now)
        return cls.view(cls._row(conn, payout_id))

    @classmethod
    def withdraw(cls, conn, user_id, payout_id, now=None):
        """Move a temporary account's balance, less the custody fee, into the receiver's own account in that currency."""
        from managers.accounting_journal import AccountingJournal
        from managers.currency_manager import CurrencyManager
        from managers.deposit_accounts import SPENDABLE
        from managers.user_manager import UserManager
        now = int(now if now is not None else time.time())
        row = cls._row(conn, payout_id, user_id)
        if row["status"] != "temp_open" or int(row["expires_epoch"]) <= now:
            raise ValueError("인출할 수 있는 임시계좌가 아닙니다.")
        currency = row["currency_code"]
        home = CurrencyManager.home(conn, user_id)
        if currency == home:
            account_number = None
        else:
            target = conn.execute(
                "SELECT a_policy.account_number FROM accounts a_policy WHERE a_policy.user_id=? "
                "AND a_policy.quote_currency=? "
                f"AND a_policy.account_type='saving' AND {SPENDABLE} ORDER BY a_policy.balance_units DESC LIMIT 1",
                (str(user_id), currency)).fetchone()
            if not target:
                raise ValueError(f"{currency} 통장을 먼저 개설해주세요. 임시계좌는 내 {currency} 통장으로만 인출할 수 있습니다.")
            account_number = target[0]
        places, minor = int(row["decimal_places"]), int(row["minor_units"])
        fee = cls.fee_minor_units(minor)
        mark = AccountingJournal.cash_mark(conn)
        UserManager._credit_source(conn, user_id, account_number, from_minor_units(minor - fee, places),
                                   event="payout_withdrawal", reference=f"payout:{payout_id}", currency=currency)
        if fee:
            CurrencyManager.receive(conn, row["payer_id"], currency, fee)
        conn.execute("UPDATE payout_requests SET status='withdrawn',decided_epoch=?,withdrawn_account=?,"
                     "fee_minor_units=? WHERE payout_id=?", (now, account_number or "wallet", fee, int(payout_id)))
        AccountingJournal.link_cash(conn, row["kind"], f"payout:{payout_id}:withdraw", mark, now)
        return cls.view(cls._row(conn, payout_id))

    @classmethod
    def expire_due(cls, conn, now=None):
        """Return undecided or unwithdrawn amounts to the payer once their hold period has passed."""
        from managers.accounting_journal import AccountingJournal
        now = int(now if now is not None else time.time())
        returned = []
        for row in conn.execute("SELECT * FROM payout_requests WHERE status IN ('pending','temp_open') "
                                "AND expires_epoch<=? ORDER BY payout_id", (now,)).fetchall():
            mark = AccountingJournal.cash_mark(conn)
            cls._receive(conn, row["payer_id"], row, f"payout:{row['payout_id']}:return", row["minor_units"], now)
            conn.execute("UPDATE payout_requests SET status='forfeited',decided_epoch=? WHERE payout_id=?",
                         (now, int(row["payout_id"])))
            AccountingJournal.link_cash(conn, row["kind"], f"payout:{row['payout_id']}:return", mark, now)
            NotificationManager.add(
                conn, row["user_id"], "payout_expired", "지급 기한이 지났습니다",
                f"{from_minor_units(int(row['minor_units']), int(row['decimal_places'])):,} {row['currency_code']} 지급이 "
                f"기한 안에 처리되지 않아 지급한 곳으로 귀속되었습니다.",
                payload={"payout_id": int(row["payout_id"])}, dedupe_key=f"payout:{row['payout_id']}:expired", now=now)
            returned.append(cls.view(cls._row(conn, row["payout_id"])))
        return returned

    @classmethod
    def open_for(cls, conn, user_id):
        return [cls.view(row) for row in conn.execute(
            "SELECT * FROM payout_requests WHERE user_id=? AND status IN ('pending','temp_open') "
            "ORDER BY payout_id DESC", (str(user_id),)).fetchall()]

    @classmethod
    def get(cls, conn, user_id, payout_id):
        return cls.view(cls._row(conn, payout_id, user_id))
