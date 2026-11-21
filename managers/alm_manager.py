"""Capital, leverage, liquidity (LCR) and stable-funding (NSFR) ratios of a financial institution.

Simulator policy, not a regulatory filing. The institution has no asset ledger, so the balance sheet is modelled from what
exists: loans (net of expected-credit-loss allowances), customer deposits, wholesale funding and equity. Whatever the
liabilities and equity fund beyond the loans is cash and central-bank reserves (HQLA level 1); this is stated in the report.
Every figure is computed in the institution's base currency with exact decimals. Thresholds are Basel-style defaults that an
administrator can override per jurisdiction (``prudential_parameters``); the report says which ones it used.
"""
from decimal import Decimal

from functions.money import currency_precision
from managers.currency_manager import CurrencyManager, SCALE
from managers.rate_manager import RateManager

DAY = 86400
BPS = Decimal(10000)

# Basis points unless noted. Minimum ratios follow the Basel III standardised levels; the run-off, inflow and stable-funding
# factors are the usual supervisory ones, simplified to the categories this simulator can tell apart.
PRUDENTIAL_DEFAULTS = {
    "cet1_min_bps": 450, "tier1_min_bps": 600, "total_min_bps": 800,
    "conservation_buffer_bps": 250, "countercyclical_buffer_bps": 0, "systemic_buffer_bps": 0,
    "leverage_min_bps": 300, "lcr_min_bps": 10000, "nsfr_min_bps": 10000,
    "t2_general_provision_cap_bps": 125,          # stage 1-2 allowances count as Tier 2 up to 1.25% of RWA
    "runoff_retail_stable_bps": 500, "runoff_retail_less_stable_bps": 1000, "runoff_corporate_bps": 4000,
    "runoff_unsecured_wholesale_bps": 10000, "runoff_repo_bps": 2500, "runoff_central_bank_bps": 0,
    "inflow_performing_loan_bps": 5000, "inflow_cap_bps": 7500,
    "asf_equity_bps": 10000, "asf_retail_stable_bps": 9500, "asf_retail_less_stable_bps": 9000, "asf_corporate_bps": 5000,
    "asf_wholesale_1y_plus_bps": 10000, "asf_wholesale_6m_to_1y_bps": 5000, "asf_wholesale_under_6m_bps": 0,
    "rsf_cash_bps": 0, "rsf_loan_under_1y_bps": 5000, "rsf_loan_1y_plus_bps": 8500, "rsf_defaulted_loan_bps": 10000,
}


