"""통장 종류와 종류별 현실 약관 기본값 (data only; managers/deposit_accounts.py re-exports it).

The database layer installs its rule triggers from these, so they live below managers.
"""

# 통장 종류 (saving.account_kind, copied onto each account at opening with the
# product's terms, so later product edits never change a signed contract).
ACCOUNT_KINDS = {
    "demand": "입출금통장",
    "parking": "파킹통장",
    "cma": "CMA",
    "time_deposit": "정기예금",
    "fixed_installment": "정기적금",
    "free_installment": "자유적금",
    "housing": "주택청약",
    "combined": "종합통장",
    "isa": "ISA",
    "irp": "IRP",
}
# Kinds whose product terms make the bank take money on a schedule. Like a real
# 정기적금; a 정기예금 is funded once at opening, so it has no installments.
INSTALLMENT_KINDS = ("fixed_installment",)
# Kinds that hold spendable money: automatic flows (dividends, settlements, FX,
# loan repayments, installments) only ever use these.
FLEXIBLE_KINDS = ("demand", "parking", "cma", "combined")
# Kinds whose money cannot be withdrawn piecemeal; only closing releases it.
LOCKED_KINDS = ("time_deposit", "fixed_installment", "free_installment", "housing", "irp")
# Kinds whose deposits are recorded, for limits, 회차 and tax credits.
CONTRIBUTION_KINDS = ("time_deposit", "fixed_installment", "free_installment", "housing", "isa", "irp")
# One account per person across every bank, as the law requires.
ONE_PER_PERSON_KINDS = ("housing", "isa")

# Default contract terms per kind: the real Korean rules (2026-11). A product's
# own kind_terms JSON overrides any key. Amounts are in the account currency.
DAY = 86400
KIND_TERMS = {
    # 우대금리 한도: the preferred rate applies up to the limit, the rest earns excess_rate.
    "parking": {"preferred_limit": 2000000, "excess_rate": 0.1},
    # Securities-firm account: daily interest, outside deposit insurance.
    "cma": {"deposit_insured": 0},
    # 만기후이율: share of the contract rate by days after maturity, then a flat rate.
    "time_deposit": {"post_maturity": [{"days": 30, "percent": 50}, {"days": 90, "percent": 30}, {"rate": 0.2}]},
    "fixed_installment": {"post_maturity": [{"days": 30, "percent": 50}, {"days": 180, "percent": 30}, {"percent": 20}]},
    "free_installment": {"period_days": 30, "period_limit": 1000000,
                         "post_maturity": [{"days": 30, "percent": 50}, {"days": 180, "percent": 30}, {"percent": 20}]},
    # 주택청약종합저축: 월 2만~50만, 회차당 25만 인정, 해지 시 가입기간별 금리.
    "housing": {"period_days": 30, "min_payment": 20000, "max_payment": 500000, "recognized_payment": 250000,
                "rate_tiers": [{"days": 30, "rate": 0}, {"days": 365, "rate": 2.3},
                               {"days": 730, "rate": 2.8}, {"rate": 3.1}]},
    # 마이너스통장: interest on the amount used; unused limits shrink; 연체 adds a spread.
    "combined": {"overdraft_limit": 10000000, "overdraft_rate": 6.0, "min_credit_score": 600,
                 "late_spread": 3.0, "review_after_days": 90, "usage_floor_percent": 50,
                 "reduce_percent": 10, "min_limit_percent": 50},
    # ISA: 연 2,000만(미사용분 이월)·총 1억, 3년 의무가입, 200만 비과세, 초과 9.9% 분리과세.
    "isa": {"annual_limit": 20000000, "total_limit": 100000000, "carry_over": 1, "lock_days": 1095,
            "tax_free": 2000000, "separate_tax_percent": 9.9},
    # IRP: 연 1,800만 납입, 900만까지 세액공제(총급여 5,500만 이하 16.5%, 초과 13.2%),
    # 중도해지 기타소득세 16.5%, 5년 경과 후 연금소득세 5.5%.
    "irp": {"annual_limit": 18000000, "credit_limit": 9000000, "credit_percent_low_income": 16.5,
            "credit_percent": 13.2, "income_threshold": 55000000, "early_tax_percent": 16.5,
            "pension_after_days": 1825, "pension_tax_percent": 5.5},
}
