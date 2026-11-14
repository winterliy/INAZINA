"""Monetary schema inventory; quantities, identifiers and rates remain separate."""
import re
from functools import lru_cache

REGISTERED = {'accounts': ('balance', 'insurance_paid', 'principal', 'reference_price', 'uninsured_claim'), 'aml_alerts': ('amount',), 'aml_monitoring_rules': ('rolling_24h_amount_limit',), 'bank_deficit_alerts': ('cash',), 'collateral_liquidation_events': ('execution_price', 'gross_proceeds', 'liquidation_cost', 'mark_price', 'net_proceeds'), 'company': ('capital', 'cumulative_dividend', 'dividend_per_share', 'liquidation_value_per_share', 'share_price'), 'company_income_log': ('tax_nwx',), 'corporate_equity_offerings': ('issue_price',), 'corporate_liquidation_payments': ('claim_amount', 'paid_amount', 'tax_amount'), 'currency_transactions': ('fee_nwx',), 'currency_transfer_transactions': ('fee_nwx',), 'deposit_insurance_claims': ('covered_amount', 'payout_amount', 'uninsured_amount'), 'depositary_receipt_movements': ('custody_underlying_average_cost', 'holder_underlying_average_cost', 'underlying_average_cost'), 'dividend_distributions': ('deferred_amount', 'entitlement_amount', 'gross_amount', 'reference_price'), 'dividend_payments': ('gross_amount', 'tax_amount'), 'dr_custody_instructions': ('fee_price',), 'dr_deviation_alert_events': ('dr_price', 'reference_value'), 'dr_deviation_samples': ('dr_price', 'reference_value'), 'dr_dividend_withholding_events': ('gross_amount', 'tax_amount'), 'els_products': ('coupon_amount', 'face_value', 'locked_amount', 'principal_amount'), 'els_settlements': ('gross_amount', 'net_amount', 'principal', 'tax_amount'), 'els_subscriptions': ('coupon_reserved', 'principal'), 'els_underlyings': ('initial_price', 'last_price'), 'financial_institution_ledger': ('amount', 'base_amount'), 'financial_institutions': ('deposit_insurance_limit', 'equity_capital', 'retained_earnings'), 'fund_accounts': ('cash', 'initial_nav'), 'fund_cash_ledger': ('amount', 'amount_minor_units', 'balance', 'balance_minor_units'), 'fund_components': ('base_price',), 'futures_positions': ('collateral', 'net_pnl', 'settlement_price'), 'hybrid_bond_events': ('cash',), 'investment': ('conversion_price', 'dividend_per_share', 'face_value', 'nav_anchor', 'unit_price', 'warrant_exercise_price'), 'issuer_cash_accounts': ('balance', 'locked_amount'), 'issuer_cash_transfers': ('delta', 'new_balance', 'old_balance'), 'leveraged_etf_state': ('base_nav',), 'liability': ('loan_limit', 'repayment_amount'), 'loan_collateral_assets': ('average_cost', 'last_price', 'last_value', 'liquidation_cost', 'liquidation_price', 'liquidation_proceeds'), 'loan_ecl_assessments': ('ead', 'expected_loss'), 'loan_payment_schedule': ('amount_paid', 'fee_due', 'fee_paid', 'interest_due', 'interest_paid', 'principal_due', 'principal_paid'), 'loans': ('charged_off_balance', 'collateral_amount', 'collateral_asset_value', 'collateral_liquidated_value', 'collateral_released', 'collateral_seized', 'ecl_allowance', 'mandatory_cost_fee', 'origination_fee', 'outstanding_balance', 'prepayment_fees_paid', 'principal', 'provisioned_loss', 'underwriting_income'), 'lottery_payouts': ('gross', 'net', 'tax'), 'lottery_pool_ledger': ('amount',), 'lottery_purchases': ('price',), 'lottery_rounds': ('reserve_total',), 'lottery_tickets': ('price',), 'market_assets': ('dividend_per_share', 'price'), 'market_auto_target_history': ('anchor_price', 'target_price'), 'market_auto_targets': ('anchor_price', 'target_price'), 'market_event_targets': ('last_price', 'start_price', 'target_price'), 'market_exchange_index_components': ('previous_price',), 'market_future_expiry_prices': ('price',), 'market_last_trade': ('price',), 'market_orders': ('limit_price', 'reserved_cash'), 'market_price_history': ('price',), 'market_quote_overrides': ('price',), 'market_simulation_state': ('last_display_price', 'latent_price'), 'market_trade_history': ('price',), 'option_collateral_cash_lots': ('amount',), 'option_collateral_lots': ('average_cost',), 'option_contracts': ('strike_price',), 'option_positions': ('average_premium',), 'preferred_dividend_claims': ('gross_amount', 'paid_amount'), 'product_settlements': ('gross_amount', 'tax_amount'), 'saving': ('deposit_amount', 'installment_amount'), 'tax_transactions': ('gross_amount', 'net_amount', 'tax_amount'), 'treasury_bond_holdings': ('acquisition_cost', 'face_value', 'principal'), 'treasury_bond_products': ('face_value', 'market_price', 'sold_amount', 'total_supply'), 'user_assets': ('average_cost',), 'users': ('annual_income', 'cash')}

NATIVE_NAMES = frozenset("cash balance price share_price unit_price market_price average_cost capital annual_income principal outstanding_balance amount gross net tax fee per_share gross_nwx tax_nwx fee_nwx treasury_total coupon_reserved initial_nav nav_anchor base_nav delta old_balance new_balance expected_loss ead acquisition_cost collateral net_pnl strike_price limit_price reserved_cash settlement_price face_value deposit_amount loan_limit repayment_amount payout refund premium market_value book_value proceeds target_price anchor_price latent_price last_display_price".split())
UNIT_NAMES = frozenset("balance_units reserved_cash_units executed_value_units net_amount_units debit_units credit_units discount_units source_units target_units baseline_supply_units".split())
EXCLUDED = {('asset_cost_basis','total_minor_units')}
REGISTERED['swap_contracts'] = ('notional', 'margin', 'realized')
MINOR_TABLES = {'financial_lines': {'amount'}, 'fund_accounts': {'cash'}}


_ALL_REGISTERED = frozenset(field for fields in REGISTERED.values() for field in fields)


@lru_cache(maxsize=8192)
def base_table(table):
    return re.sub(r'_(?:new|old|wide|v[0-9]+|legacy|migrating)$', '', str(table).lower())


@lru_cache(maxsize=16384)
def money_kind(table, column):
    table, column = base_table(table), str(column).lower()
    if (table, column) in EXCLUDED:
        return None
    if column.endswith(('_decimal_places','_percent','_bps','_epoch')) or column in {'quantity','remaining_quantity','filled_quantity','total_shares','listed_shares','index_value','base_index_value','income_stability','bond_units','decimal_places'}:
        return None
    if column.endswith(('_minor','_minor_units','_subminor_units')) or column in UNIT_NAMES or column in MINOR_TABLES.get(table,set()):
        return 'MONEYINT'
    if column in REGISTERED.get(table,()) or column in NATIVE_NAMES:
        return 'MONEY'
    return None


@lru_cache(maxsize=8192)
def possible_money(column):
    return money_kind('',column) is not None or column in _ALL_REGISTERED