class ALMManager:
    @staticmethod
    def parameters(conn, nationality):
        """(effective parameters, names overridden for this jurisdiction)."""
        values, overridden = dict(PRUDENTIAL_DEFAULTS), []
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='prudential_parameters'").fetchone()
        if table:
            for row in conn.execute("SELECT param_key,value_bps FROM prudential_parameters WHERE nationality=?",
                                    (str(nationality or ""),)):
                if row["param_key"] in values:
                    values[row["param_key"]] = int(row["value_bps"])
                    overridden.append(row["param_key"])
        return values, sorted(overridden)

    @staticmethod
    def set_parameter(conn, nationality, key, value_bps, *, actor, now):
        """An administrator's per-jurisdiction override; refuses unknown names and impossible values."""
        key, nationality = str(key), str(nationality or "").strip()
        if key not in PRUDENTIAL_DEFAULTS or not nationality:
            raise ValueError("invalid_prudential_parameter")
        if type(value_bps) is not int or not 0 <= value_bps <= 100000:
            raise ValueError("invalid_prudential_parameter_value")
        conn.execute("""INSERT INTO prudential_parameters(nationality,param_key,value_bps,updated_by,updated_epoch)
            VALUES (?,?,?,?,?) ON CONFLICT(nationality,param_key) DO UPDATE SET value_bps=excluded.value_bps,
            updated_by=excluded.updated_by,updated_epoch=excluded.updated_epoch""",
                     (nationality, key, value_bps, str(actor), int(now)))

    @staticmethod
    def _dec(value):
        return Decimal(str(value if value is not None else 0))

    @staticmethod
    def _to_base(conn, amount, currency, base):
        """``amount`` (native units of ``currency``) in the base currency, with the exchange rounding the app already uses."""
        amount = Decimal(amount)
        if amount == 0 or currency == base:
            return amount
        return Decimal(CurrencyManager.convert(conn, int(amount * SCALE), currency, base)) / SCALE

    @staticmethod
    def _status(value_percent, requirement_percent, warn_margin_percent=1):
        """breach below the requirement, warning within a point of it, ok above."""
        if value_percent is None:
            return "n/a"
        if value_percent < requirement_percent:
            return "breach"
        return "warning" if value_percent < requirement_percent + warn_margin_percent else "ok"

    @classmethod
    def _percent(cls, numerator, denominator):
        return None if denominator is None or denominator <= 0 else float(Decimal(numerator) * 100 / Decimal(denominator))

    @classmethod
    def report(cls, conn, issuer, *, now):
        issuer = str(issuer or "").strip()
        if not issuer:
            raise ValueError("financial_institution_not_found")
        now = int(now)
        sheet = RateManager.institution_balance_sheet(conn, issuer)
        base = str(sheet["base_currency"])
        places = currency_precision(conn, base)
        terms = RateManager.institution_components(conn, issuer)
        nationality = conn.execute("SELECT nationality FROM financial_institutions WHERE issuer=?", (issuer,)).fetchone()
        nationality = str(nationality["nationality"]) if nationality and nationality["nationality"] else ""
        param, overridden = cls.parameters(conn, nationality)
        insurance_limit = max(Decimal(0), cls._dec(terms["deposit_insurance_limit"]))

        # ---- deposits, split into retail stable / retail less stable / corporate -----------------------------------------
        per_owner = {}
        for query in (
            "SELECT a.user_id,a.quote_currency,a.deposit_insured,SUM(MAX(0,CASE WHEN a.account_type='currency_saving' "
            "THEN a.balance_units/1000000.0 ELSE a.balance END)) amount FROM accounts a "
            "JOIN saving s ON s.product_code=a.product_code WHERE s.issuer=? "
            "AND a.account_type IN ('saving','currency_saving') AND a.resolution_status='active' "
            "GROUP BY a.user_id,a.quote_currency,a.deposit_insured",
            "SELECT a.user_id,a.quote_currency,a.deposit_insured,SUM(MAX(0,a.balance)) amount FROM accounts a "
            "JOIN liability l ON l.product_code=a.product_code WHERE l.issuer=? AND a.account_type='loan_payout' "
            "GROUP BY a.user_id,a.quote_currency,a.deposit_insured",
        ):
            for row in conn.execute(query, (issuer,)):
                amount = cls._to_base(conn, cls._dec(row["amount"]), str(row["quote_currency"] or base), base)
                if amount <= 0:
                    continue
                owner = per_owner.setdefault(str(row["user_id"]), {"insured": Decimal(0), "uninsured": Decimal(0)})
                owner["insured" if int(row["deposit_insured"] or 0) else "uninsured"] += amount
        retail_stable = retail_less_stable = corporate = Decimal(0)
        for owner_id, owner in per_owner.items():
            if owner_id.startswith("company:"):
                corporate += owner["insured"] + owner["uninsured"]
                continue
            covered = min(owner["insured"], insurance_limit)
            retail_stable += covered
            retail_less_stable += owner["insured"] - covered + owner["uninsured"]
        deposits = retail_stable + retail_less_stable + corporate

        # ---- loans (gross, by residual maturity), allowances and 30-day inflows ------------------------------------------
        gross_loans = general_allowance = specific_allowance = inflows_30d = Decimal(0)
        loans_under_1y = loans_1y_plus = loans_defaulted = Decimal(0)
        horizon = now + 30 * DAY
        for loan in conn.execute(
            "SELECT l.loan_id,l.outstanding_balance,l.quote_currency,l.status,l.collection_status,l.maturity_epoch,"
            "(SELECT e.stage FROM loan_ecl_assessments e WHERE e.loan_id=l.loan_id "
            "ORDER BY e.as_of_day DESC,e.assessment_id DESC LIMIT 1) stage,"
            "(SELECT e.expected_loss FROM loan_ecl_assessments e WHERE e.loan_id=l.loan_id "
            "ORDER BY e.as_of_day DESC,e.assessment_id DESC LIMIT 1) expected_loss "
            "FROM loans l JOIN liability p ON p.product_code=l.product_code "
            "WHERE p.issuer=? AND l.status IN ('active','defaulted') AND l.outstanding_balance>0", (issuer,)):
            currency = str(loan["quote_currency"] or base)
            balance = cls._to_base(conn, max(Decimal(0), cls._dec(loan["outstanding_balance"])), currency, base)
            loss = cls._to_base(conn, max(Decimal(0), cls._dec(loan["expected_loss"])), currency, base)
            stage = int(loan["stage"] or 1)
            defaulted = loan["status"] == "defaulted" or loan["collection_status"] == "defaulted" or stage >= 3
            gross_loans += balance
            if defaulted:
                loans_defaulted += balance
                specific_allowance += loss
            else:
                general_allowance += loss
                maturity = loan["maturity_epoch"]
                if maturity is not None and float(maturity) - now > 365 * DAY:
                    loans_1y_plus += balance
                else:
                    loans_under_1y += balance
        for row in conn.execute(
            "SELECT l.quote_currency,SUM(MAX(0,s.principal_due+s.interest_due+s.fee_due-s.amount_paid)) amount "
            "FROM loans l JOIN liability p ON p.product_code=l.product_code "
            "JOIN loan_payment_schedule s ON s.loan_id=l.loan_id WHERE p.issuer=? AND l.status='active' "
            "AND l.collection_status='current' AND s.status IN ('scheduled','partial') AND s.due_epoch>=? AND s.due_epoch<=? "
            "GROUP BY l.quote_currency", (issuer, now, horizon)):
            inflows_30d += cls._to_base(conn, max(Decimal(0), cls._dec(row["amount"])), str(row["quote_currency"] or base), base)
        allowance = general_allowance + specific_allowance
        net_loans = max(Decimal(0), gross_loans - allowance)

        # ---- wholesale funding ---------------------------------------------------------------------------------------------
        wholesale = Decimal(0)
        funding_due_30d = {}   # source type -> (balance due within 30 days, run-off rate in bps)
        asf_wholesale = {"1y_plus": Decimal(0), "6m_to_1y": Decimal(0), "under_6m": Decimal(0)}
        for row in conn.execute(
            "SELECT source_type,currency_code,decimal_places,principal_minor_units,maturity_epoch FROM institution_funding "
            "WHERE issuer=? AND status='active' AND start_epoch<=?", (issuer, now)):
            principal = cls._to_base(conn, Decimal(int(row["principal_minor_units"])) / (10 ** int(row["decimal_places"])),
                                     str(row["currency_code"]), base)
            wholesale += principal
            residual = int(row["maturity_epoch"]) - now
            if residual <= 30 * DAY:
                rate = {"central_bank": param["runoff_central_bank_bps"], "repo": param["runoff_repo_bps"]}.get(
                    str(row["source_type"]), param["runoff_unsecured_wholesale_bps"])
                due, _ = funding_due_30d.get(str(row["source_type"]), (Decimal(0), rate))
                funding_due_30d[str(row["source_type"])] = (due + principal, rate)
            asf_wholesale["1y_plus" if residual > 365 * DAY else "6m_to_1y" if residual > 182 * DAY else "under_6m"] += principal

        equity = cls._dec(terms["effective_equity"])
        cash = deposits + wholesale + equity - net_loans
        funding_gap = max(Decimal(0), -cash)            # loans larger than everything that funds them: the model is inconsistent
        cash = max(Decimal(0), cash)
        total_assets = cash + net_loans

        # ---- capital and leverage ------------------------------------------------------------------------------------------
        rwa = cls._dec(sheet["risk_weighted_assets"])
        cet1 = equity
        at1 = Decimal(0)
        tier1 = cet1 + at1
        tier2 = min(general_allowance, rwa * param["t2_general_provision_cap_bps"] / BPS) if rwa > 0 else Decimal(0)
        total_capital = tier1 + tier2
        buffers = {"conservation": param["conservation_buffer_bps"] / 100, "countercyclical": param["countercyclical_buffer_bps"] / 100,
                   "systemic": param["systemic_buffer_bps"] / 100}
        buffer_total = sum(buffers.values())
        requirements = {"cet1": param["cet1_min_bps"] / 100 + buffer_total, "tier1": param["tier1_min_bps"] / 100 + buffer_total,
                        "total": param["total_min_bps"] / 100 + buffer_total}
        ratios = {"cet1": cls._percent(cet1, rwa), "tier1": cls._percent(tier1, rwa), "total": cls._percent(total_capital, rwa)}
        capital = {
            "cet1": cet1, "at1": at1, "tier1": tier1, "tier2": tier2, "total": total_capital, "riskWeightedAssets": rwa,
            "ratiosPercent": ratios, "minimumsPercent": {"cet1": param["cet1_min_bps"] / 100, "tier1": param["tier1_min_bps"] / 100,
                                                         "total": param["total_min_bps"] / 100},
            "buffersPercent": buffers, "requirementsPercent": requirements,
            "headroomPercent": {key: (None if ratios[key] is None else ratios[key] - requirements[key]) for key in ratios},
            "status": {key: cls._status(ratios[key], requirements[key]) for key in ratios},
        }
        leverage_ratio = cls._percent(tier1, total_assets)
        leverage = {"tier1": tier1, "exposure": total_assets, "ratioPercent": leverage_ratio,
                    "minimumPercent": param["leverage_min_bps"] / 100,
                    "status": cls._status(leverage_ratio, param["leverage_min_bps"] / 100)}

        # ---- LCR -----------------------------------------------------------------------------------------------------------
        outflow_rows = [
            ("retail_stable_deposits", retail_stable, param["runoff_retail_stable_bps"]),
            ("retail_less_stable_deposits", retail_less_stable, param["runoff_retail_less_stable_bps"]),
            ("corporate_deposits", corporate, param["runoff_corporate_bps"]),
        ] + [(f"funding_{source}_due_30d", balance, rate) for source, (balance, rate) in sorted(funding_due_30d.items())]
        outflows = [{"category": name, "balance": balance, "rateBps": rate, "amount": balance * rate / BPS}
                    for name, balance, rate in outflow_rows]
        total_outflows = sum((row["amount"] for row in outflows), Decimal(0))
        inflow_gross = inflows_30d * param["inflow_performing_loan_bps"] / BPS
        inflow_capped = min(inflow_gross, total_outflows * param["inflow_cap_bps"] / BPS)
        net_outflows = total_outflows - inflow_capped
        lcr_ratio = cls._percent(cash, net_outflows)
        lcr = {"hqla": {"level1": cash, "level2a": Decimal(0), "level2b": Decimal(0), "total": cash},
               "outflows": outflows, "totalOutflows": total_outflows,
               "inflows": {"performingLoanPayments30d": inflows_30d, "rateBps": param["inflow_performing_loan_bps"],
                           "grossInflows": inflow_gross, "capBps": param["inflow_cap_bps"], "countedInflows": inflow_capped},
               "netOutflows": net_outflows, "ratioPercent": lcr_ratio, "minimumPercent": param["lcr_min_bps"] / 100,
               "status": "ok" if net_outflows <= 0 else cls._status(lcr_ratio, param["lcr_min_bps"] / 100, warn_margin_percent=10)}

        # ---- NSFR ----------------------------------------------------------------------------------------------------------
        asf_rows = [("equity", equity, param["asf_equity_bps"]),
                    ("retail_stable_deposits", retail_stable, param["asf_retail_stable_bps"]),
                    ("retail_less_stable_deposits", retail_less_stable, param["asf_retail_less_stable_bps"]),
                    ("corporate_deposits", corporate, param["asf_corporate_bps"]),
                    ("wholesale_1y_plus", asf_wholesale["1y_plus"], param["asf_wholesale_1y_plus_bps"]),
                    ("wholesale_6m_to_1y", asf_wholesale["6m_to_1y"], param["asf_wholesale_6m_to_1y_bps"]),
                    ("wholesale_under_6m", asf_wholesale["under_6m"], param["asf_wholesale_under_6m_bps"])]
        rsf_rows = [("cash_and_reserves", cash, param["rsf_cash_bps"]),
                    ("loans_under_1y", loans_under_1y, param["rsf_loan_under_1y_bps"]),
                    ("loans_1y_plus", loans_1y_plus, param["rsf_loan_1y_plus_bps"]),
                    ("defaulted_loans", loans_defaulted, param["rsf_defaulted_loan_bps"])]
        # Allowances reduce the loans' carrying value; take them off the buckets in proportion so the RSF base is the net book value.
        if gross_loans > 0 and allowance > 0:
            keep = net_loans / gross_loans
            rsf_rows = [(name, balance * (keep if name != "cash_and_reserves" else 1), rate) for name, balance, rate in rsf_rows]
        asf = [{"category": name, "balance": balance, "factorBps": rate, "amount": balance * rate / BPS} for name, balance, rate in asf_rows]
        rsf = [{"category": name, "balance": balance, "factorBps": rate, "amount": balance * rate / BPS} for name, balance, rate in rsf_rows]
        available = sum((row["amount"] for row in asf), Decimal(0))
        required = sum((row["amount"] for row in rsf), Decimal(0))
        nsfr_ratio = cls._percent(available, required)
        nsfr = {"availableStableFunding": available, "requiredStableFunding": required, "asf": asf, "rsf": rsf,
                "ratioPercent": nsfr_ratio, "minimumPercent": param["nsfr_min_bps"] / 100,
                "status": "ok" if required <= 0 else cls._status(nsfr_ratio, param["nsfr_min_bps"] / 100, warn_margin_percent=10)}

        return {
            "issuer": issuer, "jurisdiction": nationality, "currency": base, "decimalPlaces": places, "asOfEpoch": now,
            "balanceSheet": {"cashAndReserves": cash, "grossLoans": gross_loans, "allowance": allowance, "netLoans": net_loans,
                             "totalAssets": total_assets,
                             "deposits": {"retailStable": retail_stable, "retailLessStable": retail_less_stable,
                                          "corporate": corporate, "total": deposits},
                             "wholesaleFunding": wholesale, "equity": equity, "fundingGap": funding_gap},
            "capital": capital, "leverage": leverage, "lcr": lcr, "nsfr": nsfr,
            "parameters": {"values": param, "overriddenForJurisdiction": overridden},
            "method": ("simulator policy, not a regulatory filing; no asset ledger: cash and reserves (HQLA level 1) are what deposits, "
                       "wholesale funding and equity fund beyond net loans; Tier 2 = stage 1-2 allowances up to the cap; AT1 and "
                       "level 2 HQLA are zero until the simulator holds such instruments; secured funding run-off uses the "
                       "repo / central-bank factors; the corporate class is company accounts"),
        }
