"""Append-only, currency-minor-unit journal for financial institutions."""
from functions.exact_money import ExactDecimal, native_value
from functions.money import base_currency_code

from decimal import Decimal
from functions.time_utils import MAX_SUPPORTED_EPOCH as _MAX_SUPPORTED_EPOCH, validate_epoch


class BankJournal:
    MAX_SUPPORTED_EPOCH = _MAX_SUPPORTED_EPOCH

    @classmethod
    def validate_epoch(cls, value):
        return validate_epoch(value, "분개 시각은 유효한 UNIX epoch이어야 합니다.")

    @staticmethod
    def reconcile_institution_results(conn, issuer, *, since_epoch=None):
        """Compare native-currency P&L journal movement with legacy result rows.

        Unmapped legacy events are reported, not silently treated as balanced.
        Multiple result types sharing a transaction reference are consolidated.
        """
        if since_epoch is not None:
            since_epoch = BankJournal.validate_epoch(since_epoch)
        from functions.money import from_minor_units

        query = (
            "SELECT reference_key,currency_code,SUM(amount_minor_units) ledger_units,"
            "MAX(amount_decimal_places) decimal_places,"
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
            places = int(ledger["decimal_places"])
            ledger_amount = from_minor_units(int(ledger["ledger_units"] or 0), places)
            journal = conn.execute(
                "SELECT COUNT(DISTINCT e.entry_id) entry_count,"
                "COALESCE(SUM(l.credit_units-l.debit_units),0) pnl_units,"
                "MAX(e.decimal_places) journal_decimal_places "
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
                    "ledger_amount": ExactDecimal(ledger_amount),
                    "journal_amount": None,
                    "difference": None,
                    "status": "missing_journal",
                })
                continue
            journal_units = int(journal["pnl_units"] or 0)
            journal_places = int(journal["journal_decimal_places"])
            journal_amount = from_minor_units(journal_units, journal_places)
            difference = ledger_amount-journal_amount
            results.append({
                "reference_key": ledger["reference_key"],
                "currency_code": ledger["currency_code"],
                "ledger_amount": ExactDecimal(ledger_amount),
                "journal_amount": ExactDecimal(journal_amount),
                "difference": difference,
                "status": "matched" if difference == 0 else "mismatch",
            })
        return results

    @staticmethod
    def trial_balance(conn, issuer, currency_code=None):
        """Return posted account balances in debit-positive minor units."""
        sql = (
            "SELECT e.currency_code,e.decimal_places,l.account_code,"
            "SUM(l.debit_units) debit_units,SUM(l.credit_units) credit_units,"
            "SUM(l.debit_units-l.credit_units) balance_units "
            "FROM bank_journal_entries e JOIN bank_journal_lines l USING(entry_id) "
            "WHERE e.issuer=? AND e.status='posted'"
        )
        params = [str(issuer)]
        if currency_code:
            sql += " AND e.currency_code=?"
            params.append(str(currency_code).upper())
        sql += " GROUP BY e.currency_code,e.decimal_places,l.account_code " \
               "ORDER BY e.currency_code,e.decimal_places,l.account_code"
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
        from functions.money import currency_precision, round_currency, to_minor_units

        accounts = conn.execute(
            "SELECT a.quote_currency,a.account_type,a.balance,a.balance_units "
            "FROM accounts.accounts a JOIN product.saving s ON s.product_code=a.product_code "
            "WHERE s.issuer=? AND a.account_type IN ('saving','currency_saving')",
            (str(issuer),),
        ).fetchall()
        account_units = {}
        for row in accounts:
            currency = str(row["quote_currency"] or base_currency_code(conn)).upper()
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
            try:
                places = currency_precision(conn, currency)
            except ValueError:
                # An unknown currency cannot be scaled; report it instead of dropping the balance.
                results.append({
                    "issuer": str(issuer), "currency_code": currency,
                    "account_units": account_units.get(currency, 0), "journal_units": journal_units.get(currency, 0),
                    "difference_units": account_units.get(currency, 0) - journal_units.get(currency, 0),
                    "decimal_places": 0, "status": "invalid_currency",
                })
                continue
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
        from functions.money import currency_precision, round_currency, to_minor_units

        accounts = conn.execute(
            "SELECT l.quote_currency,l.outstanding_balance,l.outstanding_balance_minor_units "
            "FROM accounts.loans l "
            "JOIN product.liability p USING(product_code) WHERE p.issuer=? "
            "AND l.status IN ('active','defaulted')",
            (str(issuer),),
        ).fetchall()
        account_units = {}
        for row in accounts:
            currency = str(row["quote_currency"] or base_currency_code(conn)).upper()
            places = currency_precision(conn, currency)
            units = (int(row["outstanding_balance_minor_units"] or 0)
                     if "outstanding_balance_minor_units" in row.keys()
                     else to_minor_units(round_currency(
                         conn, Decimal(str(row["outstanding_balance"] or 0)), currency,
                     ), places))
            account_units[currency] = account_units.get(currency, 0) + units

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
        from functions.money import currency_precision, round_currency, to_minor_units

        exposures = conn.execute(
            "SELECT l.quote_currency,l.ecl_allowance,l.ecl_allowance_minor_units "
            "FROM accounts.loans l "
            "JOIN product.liability p USING(product_code) WHERE p.issuer=?",
            (str(issuer),),
        ).fetchall()
        account_units = {}
        for row in exposures:
            currency = str(row["quote_currency"] or base_currency_code(conn)).upper()
            places = currency_precision(conn, currency)
            units = (int(row["ecl_allowance_minor_units"] or 0)
                     if "ecl_allowance_minor_units" in row.keys()
                     else to_minor_units(round_currency(
                         conn, max(0, row["ecl_allowance"] or 0), currency), places))
            account_units[currency] = account_units.get(currency, 0) + units

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
    def reconcile_charged_off_balances(conn, issuer):
        """Reconcile contractual off-balance memo balances to immutable events."""
        from functions.money import currency_precision, to_minor_units

        loans = conn.execute(
            "SELECT l.quote_currency,l.charged_off_balance,"
            "l.charged_off_balance_minor_units FROM accounts.loans l "
            "JOIN product.liability p USING(product_code) WHERE p.issuer=?",
            (str(issuer),),
        ).fetchall()
        source = {}
        for row in loans:
            currency = str(row["quote_currency"] or base_currency_code(conn)).upper()
            places = currency_precision(conn, currency)
            units = (int(row["charged_off_balance_minor_units"] or 0)
                     if "charged_off_balance_minor_units" in row.keys()
                     else to_minor_units(row["charged_off_balance"] or 0, places))
            source[currency] = source.get(currency, 0) + units
        event_rows = conn.execute(
            "SELECT currency_code,SUM(CASE event_type WHEN 'charge_off' "
            "THEN amount_minor_units ELSE -amount_minor_units END) memo_units "
            "FROM accounts.loan_chargeoff_events WHERE issuer=? GROUP BY currency_code",
            (str(issuer),),
        ).fetchall()
        events = {str(row["currency_code"]): int(row["memo_units"] or 0)
                  for row in event_rows}
        currencies = sorted(set(source) | set(events))
        return [{"issuer": str(issuer), "currency_code": currency,
                 "subledger_units": source.get(currency, 0),
                 "event_units": events.get(currency, 0),
                 "difference_units": source.get(currency, 0)-events.get(currency, 0),
                 "decimal_places": currency_precision(conn, currency),
                 "status": "matched" if source.get(currency, 0) == events.get(currency, 0)
                 else "mismatch"} for currency in currencies]

    @staticmethod
    def reconcile_loan_payment_schedules(conn, issuer):
        """Check payment schedule allocation identities against their due amounts.

        This is a schedule-internal control, not a substitute for reconciling
        cash movements to the journal. Values are compared in contract-currency
        minor units so currencies with different scales are handled correctly.
        """
        from functions.money import currency_precision, to_minor_units

        rows = conn.execute(
            "SELECT s.*,l.quote_currency FROM accounts.loan_payment_schedule s "
            "JOIN accounts.loans l USING(loan_id) "
            "JOIN product.liability p USING(product_code) WHERE p.issuer=? "
            "ORDER BY s.schedule_id", (str(issuer),),
        ).fetchall()
        findings = []
        for row in rows:
            currency = str(row["quote_currency"] or base_currency_code(conn)).upper()
            places = currency_precision(conn, currency)
            def units(field):
                minor_field = f"{field}_minor_units"
                if minor_field in row.keys():
                    return int(row[minor_field] or 0)
                return to_minor_units(row[field] or 0, places)
            due = sum(units(field) for field in ("principal_due", "interest_due", "fee_due"))
            paid_components = sum(units(field) for field in
                                  ("principal_paid", "interest_paid", "fee_paid"))
            paid = units("amount_paid")
            legacy_mismatches = [
                field for field in (
                    "principal_due", "interest_due", "fee_due", "amount_paid",
                    "principal_paid", "interest_paid", "fee_paid",
                ) if f"{field}_minor_units" in row.keys()
                and int(row[f"{field}_minor_units"] or 0)
                != to_minor_units(row[field] or 0, places)
            ]
            overallocated = any(units(paid_field) > units(due_field) for due_field, paid_field in (
                ("principal_due", "principal_paid"),
                ("interest_due", "interest_paid"),
                ("fee_due", "fee_paid"),
            ))
            status_invalid = row["status"] == "paid" and paid != due
            if (paid != paid_components or paid > due or overallocated or status_invalid
                    or legacy_mismatches):
                findings.append({
                    "schedule_id": int(row["schedule_id"]),
                    "loan_id": int(row["loan_id"]), "currency_code": currency,
                    "amount_paid_minor_units": paid,
                    "allocated_minor_units": paid_components,
                    "due_minor_units": due,
                    "legacy_mirror_mismatch_fields": legacy_mismatches,
                    "status": "mismatch",
                })
        return findings

    @staticmethod
    def reconcile_deposit_insurance_claims(conn, issuer):
        """Reconcile claim liabilities and canonical payout-unit snapshots."""
        from functions.money import currency_precision, round_currency, to_minor_units

        claims = conn.execute(
            "SELECT COALESCE(a.quote_currency,base_currency_code()) currency_code,"
            "SUM(c.covered_amount) covered_amount,SUM(c.uninsured_amount) uninsured_amount "
            "FROM accounts.deposit_insurance_claims c LEFT JOIN accounts.accounts a "
            "ON a.account_number=c.account_number WHERE c.issuer=? "
            "GROUP BY COALESCE(a.quote_currency,base_currency_code())", (str(issuer),),
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
        payout_rows = conn.execute(
            "SELECT claim_id,payout_currency,payout_amount,payout_amount_minor_units,"
            "payout_decimal_places FROM accounts.deposit_insurance_claims "
            "WHERE issuer=? AND (payout_currency IS NOT NULL OR payout_amount IS NOT NULL "
            "OR payout_amount_minor_units IS NOT NULL)", (str(issuer),),
        ).fetchall()
        payout_by_currency = {}
        for row in payout_rows:
            currency = str(row["payout_currency"] or "")
            entry = payout_by_currency.setdefault(currency, {"units": 0, "invalid": 0})
            if (not currency or row["payout_amount_minor_units"] is None
                    or row["payout_decimal_places"] is None):
                entry["invalid"] += 1
                continue
            try:
                places = currency_precision(conn, currency)
            except ValueError:
                entry["invalid"] += 1
                continue
            canonical = int(row["payout_amount_minor_units"])
            legacy = to_minor_units(row["payout_amount"] or 0, places)
            if int(row["payout_decimal_places"]) != places or canonical != legacy:
                entry["invalid"] += 1
            entry["units"] += canonical
        for currency, entry in sorted(payout_by_currency.items()):
            try:
                places = currency_precision(conn, currency) if currency else None
            except ValueError:
                places = None
            results.append({
                "issuer": str(issuer), "currency_code": currency or None,
                "account_code": "deposit_insurance_payout_subledger",
                "claim_units": entry["units"], "journal_units": None,
                "difference_units": None, "decimal_places": places,
                "unverifiable_claim_count": entry["invalid"],
                "status": "mismatch" if entry["invalid"] else "matched",
            })
        return results

    @staticmethod
    def reconcile_collateral_liquidations(conn, issuer):
        """Reconcile realized collateral proceeds to liquidation events."""
        from functions.money import currency_precision, to_minor_units

        rows = conn.execute(
            "SELECT e.event_id,e.loan_id,e.collateral_id,e.mark_price,e.mark_price_minor_units,"
            "e.execution_price,e.execution_price_minor_units,e.gross_proceeds,"
            "e.gross_proceeds_minor_units,e.liquidation_cost,e.liquidation_cost_minor_units,"
            "e.net_proceeds,e.net_proceeds_minor_units,c.status collateral_status,"
            "c.liquidation_proceeds asset_proceeds,"
            "c.liquidation_proceeds_minor_units asset_proceeds_minor_units,l.quote_currency "
            "FROM accounts.collateral_liquidation_events e "
            "JOIN accounts.loan_collateral_assets c USING(collateral_id) "
            "JOIN accounts.loans l USING(loan_id) "
            "JOIN product.liability p USING(product_code) WHERE p.issuer=? "
            "ORDER BY e.event_id", (str(issuer),),
        ).fetchall()
        findings = []
        for row in rows:
            currency = str(row["quote_currency"] or base_currency_code(conn)).upper()
            places = currency_precision(conn, currency)

            def units(minor, legacy):
                return (int(row[minor] or 0) if minor in row.keys()
                        else to_minor_units(row[legacy] or 0, places))

            gross = units("gross_proceeds_minor_units", "gross_proceeds")
            cost = units("liquidation_cost_minor_units", "liquidation_cost")
            net = units("net_proceeds_minor_units", "net_proceeds")
            asset_net = units("asset_proceeds_minor_units", "asset_proceeds")
            price_mismatches = [
                field for field, legacy, minor in (
                    ("mark_price", "mark_price", "mark_price_minor_units"),
                    ("execution_price", "execution_price", "execution_price_minor_units"),
                )
                if int(row[minor] or 0) != to_minor_units(row[legacy] or 0, places)
            ]
            findings.append({
                "event_id": int(row["event_id"]), "loan_id": int(row["loan_id"]),
                "collateral_id": int(row["collateral_id"]), "currency_code": currency,
                "gross_proceeds_units": gross, "liquidation_cost_units": cost,
                "net_proceeds_units": net, "asset_proceeds_units": asset_net,
                "price_mirror_mismatches": price_mismatches,
                "difference_units": net-asset_net,
                "status": ("matched" if row["collateral_status"] == "liquidated"
                           and gross-cost == net and net == asset_net
                           and not price_mismatches else "mismatch"),
            })
        missing = conn.execute(
            "SELECT c.collateral_id,c.loan_id,l.quote_currency "
            "FROM accounts.loan_collateral_assets c JOIN accounts.loans l USING(loan_id) "
            "JOIN product.liability p USING(product_code) LEFT JOIN "
            "accounts.collateral_liquidation_events e USING(collateral_id) "
            "WHERE p.issuer=? AND c.status='liquidated' AND e.event_id IS NULL",
            (str(issuer),),
        ).fetchall()
        findings.extend({"collateral_id": int(row["collateral_id"]),
                         "loan_id": int(row["loan_id"]),
                         "currency_code": str(row["quote_currency"] or base_currency_code(conn)).upper(),
                         "status": "missing_event"} for row in missing)
        return findings

    @staticmethod
    def reconcile_all(conn, issuer):
        """Run every implemented institution subledger-to-journal reconciliation.

        This is deliberately explicit: it does not imply coverage of consumer
        wallets, market securities, corporate books, or unjournaled legacy
        events. Missing source/journal coverage remains a finding, never a pass.
        """
        checks = {
            "institution_pnl": BankJournal.reconcile_institution_results(conn, issuer),
            "customer_deposits": BankJournal.reconcile_customer_deposits(conn, issuer),
            "loan_receivables": BankJournal.reconcile_loan_receivables(conn, issuer),
            "ecl_allowances": BankJournal.reconcile_ecl_allowances(conn, issuer),
            "charged_off_memo_balances": BankJournal.reconcile_charged_off_balances(conn, issuer),
            "loan_payment_schedules": BankJournal.reconcile_loan_payment_schedules(
                conn, issuer
            ),
            "collateral_liquidations": BankJournal.reconcile_collateral_liquidations(
                conn, issuer
            ),
            "deposit_insurance_claims": BankJournal.reconcile_deposit_insurance_claims(
                conn, issuer
            ),
            "journal_invariants": [
                {**row, "status": "mismatch"}
                for row in BankJournal.audit_unbalanced(conn, issuer)
            ],
        }
        findings = [
            {"subledger": name, **row}
            for name, rows in checks.items()
            for row in rows
            if row.get("status") != "matched"
        ]
        return {
            "issuer": str(issuer),
            "status": "matched" if not findings else "mismatch",
            "checks": checks,
            "findings": findings,
            "coverage": tuple(checks),
        }

    @staticmethod
    def post_tax_remittance(conn, issuer, reference_key, source_currency,
                            source_amount, settlement_currency, settlement_amount,
                            *, description="", now=None):
        """Settle withheld tax, using currency-specific FX clearing legs if needed."""
        conn.execute('SAVEPOINT bank_tax_remittance')
        try:
            result = BankJournal._post_tax_remittance_impl(
                conn, issuer, reference_key, source_currency, source_amount,
                settlement_currency, settlement_amount, description=description, now=now)
        except Exception:
            conn.execute('ROLLBACK TO bank_tax_remittance')
            conn.execute('RELEASE bank_tax_remittance')
            raise
        conn.execute('RELEASE bank_tax_remittance')
        return result

    @staticmethod
    def _post_tax_remittance_impl(conn, issuer, reference_key, source_currency,
                                  source_amount, settlement_currency, settlement_amount,
                                  *, description="", now=None):
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
        if Decimal(str(settlement_amount)) == 0:
            # The converted value is below one settlement-currency minor unit.
            return source_posted
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
        """Post bank and common-ledger entries atomically within the caller's transaction."""
        conn.execute('SAVEPOINT bank_journal_post')
        try:
            result = BankJournal._post_impl(conn, issuer, event_type, reference_key, currency_code,
                                            lines, description=description, now=now)
        except Exception:
            conn.execute('ROLLBACK TO bank_journal_post')
            conn.execute('RELEASE bank_journal_post')
            raise
        conn.execute('RELEASE bank_journal_post')
        return result

    @staticmethod
    def _post_impl(conn, issuer, event_type, reference_key, currency_code, lines,
                   *, description="", now=None):
        """Post balanced lines; ``lines`` is [(account, side, amount), ...].

        Each amount is rounded to the currency's configured precision before it
        is converted to integer minor units. Duplicate references are idempotent.
        """
        if not issuer or not reference_key:
            return False
        from functions.money import currency_precision, to_minor_units

        created_epoch = BankJournal.validate_epoch(now)

        currency = str(currency_code or "").upper()
        places = currency_precision(conn, currency)
        normalized = []
        debit_total = credit_total = 0
        for account, side, amount in lines:
            account = str(account)
            if not conn.execute(
                "SELECT 1 FROM bank_journal_accounts "
                "WHERE account_code=? AND active=1", (account,),
            ).fetchone():
                raise ValueError(f"등록되지 않았거나 비활성인 회계 계정입니다: {account}")
            units = to_minor_units(amount, places)
            if units <= 0:
                continue
            side = str(side).lower()
            if side == "debit":
                debit_total += units
                normalized.append((account, units, 0))
            elif side == "credit":
                credit_total += units
                normalized.append((account, 0, units))
            else:
                raise ValueError("분개 차변/대변 방향이 올바르지 않습니다.")
        if debit_total == 0 or debit_total != credit_total or len(normalized) < 2:
            raise ValueError("금융기관 분개가 통화 최소단위 기준으로 균형을 이루지 않습니다.")
        cursor = conn.execute(
            "INSERT OR IGNORE INTO bank_journal_entries "
            "(issuer,event_type,reference_key,currency_code,created_epoch,description,decimal_places) "
            "VALUES (?,?,?,?,?,?,?)",
            (str(issuer), str(event_type), str(reference_key), currency,
             created_epoch, str(description or ""), places),
        )
        if cursor.rowcount != 1:
            existing = conn.execute("""SELECT entry_id,currency_code,decimal_places,status
                FROM bank_journal_entries WHERE issuer=? AND event_type=? AND reference_key=?""",
                (str(issuer), str(event_type), str(reference_key))).fetchone()
            old_lines = (conn.execute("""SELECT account_code,debit_units,credit_units
                FROM bank_journal_lines WHERE entry_id=?""", (existing['entry_id'],)).fetchall()
                if existing else [])
            if (not existing or existing['status'] != 'posted'
                or (existing['currency_code'], existing['decimal_places']) != (currency, places)
                or sorted(tuple(row) for row in old_lines) != sorted(normalized)):
                raise ValueError('동일한 금융기관 원장 멱등성 키에 다른 통화 또는 분개가 들어왔습니다.')
            from managers.accounting_journal import AccountingJournal
            AccountingJournal.rebuild_from_subledgers(conn)
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
        from managers.accounting_journal import AccountingJournal
        common_lines = []
        AccountingJournal.register_mappings(conn, "institution", [(account, account) for account, _, _ in normalized])
        for account, debit, credit in normalized:
            if debit:
                common_lines.append((account, "debit", debit))
            if credit:
                common_lines.append((account, "credit", credit))
        AccountingJournal.post(
            conn, ledger_scope="institution", entity_type="financial_institution",
            entity_id=issuer, source_key=f"{event_type}:{reference_key}",
            currency_code=currency, decimal_places=places,
            recorded_epoch=int(created_epoch), lines=common_lines,
            description=description,
        )
        return True
