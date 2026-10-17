"""Append-only, currency-minor-unit journal for financial institutions."""

from decimal import Decimal
import time


class BankJournal:
    @staticmethod
    def reconcile_institution_results(conn, issuer, *, since_epoch=None):
        """Compare native-currency P&L journal movement with legacy result rows.

        Unmapped legacy events are reported, not silently treated as balanced.
        Multiple result types sharing a transaction reference are consolidated.
        """
        from functions.money import currency_precision, round_currency

        query = (
            "SELECT reference_key,currency_code,SUM(amount) ledger_amount,"
            "MIN(created_epoch) first_epoch FROM financial_institution_ledger "
            "WHERE issuer=?"
        )
        params = [str(issuer)]
        if since_epoch is not None:
            query += " AND created_epoch>=?"
            params.append(float(since_epoch))
        query += " GROUP BY reference_key,currency_code ORDER BY first_epoch,reference_key"
        pnl_accounts = (
            "interest_income", "interest_expense", "fee_income",
            "impairment_expense", "impairment_reversal",
            "credit_loss_expense", "credit_loss_recovery_income",
        )
        placeholders = ",".join("?" for _ in pnl_accounts)
        results = []
        for ledger in conn.execute(query, params).fetchall():
            journal = conn.execute(
                "SELECT COUNT(DISTINCT e.entry_id) entry_count,"
                "COALESCE(SUM(l.credit_units-l.debit_units),0) pnl_units "
                "FROM bank_journal_entries e JOIN bank_journal_lines l USING(entry_id) "
                f"WHERE e.issuer=? AND e.reference_key=? AND e.currency_code=? "
                f"AND e.status='posted' AND l.account_code IN ({placeholders})",
                (str(issuer), str(ledger["reference_key"]),
                 str(ledger["currency_code"]), *pnl_accounts),
            ).fetchone()
            if not int(journal["entry_count"] or 0):
                results.append({
                    "reference_key": ledger["reference_key"],
                    "currency_code": ledger["currency_code"],
                    "ledger_amount": ledger["ledger_amount"],
                    "journal_amount": None,
                    "difference": None,
                    "status": "missing_journal",
                })
                continue
            places = currency_precision(conn, ledger["currency_code"])
            journal_amount = Decimal(int(journal["pnl_units"]))/Decimal(10**places)
            difference = round_currency(
                conn, Decimal(str(ledger["ledger_amount"]))-journal_amount,
                ledger["currency_code"],
            )
            results.append({
                "reference_key": ledger["reference_key"],
                "currency_code": ledger["currency_code"],
                "ledger_amount": ledger["ledger_amount"],
                "journal_amount": float(journal_amount),
                "difference": difference,
                "status": "matched" if difference == 0 else "mismatch",
            })
        return results

    @staticmethod
    def trial_balance(conn, issuer, currency_code=None):
        """Return posted account balances in debit-positive minor units."""
        sql = (
            "SELECT e.currency_code,l.account_code,"
            "SUM(l.debit_units) debit_units,SUM(l.credit_units) credit_units,"
            "SUM(l.debit_units-l.credit_units) balance_units "
            "FROM bank_journal_entries e JOIN bank_journal_lines l USING(entry_id) "
            "WHERE e.issuer=? AND e.status='posted'"
        )
        params = [str(issuer)]
        if currency_code:
            sql += " AND e.currency_code=?"
            params.append(str(currency_code).upper())
        sql += " GROUP BY e.currency_code,l.account_code ORDER BY e.currency_code,l.account_code"
        return [dict(row) for row in conn.execute(sql, params).fetchall()]

    @staticmethod
    def audit_unbalanced(conn, issuer=None):
        """Find any persisted posted entries that fail the double-entry invariant."""
        sql = (
            "SELECT e.entry_id,e.issuer,e.event_type,e.reference_key,e.currency_code,"
            "COUNT(l.line_id) line_count,COALESCE(SUM(l.debit_units),0) debit_units,"
            "COALESCE(SUM(l.credit_units),0) credit_units "
            "FROM bank_journal_entries e LEFT JOIN bank_journal_lines l USING(entry_id) "
            "WHERE e.status='posted'"
        )
        params = []
        if issuer is not None:
            sql += " AND e.issuer=?"
            params.append(str(issuer))
        # Repeat aggregate expressions here: SQLite can resolve these aliases
        # to the source line columns inside HAVING, yielding false positives.
        sql += (" GROUP BY e.entry_id HAVING COUNT(l.line_id)<2 OR "
                "COALESCE(SUM(l.debit_units),0)<>COALESCE(SUM(l.credit_units),0)")
        return [dict(row) for row in conn.execute(sql, params).fetchall()]

    @staticmethod
    def reconcile_customer_deposits(conn, issuer):
        """Compare posted deposit liabilities with live deposit-account balances.

        Balances created before journal coverage was introduced are intentionally
        reported as missing/mismatched rather than silently repaired.
        """
        from functions.money import currency_precision, round_currency

        accounts = conn.execute(
            "SELECT a.quote_currency,a.account_type,a.balance,a.balance_units "
            "FROM accounts.accounts a JOIN product.saving s ON s.product_code=a.product_code "
            "WHERE s.issuer=? AND a.account_type IN ('saving','currency_saving')",
            (str(issuer),),
        ).fetchall()
        account_units = {}
        for row in accounts:
            currency = str(row["quote_currency"] or "NWX").upper()
            places = currency_precision(conn, currency)
            balance = (Decimal(int(row["balance_units"] or 0)) / Decimal(1_000_000)
                       if row["account_type"] == "currency_saving"
                       else Decimal(str(row["balance"] or 0)))
            rounded = Decimal(str(round_currency(conn, balance, currency)))
            units = int(rounded * (10 ** places))
            account_units[currency] = account_units.get(currency, 0) + units

        journal = conn.execute(
            "SELECT e.currency_code,SUM(l.credit_units-l.debit_units) liability_units "
            "FROM bank_journal_entries e JOIN bank_journal_lines l USING(entry_id) "
            "WHERE e.issuer=? AND e.status='posted' "
            "AND l.account_code='customer_deposits_payable' "
            "GROUP BY e.currency_code",
            (str(issuer),),
        ).fetchall()
        journal_units = {str(row["currency_code"]): int(row["liability_units"] or 0)
                         for row in journal}
        currencies = sorted(set(account_units) | set(journal_units))
        results = []
        for currency in currencies:
            places = currency_precision(conn, currency)
            actual = account_units.get(currency, 0)
            posted = journal_units.get(currency, 0)
            results.append({
                "issuer": str(issuer), "currency_code": currency,
                "account_units": actual, "journal_units": posted,
                "difference_units": actual-posted, "decimal_places": places,
                "status": ("missing_journal" if actual and not posted else
                           "matched" if actual == posted else "mismatch"),
            })
        return results

    @staticmethod
    def reconcile_loan_receivables(conn, issuer):
        """Compare live gross loan balances with the posted receivable account."""
        from functions.money import currency_precision, round_currency

        accounts = conn.execute(
            "SELECT l.quote_currency,l.outstanding_balance FROM accounts.loans l "
            "JOIN product.liability p USING(product_code) WHERE p.issuer=? "
            "AND l.status IN ('active','defaulted')",
            (str(issuer),),
        ).fetchall()
        account_units = {}
        for row in accounts:
            currency = str(row["quote_currency"] or "NWX").upper()
            places = currency_precision(conn, currency)
            rounded = Decimal(str(round_currency(
                conn, Decimal(str(row["outstanding_balance"] or 0)), currency,
            )))
            account_units[currency] = account_units.get(currency, 0) + int(
                rounded * (10 ** places)
            )

        journal = conn.execute(
            "SELECT e.currency_code,SUM(l.debit_units-l.credit_units) receivable_units "
            "FROM bank_journal_entries e JOIN bank_journal_lines l USING(entry_id) "
            "WHERE e.issuer=? AND e.status='posted' AND l.account_code='loans_receivable' "
            "GROUP BY e.currency_code",
            (str(issuer),),
        ).fetchall()
        journal_units = {str(row["currency_code"]): int(row["receivable_units"] or 0)
                         for row in journal}
        currencies = sorted(set(account_units) | set(journal_units))
        return [{
            "issuer": str(issuer), "currency_code": currency,
            "account_units": account_units.get(currency, 0),
            "journal_units": journal_units.get(currency, 0),
            "difference_units": account_units.get(currency, 0)-journal_units.get(currency, 0),
            "decimal_places": currency_precision(conn, currency),
            "status": ("missing_journal" if account_units.get(currency, 0)
                       and not journal_units.get(currency, 0) else
                       "matched" if account_units.get(currency, 0) == journal_units.get(currency, 0)
                       else "mismatch"),
        } for currency in currencies]

    @staticmethod
    def reconcile_ecl_allowances(conn, issuer):
        """Compare loan-level ECL allowances with the posted contra-asset balance."""
        from functions.money import currency_precision, round_currency

        exposures = conn.execute(
            "SELECT l.quote_currency,l.ecl_allowance FROM accounts.loans l "
            "JOIN product.liability p USING(product_code) WHERE p.issuer=?",
            (str(issuer),),
        ).fetchall()
        account_units = {}
        for row in exposures:
            currency = str(row["quote_currency"] or "NWX").upper()
            places = currency_precision(conn, currency)
            amount = round_currency(conn, max(0, row["ecl_allowance"] or 0), currency)
            account_units[currency] = account_units.get(currency, 0) + int(
                Decimal(str(amount)) * (10 ** places)
            )

        journal = conn.execute(
            "SELECT e.currency_code,SUM(l.credit_units-l.debit_units) allowance_units "
            "FROM bank_journal_entries e JOIN bank_journal_lines l USING(entry_id) "
            "WHERE e.issuer=? AND e.status='posted' AND l.account_code='ecl_allowance' "
            "GROUP BY e.currency_code",
            (str(issuer),),
        ).fetchall()
        journal_units = {str(row["currency_code"]): int(row["allowance_units"] or 0)
                         for row in journal}
        currencies = sorted(set(account_units) | set(journal_units))
        return [{
            "issuer": str(issuer), "currency_code": currency,
            "account_units": account_units.get(currency, 0),
            "journal_units": journal_units.get(currency, 0),
            "difference_units": account_units.get(currency, 0)-journal_units.get(currency, 0),
            "decimal_places": currency_precision(conn, currency),
            "status": ("missing_journal" if account_units.get(currency, 0)
                       and not journal_units.get(currency, 0) else
                       "matched" if account_units.get(currency, 0) == journal_units.get(currency, 0)
                       else "mismatch"),
        } for currency in currencies]

    @staticmethod
    def reconcile_deposit_insurance_claims(conn, issuer):
        """Compare resolved claim totals with the two posted liability accounts."""
        from functions.money import currency_precision, round_currency

        claims = conn.execute(
            "SELECT COALESCE(a.quote_currency,'NWX') currency_code,"
            "SUM(c.covered_amount) covered_amount,SUM(c.uninsured_amount) uninsured_amount "
            "FROM accounts.deposit_insurance_claims c LEFT JOIN accounts.accounts a "
            "ON a.account_number=c.account_number WHERE c.issuer=? "
            "GROUP BY COALESCE(a.quote_currency,'NWX')", (str(issuer),),
        ).fetchall()
        posted = conn.execute(
            "SELECT e.currency_code,l.account_code,SUM(l.credit_units-l.debit_units) balance_units "
            "FROM bank_journal_entries e JOIN bank_journal_lines l USING(entry_id) "
            "WHERE e.issuer=? AND e.status='posted' AND l.account_code IN "
            "('deposit_insurance_payable','uninsured_deposit_claim') "
            "GROUP BY e.currency_code,l.account_code", (str(issuer),),
        ).fetchall()
        expected = {str(row['currency_code']): row for row in claims}
        actual = {(str(row['currency_code']), str(row['account_code'])):
                  int(row['balance_units'] or 0) for row in posted}
        currencies = sorted(set(expected) | {currency for currency, _ in actual})
        results = []
        for currency in currencies:
            places = currency_precision(conn, currency)
            scale = 10 ** places
            claim = expected.get(currency)
            for code, field in (("deposit_insurance_payable", "covered_amount"),
                                ("uninsured_deposit_claim", "uninsured_amount")):
                amount = round_currency(conn, Decimal(str(
                    claim[field] if claim is not None and claim[field] is not None else 0
                )), currency)
                expected_units = int(Decimal(str(amount)) * scale)
                actual_units = actual.get((currency, code), 0)
                difference = expected_units - actual_units
                results.append({
                    "issuer": str(issuer), "currency_code": currency,
                    "account_code": code, "claim_units": expected_units,
                    "journal_units": actual_units, "difference_units": difference,
                    "decimal_places": places,
                    "status": ("missing_journal" if expected_units and not actual_units else
                               "matched" if difference == 0 else "mismatch"),
                })
        return results

    @staticmethod
    def post_tax_remittance(conn, issuer, reference_key, source_currency,
                            source_amount, settlement_currency, settlement_amount,
                            *, description="", now=None):
        """Settle withheld tax, using currency-specific FX clearing legs if needed."""
        source = str(source_currency or "").upper()
        settlement = str(settlement_currency or "").upper()
        if source == settlement:
            return BankJournal.post(
                conn, issuer, "tax_remittance", reference_key, source,
                [("tax_payable", "debit", source_amount),
                 ("settlement_asset", "credit", source_amount)],
                description=description, now=now,
            )
        source_posted = BankJournal.post(
            conn, issuer, "tax_remittance", f"{reference_key}:source", source,
            [("tax_payable", "debit", source_amount),
             ("fx_conversion_clearing", "credit", source_amount)],
            description=f"{description} · source currency settlement",
            now=now,
        )
        settlement_posted = BankJournal.post(
            conn, issuer, "tax_remittance", f"{reference_key}:settlement", settlement,
            [("fx_conversion_clearing", "debit", settlement_amount),
             ("settlement_asset", "credit", settlement_amount)],
            description=f"{description} · treasury settlement currency",
            now=now,
        )
        return source_posted or settlement_posted

    @staticmethod
    def post(conn, issuer, event_type, reference_key, currency_code, lines,
             *, description="", now=None):
        """Post balanced lines; ``lines`` is [(account, side, amount), ...].

        Each amount is rounded to the currency's configured precision before it
        is converted to integer minor units. Duplicate references are idempotent.
        """
        if not issuer or not reference_key:
            return False
        from functions.money import currency_precision, round_currency

        currency = str(currency_code or "").upper()
        places = currency_precision(conn, currency)
        scale = 10 ** places
        normalized = []
        debit_total = credit_total = 0
        for account, side, amount in lines:
            rounded = round_currency(conn, amount, currency)
            units = int(Decimal(str(rounded)) * scale)
            if units <= 0:
                continue
            side = str(side).lower()
            if side == "debit":
                debit_total += units
                normalized.append((str(account), units, 0))
            elif side == "credit":
                credit_total += units
                normalized.append((str(account), 0, units))
            else:
                raise ValueError("분개 차변/대변 방향이 올바르지 않습니다.")
        if debit_total == 0 or debit_total != credit_total or len(normalized) < 2:
            raise ValueError("금융기관 분개가 통화 최소단위 기준으로 균형을 이루지 않습니다.")
        cursor = conn.execute(
            "INSERT OR IGNORE INTO bank_journal_entries "
            "(issuer,event_type,reference_key,currency_code,created_epoch,description) "
            "VALUES (?,?,?,?,?,?)",
            (str(issuer), str(event_type), str(reference_key), currency,
             float(now) if now is not None else time.time(),
             str(description or "")),
        )
        if cursor.rowcount != 1:
            return False
        entry_id = int(cursor.lastrowid)
        conn.executemany(
            "INSERT INTO bank_journal_lines(entry_id,account_code,debit_units,credit_units) "
            "VALUES (?,?,?,?)",
            [(entry_id, account, debit, credit)
             for account, debit, credit in normalized],
        )
        conn.execute(
            "UPDATE bank_journal_entries SET status='posted' WHERE entry_id=?",
            (entry_id,),
        )
        return True
