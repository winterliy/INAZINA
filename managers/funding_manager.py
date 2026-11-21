"""Wholesale funding of a financial institution: bank bonds, interbank, repo, central bank.

Simulator model. Each draw is one immutable row with its own rate, maturity,
collateral and haircut; it is posted to the institution journal as a liability
and repaid with simple ACT/365 interest. Limits are an operator setting per
source and currency, not a regulatory figure.
"""
from decimal import Decimal, ROUND_HALF_UP

from functions.money import currency_precision, from_minor_units, to_minor_units
from managers.bank_journal import BankJournal

DAY = 86400


class FundingManager:
    SOURCES = ("bank_bond", "interbank", "repo", "central_bank")
    SECURED = frozenset(("repo", "central_bank"))  # need collateral worth the draw after haircut
    LADDER = (("0-7d", 7), ("8-30d", 30), ("31-90d", 90), ("91-365d", 365), ("365d+", None))

    @classmethod
    def _source(cls, source_type):
        source = str(source_type)
        if source not in cls.SOURCES:
            raise ValueError("조달원은 bank_bond, interbank, repo, central_bank 중 하나여야 합니다.")
        return source

    @staticmethod
    def _units(conn, currency, amount):
        """Exact minor units of ``amount``; refuses anything finer than the currency's minor unit."""
        places = currency_precision(conn, currency)
        units = to_minor_units(amount, places)
        if from_minor_units(units, places) != Decimal(str(amount)):
            raise ValueError(f"{currency} 금액은 소수점 {places}자리까지 입력해주세요.")
        return places, units

    @classmethod
    def set_limit(cls, conn, issuer, source_type, currency_code, limit, *, actor, now):
        source, currency = cls._source(source_type), str(currency_code).upper()
        _, units = cls._units(conn, currency, limit)
        if units < 0:
            raise ValueError("조달 한도는 0 이상이어야 합니다.")
        conn.execute("""INSERT INTO institution_funding_limits
            (issuer,source_type,currency_code,limit_minor_units,updated_by,updated_epoch) VALUES (?,?,?,?,?,?)
            ON CONFLICT(issuer,source_type,currency_code) DO UPDATE SET
                limit_minor_units=excluded.limit_minor_units,updated_by=excluded.updated_by,
                updated_epoch=excluded.updated_epoch""",
            (str(issuer), source, currency, units, str(actor), int(BankJournal.validate_epoch(now))))

    @classmethod
    def draw(cls, conn, issuer, source_type, currency_code, amount, *, reference_key, rate_bps,
             maturity_days, now, counterparty="", collateral=0, haircut_bps=0):
        """Borrow ``amount``; returns the funding id. The same key with the same terms is a no-op."""
        source, currency, issuer = cls._source(source_type), str(currency_code).upper(), str(issuer)
        start = int(BankJournal.validate_epoch(now))
        places, units = cls._units(conn, currency, amount)
        _, collateral_units = cls._units(conn, currency, collateral)
        rate_bps, maturity_days, haircut_bps = int(rate_bps), int(maturity_days), int(haircut_bps)
        if (not reference_key or units <= 0 or rate_bps < 0 or not 1 <= maturity_days <= 36500
                or collateral_units < 0 or not 0 <= haircut_bps <= 10000):
            raise ValueError("조달 금액·금리·만기·담보 조건을 확인해주세요.")
        terms = (issuer, source, currency, places, units, rate_bps, collateral_units, haircut_bps,
                 start + maturity_days * DAY)
        prior = conn.execute("""SELECT funding_id,issuer,source_type,currency_code,decimal_places,
            principal_minor_units,rate_bps,collateral_minor_units,haircut_bps,maturity_epoch-start_epoch+?
            FROM institution_funding WHERE reference_key=?""", (start, str(reference_key))).fetchone()
        if prior:
            if tuple(prior)[1:] != terms:
                raise ValueError("동일한 조달 멱등성 키에 다른 조건이 들어왔습니다.")
            return int(prior[0])
        limit = conn.execute("""SELECT limit_minor_units FROM institution_funding_limits
            WHERE issuer=? AND source_type=? AND currency_code=?""", (issuer, source, currency)).fetchone()
        outstanding = int(conn.execute("""SELECT COALESCE(SUM(principal_minor_units),0) FROM institution_funding
            WHERE issuer=? AND source_type=? AND currency_code=? AND status='active'""",
            (issuer, source, currency)).fetchone()[0])
        if not limit or outstanding + units > int(limit[0]):
            raise ValueError("조달 한도가 설정되지 않았거나 한도를 초과합니다.")
        if source in cls.SECURED and collateral_units * (10000 - haircut_bps) < units * 10000:
            raise ValueError("담보의 헤어컷 후 가치가 조달 금액보다 작습니다.")
        funding_id = conn.execute("""INSERT INTO institution_funding
            (reference_key,issuer,source_type,counterparty,currency_code,decimal_places,principal_minor_units,
             rate_bps,collateral_minor_units,haircut_bps,start_epoch,maturity_epoch)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(reference_key), issuer, source, str(counterparty or ""), currency, places, units, rate_bps,
             collateral_units, haircut_bps, start, start + maturity_days * DAY)).lastrowid
        conn.execute("""INSERT INTO institution_funding_events
            (funding_id,event_type,principal_minor_units,interest_minor_units,recorded_epoch)
            VALUES (?,'draw',?,0,?)""", (funding_id, units, start))
        principal = from_minor_units(units, places)
        BankJournal.post(conn, issuer, "wholesale_funding_draw", str(reference_key), currency,
                         [("settlement_asset", "debit", principal),
                          ("wholesale_funding_payable", "credit", principal)],
                         description=f"{source} funding draw", now=start)
        return int(funding_id)

    @staticmethod
    def repay(conn, funding_id, *, now):
        """Repay principal with ACT/365 simple interest for the time actually outstanding."""
        from managers.rate_manager import RateManager

        row = conn.execute("SELECT * FROM institution_funding WHERE funding_id=?", (int(funding_id),)).fetchone()
        if not row:
            raise ValueError("조달 내역을 찾을 수 없습니다.")
        places = int(row["decimal_places"])
        principal_units = int(row["principal_minor_units"])
        if row["status"] == "repaid":
            return {"principal": from_minor_units(principal_units, places),
                    "interest": from_minor_units(int(row["interest_minor_units"]), places)}
        when = int(BankJournal.validate_epoch(now))
        elapsed = max(0, when - int(row["start_epoch"]))
        interest_units = int((Decimal(principal_units) * int(row["rate_bps"]) * elapsed
                              / (Decimal(10000) * 365 * DAY)).to_integral_value(rounding=ROUND_HALF_UP))
        principal = from_minor_units(principal_units, places)
        interest = from_minor_units(interest_units, places)
        reference = f"{row['reference_key']}:repay"
        conn.execute("""UPDATE institution_funding SET status='repaid',interest_minor_units=?,repaid_epoch=?
            WHERE funding_id=? AND status='active'""", (interest_units, when, int(funding_id)))
        conn.execute("""INSERT INTO institution_funding_events
            (funding_id,event_type,principal_minor_units,interest_minor_units,recorded_epoch)
            VALUES (?,'repay',?,?,?)""", (int(funding_id), principal_units, interest_units, when))
        lines = [("wholesale_funding_payable", "debit", principal),
                 ("settlement_asset", "credit", principal + interest)]
        if interest_units:
            lines.insert(1, ("interest_expense", "debit", interest))
            RateManager.record_institution_result(
                conn, row["issuer"], -interest, "wholesale_funding_interest", reference,
                now=when, currency_code=row["currency_code"])
        BankJournal.post(conn, row["issuer"], "wholesale_funding_interest", reference, row["currency_code"],
                         lines, description=f"{row['source_type']} funding repayment", now=when)
        return {"principal": principal, "interest": interest}

    @classmethod
    def overview(cls, conn, issuer, *, now, history=100):
        """What an administrator's funding screen needs: each limit with what is drawn against it, the newest draws, and the
        maturity profile. Amounts are minor units; ``decimal_places`` says how to show them."""
        when = int(BankJournal.validate_epoch(now))
        issuer = str(issuer)
        used = {(str(row["source_type"]), str(row["currency_code"])): int(row["total"]) for row in conn.execute(
            """SELECT source_type,currency_code,SUM(principal_minor_units) total FROM institution_funding
               WHERE issuer=? AND status='active' GROUP BY source_type,currency_code""", (issuer,))}
        limits = []
        for row in conn.execute("""SELECT source_type,currency_code,limit_minor_units,updated_by,updated_epoch
                FROM institution_funding_limits WHERE issuer=? ORDER BY currency_code,source_type""", (issuer,)):
            key = (str(row["source_type"]), str(row["currency_code"]))
            limit, drawn = int(row["limit_minor_units"]), used.get(key, 0)
            limits.append({"source_type": key[0], "currency_code": key[1], "decimal_places": currency_precision(conn, key[1]),
                           "limit_minor_units": limit, "outstanding_minor_units": drawn,
                           "available_minor_units": max(0, limit - drawn),
                           "updated_by": row["updated_by"], "updated_epoch": int(row["updated_epoch"])})
        draws = []
        for row in conn.execute("""SELECT funding_id,reference_key,source_type,counterparty,currency_code,decimal_places,
                principal_minor_units,rate_bps,collateral_minor_units,haircut_bps,start_epoch,maturity_epoch,status,
                interest_minor_units,repaid_epoch FROM institution_funding WHERE issuer=?
                ORDER BY funding_id DESC LIMIT ?""", (issuer, int(history))):
            draws.append({**{key: row[key] for key in row.keys()},
                          "funding_id": int(row["funding_id"]), "decimal_places": int(row["decimal_places"]),
                          "principal_minor_units": int(row["principal_minor_units"]),
                          "collateral_minor_units": int(row["collateral_minor_units"]),
                          "interest_minor_units": int(row["interest_minor_units"]),
                          "overdue": row["status"] == "active" and int(row["maturity_epoch"]) <= when})
        return {"issuer": issuer, "as_of_epoch": when, "limits": limits, "draws": draws,
                "overdue_count": sum(1 for draw in draws if draw["overdue"]), "profile": cls.profile(conn, issuer, now=when)}

    @classmethod
    def profile(cls, conn, issuer, *, now):
        """Outstanding funding by currency: per source, weighted average rate and maturity ladder."""
        when = int(BankJournal.validate_epoch(now))
        currencies = {}
        for row in conn.execute("""SELECT source_type,currency_code,decimal_places,principal_minor_units,
                rate_bps,maturity_epoch FROM institution_funding
                WHERE issuer=? AND status='active' ORDER BY currency_code,source_type""", (str(issuer),)):
            entry = currencies.setdefault(str(row["currency_code"]), {
                "decimal_places": int(row["decimal_places"]), "outstanding_minor_units": 0,
                "weighted": 0, "sources": {}, "ladder": {name: 0 for name, _ in cls.LADDER}})
            units = int(row["principal_minor_units"])
            entry["outstanding_minor_units"] += units
            entry["weighted"] += units * int(row["rate_bps"])
            entry["sources"][row["source_type"]] = entry["sources"].get(row["source_type"], 0) + units
            days = max(0, int(row["maturity_epoch"]) - when) / DAY
            bucket = next(name for name, limit in cls.LADDER if limit is None or days <= limit)
            entry["ladder"][bucket] += units
        for entry in currencies.values():
            total = entry["outstanding_minor_units"]
            entry["weighted_average_rate_bps"] = Decimal(entry.pop("weighted")) / total if total else Decimal(0)
            entry["sources"] = [{"source_type": source, "outstanding_minor_units": units}
                                for source, units in sorted(entry["sources"].items())]
            entry["maturity_ladder"] = [{"bucket": name, "outstanding_minor_units": entry["ladder"][name]}
                                        for name, _ in cls.LADDER]
            del entry["ladder"]
        return {"issuer": str(issuer), "as_of_epoch": when, "currencies": currencies,
                "method": "simulator funding ledger; ACT/365 simple interest; not a regulatory measure"}

    @staticmethod
    def cashflow_projection(conn, issuer, *, now):
        """Project active draws to contractual maturity, or to now if already overdue.

        This is a current-status projection, not a historical reconstruction: rows
        since repaid are excluded even when querying an earlier as-of time.
        """
        when = int(BankJournal.validate_epoch(now))
        cashflows = []
        for row in conn.execute("""SELECT funding_id,source_type,currency_code,decimal_places,
                principal_minor_units,rate_bps,start_epoch,maturity_epoch FROM institution_funding
                WHERE issuer=? AND status='active' AND start_epoch<=?
                ORDER BY maturity_epoch,funding_id""", (str(issuer), when)):
            maturity = int(row['maturity_epoch'])
            settlement = max(when, maturity)
            principal = int(row['principal_minor_units'])
            interest = int((Decimal(principal) * int(row['rate_bps']) * (settlement - int(row['start_epoch']))
                            / (Decimal(10000) * 365 * DAY)).to_integral_value(rounding=ROUND_HALF_UP))
            cashflows.append({
                'funding_id': int(row['funding_id']), 'source_type': row['source_type'],
                'currency_code': row['currency_code'], 'decimal_places': int(row['decimal_places']),
                'maturity_epoch': maturity, 'projected_settlement_epoch': settlement,
                'overdue': maturity < when, 'principal_minor_units': principal,
                'interest_minor_units': interest, 'total_minor_units': principal + interest,
            })
        return {'issuer': str(issuer), 'as_of_epoch': when, 'cashflows': cashflows,
                'method': 'current active draws; ACT/365 simple interest; overdue accrues to as-of; '
                          'not a historical reconstruction or regulatory measure'}
